import ast
import base64
import hashlib
import json
import random
from uuid import uuid4

import pytest

from mgar_v3.agents.data import provision
from mgar_v3.agents.numeric import revenue_metrics
from mgar_v3.benchmark.contract import capability_decision, evidence_decision, output_issues
from mgar_v3.benchmark.domain import Answer, Case, OutputContract, Source, Summary
from mgar_v3.benchmark.executor import Executor, bounded, public_source
from mgar_v3.benchmark.oracle import cross_expected
from mgar_v3.benchmark.prepare import DATA
from mgar_v3.benchmark.tools import collect
from mgar_v3.benchmark.wire import WireAnswer, WirePatch
from mgar_v3.domain import DomainError, digest
from mgar_v3.rag.domain import ACLUpdate, Upload


def case(**changes):
    return Case.model_validate(
        {
            "id": "unseen-request",
            "category": "qa",
            "question": "What is the cap?",
            "source_path": "public.py",
            "required_roles": ["knowledge"],
            "document_id": "wanted",
            "required_revision": "current",
            "output_contract": {"fields": {"limit": "decimal"}},
            **changes,
        }
    )


def source(role="knowledge", text="The cap is 4.", **changes):
    return Source(
        id=str(uuid4()),
        role=role,
        text=text,
        version="content-hash",
        document_id="wanted",
        revision="current",
        **changes,
    )


def test_exact_amounts_keep_zero_negative_first_paid_and_ignore_prior_cancelled():
    rows = [
        {"event_id": 1, "order_id": "a", "status": "cancelled", "amount": "99"},
        {"event_id": 2, "order_id": "a", "status": "paid", "amount": "0.10"},
        {"event_id": 3, "order_id": "a", "status": "paid", "amount": "0.20"},
        {"event_id": 4, "order_id": "b", "status": "paid", "amount": "0.00"},
        {"event_id": 5, "order_id": "b", "status": "paid", "amount": "0.30"},
        {"event_id": 6, "order_id": "c", "status": "paid", "amount": "-0.20"},
    ]
    assert revenue_metrics(rows) == {
        "paid_events": "5",
        "distinct_orders": "3",
        "event_amount": "0.40",
        "dedup_amount": "-0.10",
    }
    assert revenue_metrics(list(reversed(rows))) == revenue_metrics(rows)
    assert revenue_metrics([])["dedup_amount"] == "0"


@pytest.mark.parametrize("amount", [0.1, True, "NaN", "Infinity", "1e999"])
def test_unsafe_amount_types_fail_closed(amount):
    with pytest.raises(DomainError, match="exact bounded data"):
        revenue_metrics([{"event_id": 1, "order_id": "a", "status": "paid", "amount": amount}])


def test_duplicate_event_keys_cannot_silently_change_dedup():
    r = {"event_id": 1, "order_id": "a", "status": "paid", "amount": "2"}
    with pytest.raises(DomainError):
        revenue_metrics([r, {**r, "amount": "3"}])


def test_runtime_arithmetic_matches_independent_oracle_on_unseen_permutations():
    rng = random.Random(190325)
    for _ in range(80):
        rows = [
            {
                "event_id": n,
                "order_id": str(rng.randrange(5)),
                "status": rng.choice(["paid", "pending", "cancelled"]),
                "amount": str(rng.randrange(-1000, 1000)),
            }
            for n in range(20)
        ]
        expected = cross_expected(rows)
        rng.shuffle(rows)
        assert revenue_metrics(rows) == {k: expected[k] for k in revenue_metrics(rows)}


def test_public_capabilities_and_evidence_not_hidden_labels_control_decisions():
    s = source()
    request = case(negative_condition="forbidden_write")
    assert capability_decision(request) is None
    assert evidence_decision(request, [s], []) is None
    assert capability_decision(case(requested_capabilities=["external_write"])).status == "denied"
    assert evidence_decision(case(), [], ["knowledge:KNOWLEDGE_REVOKED"]).status == "denied"
    assert (
        evidence_decision(case(), [], ["knowledge:KNOWLEDGE_DOCUMENT_NOT_FOUND"]).status
        == "insufficient"
    )
    assert evidence_decision(case(required_revision="newer"), [s], []).status == "insufficient"
    assert evidence_decision(case(document_id="other"), [s], []).status == "insufficient"


@pytest.mark.parametrize("value", ["yes", "no", "False", "0", ""])
def test_boolean_strings_are_explicit_and_never_posthoc_coerced(value):
    s = source()
    c = case(output_contract={"fields": {"flag": "boolean"}})
    answer = Answer(
        status="supported", values={"flag": value}, citations=[{"source_id": s.id, "quote": s.text}]
    )
    assert "canonical_boolean_required:flag" in output_issues(answer, c, [s])
    assert not output_issues(answer.model_copy(update={"values": {"flag": "false"}}), c, [s])


def test_final_contract_rejects_numeric_disagreement_forged_refs_and_extra_fields():
    s = source(role="data", text="Observed amount 0.10", numeric_facts={"amount": "0.10"})
    c = case(
        required_roles=["data"], document_id=None, output_contract={"fields": {"amount": "decimal"}}
    )
    answer = Answer(
        status="supported",
        values={"amount": "0.20"},
        citations=[{"source_id": s.id, "quote": s.text}],
    )
    assert "numeric_evidence_conflict:amount" in output_issues(answer, c, [s])
    good = answer.model_copy(update={"values": {"amount": "0.10"}})
    assert not output_issues(good, c, [s])
    assert output_issues(
        good.model_copy(update={"values": {"amount": "0.10", "extra": "1"}}), c, [s]
    )
    bad = Answer(
        status="supported",
        values={"amount": "0.10"},
        citations=[{"source_id": "fabricated", "quote": s.text}],
    )
    assert "invalid_citation" in output_issues(bad, c, [s])
    assert output_issues(Answer(status="denied", values={"amount": "0"}), c, [s])
    assert output_issues(Answer(status="insufficient", diff="not allowed"), c, [s])
    assert output_issues(
        Answer(status="supported", values={"limit": "4"}, diff="patch"),
        case(category="repair", output_contract={}),
        [source()],
    )


def test_execution_fails_closed_after_three_known_invalid_outputs(engine, monkeypatch):
    s = source()
    monkeypatch.setattr(
        "mgar_v3.benchmark.executor.collect",
        lambda *a, **k: (
            [s],
            [],
            {"tool_calls": 1, "recall_at5": 1},
        ),
    )

    class Controlled:
        def complete(self, *args):
            if args[-1] is Summary:
                return Summary(notes="No additional evidence needed.")
            return WireAnswer(
                status="supported",
                values={"limit": "4"},
                citations=[{"quote_id": "s99q0"}],
            )

    for arm in "ABC":
        executor = Executor(engine, None, uuid4(), 1, case(), arm, {})
        executor.model = Controlled()
        answer, _, _ = executor.execute()
        assert answer.status == "insufficient" and not answer.values and not answer.diff
        assert executor.calls == 3


def test_capability_denial_prevents_tools_and_paid_model_access(engine, monkeypatch):
    def forbidden(*a, **k):
        pytest.fail("Denied capability must not reach evidence tools or paid model")

    monkeypatch.setattr("mgar_v3.benchmark.executor.collect", forbidden)
    executor = Executor(
        engine, None, uuid4(), 1, case(requested_capabilities=["external_write"]), "C", {}
    )
    answer, sources, stats = executor.execute()
    assert answer.status == "denied" and sources == [] and stats["tool_calls"] == 0
    assert executor.calls == 0


def test_scoped_real_postgres_arithmetic_and_revocation_never_use_case_expected_rows(
    engine, trigger, tmp_path
):
    for title in ["wanted", "unrelated"]:
        job = engine.knowledge.upload(
            Upload(
                title=title,
                source_uri="kb://contract/" + title,
                source_version="current",
                media_type="text/markdown",
                content_base64=base64.b64encode((title + ": cap is 4.").encode()).decode(),
            )
        )
        engine.knowledge.index(job["job_id"])
    documents = {d["title"]: str(d["document_id"]) for d in engine.knowledge.documents()}
    snapshot = str(engine.knowledge.active())
    rows = [
        {"event_id": 1, "order_id": "a", "status": "paid", "amount": "20"},
        {"event_id": 2, "order_id": "a", "status": "paid", "amount": "25"},
        {"event_id": 3, "order_id": "b", "status": "paid", "amount": "0"},
        {"event_id": 4, "order_id": "b", "status": "paid", "amount": "28"},
    ]
    reader = provision(engine, tmp_path / "reader.json", rows)
    content = (
        "# current\ndef paid_event_revenue(rows):\n"
        '    return sum(row["amount"] for row in rows if row["status"] == "paid")\n'
    )
    inputs = {
        "projects": {
            engine.settings.project_id: {
                "snapshot_id": snapshot,
                "rag_config_hash": digest(engine.knowledge.config()),
                "documents": documents,
            }
        },
        "sources": {
            "public.py": {
                "content": content,
                "sha256": hashlib.sha256(content.encode()).hexdigest(),
                "blob": "fixed",
            }
        },
        "readers": {
            "unseen-request": {
                "path": str(tmp_path / "reader.json"),
                "fixture_hash": reader["fixture_hash"],
            }
        },
    }
    run = engine.ingest(trigger).run_id
    epoch = engine.begin_run(run)["execution_epoch"]
    request = case(
        category="cross",
        required_roles=["knowledge", "repo", "data"],
        rows=[{"fake_expected_answer": 999999}],
    )
    sources, errors, stats = collect(engine, run, epoch, request, inputs)
    assert not errors
    assert {s.role for s in sources} == {"knowledge", "repo", "data"}
    assert all(s.document_id == "wanted" for s in sources if s.role == "knowledge")
    data = next(s for s in sources if s.role == "data")
    assert data.numeric_facts["dedup_amount"] == "20.00"
    assert data.numeric_facts["event_amount"] == "73.00"
    assert len(data.audit_artifacts) == 3
    audit = [json.loads(engine.artifacts.read(h)) for h in data.audit_artifacts]
    assert all("result_hash" in r and "observed_at" in r for r in audit)
    again, _, _ = collect(engine, run, epoch, request, inputs, supplement=True)
    repeated = next(s for s in again if s.role == "data")
    assert repeated.text == data.text and repeated.numeric_facts == data.numeric_facts
    assert repeated.audit_artifacts != data.audit_artifacts
    assert len(bounded([data, repeated])) == 1
    assert "audit_artifacts" not in public_source(data)
    assert all(h not in json.dumps(bounded([data, repeated])) for h in data.audit_artifacts)
    assert "sql_result_hashes" not in data.text
    assert stats["tool_calls"] <= 8
    repair_sources, _, _ = collect(
        engine,
        run,
        epoch,
        case(category="repair", required_roles=["repo"], document_id=None),
        inputs,
    )
    assert [s.role for s in repair_sources] == ["repo"]
    from uuid import UUID

    engine.knowledge.acl(UUID(documents["wanted"]), ACLUpdate(revoked=True))
    sources, errors, _ = collect(engine, run, epoch, case(), inputs)
    assert sources == [] and errors == ["knowledge:KNOWLEDGE_REVOKED"]
    assert evidence_decision(case(), sources, errors).status == "denied"
    sources, errors, _ = collect(engine, run, epoch, case(document_id="nonexistent"), inputs)
    assert sources == [] and evidence_decision(case(), sources, errors).status == "insufficient"
    with engine.transaction() as conn:
        for table in ["usage_calls", "actions", "outbox"]:
            assert not conn.execute("SELECT 1 FROM app." + table).fetchone()


def test_runtime_fix_cannot_read_negative_labels_case_rows_or_independent_oracles():
    root = DATA.parents[1] / "src/mgar_v3"
    for name in [
        "benchmark/contract.py",
        "benchmark/scoped_tools.py",
        "benchmark/executor.py",
        "agents/numeric.py",
    ]:
        tree = ast.parse((root / name).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert not node.module or "oracle" not in node.module
            if isinstance(node, ast.Attribute):
                assert node.attr not in {"negative_condition", "repair_variant"}
                assert not (
                    node.attr == "rows"
                    and isinstance(node.value, ast.Attribute)
                    and node.value.attr == "case"
                )


def test_new_public_contracts_keep_original_business_inputs_and_frozen_judge():
    original = json.loads((DATA / "cases.json").read_text())
    revised = json.loads((DATA.parent / "final-v3/cases.json").read_text())
    assert len(revised) == len(original) == 30
    for old, new in zip(original, revised, strict=True):
        assert all(new[key] == value for key, value in old.items())
        assert OutputContract.model_validate(new["output_contract"]).version == "r6-output-v3"


def test_repair_output_survives_full_public_and_hidden_pipeline_without_optional_knowledge(
    engine, monkeypatch
):
    import difflib

    from mgar_v3.benchmark.oracle import hidden_suite
    from mgar_v3.repair.sandbox import Sandbox
    from mgar_v3.repair.source import working_files
    from mgar_v3.repair.workspace import fixture_snapshot, tree

    prefix = DATA / "public/repair-0"
    files = {p.name: p.read_text() for p in prefix.iterdir() if p.is_file()}
    before = files["orders.go"]
    after = (
        before[: before.index("func PaidRevenueCents")]
        + """func PaidRevenueCents(orders []Order) int {
    selected := make(map[string]int)
    for _, order := range orders {
        if order.Status == "paid" { selected[order.ID] = order.Amount }
    }
    total := 0
    for _, amount := range selected { total += amount }
    return total
}
"""
    )
    patch = "".join(
        difflib.unified_diff(
            before.splitlines(True),
            after.splitlines(True),
            fromfile="a/orders.go",
            tofile="b/orders.go",
        )
    )
    snapshot = fixture_snapshot(engine.settings.repository).model_copy(
        update={"files": files, "base_tree": tree(files)},
    )
    s = source(role="repo", text=before)
    monkeypatch.setattr(
        "mgar_v3.benchmark.executor.collect",
        lambda *a, **k: (
            [s],
            [],
            {"tool_calls": 1, "recall_at5": None},
        ),
    )
    seen = []

    class Controlled:
        def complete(self, *args):
            payload, schema = args[-2:]
            if schema is Summary:
                if payload["role"] == "knowledge":
                    assert payload["required"] is False and not payload["sources"]
                return Summary(notes="Only the requested Repo patch contract applies.")
            seen.append(payload)
            assert payload["required_contract_version"] is None
            assert payload["output_contract"]["fields"] == {}
            assert all(s["role"] == "repo" for s in payload["sources"])
            assert {n["role"]: n["required"] for n in payload["expert_notes"]} == {
                "knowledge": False,
                "repo": True,
            }
            return WirePatch(
                status="supported",
                function_body=after.split("func PaidRevenueCents(orders []Order) int {", 1)[
                    1
                ].rsplit("}", 1)[0],
                citations=[{"quote_id": "s0q0"}],
            )

    request = case(
        category="repair",
        required_roles=["repo"],
        document_id=None,
        required_revision=None,
        output_contract={},
    )
    run = uuid4()
    executor = Executor(
        engine,
        None,
        run,
        1,
        request,
        "B",
        {"repair_snapshots": {request.id: snapshot.model_dump(mode="json")}},
    )
    executor.model = Controlled()
    answer, _, _ = executor.execute()
    assert answer.status == "supported" and answer.values == {} and answer.diff == patch
    assert len(seen) == 1 and executor.calls == 3
    assert executor.final_files is not None
    receipt, log = Sandbox(str(run)).run(
        {**working_files(snapshot, executor.final_files), "zz_r6_hidden_test.go": hidden_suite(0)},
        ["TestPaidRevenueCents", "TestR6Hidden"],
    )
    assert receipt["passed"], log.decode()
    assert len([n for n in receipt["tests"] if n.startswith("TestR6Hidden/")]) == 28


def test_remaining_call_envelope_is_frozen_below_task_reservations(tmp_path, monkeypatch):
    from mgar_v3.benchmark import run

    m = json.loads((DATA.parents[1] / "eval/reports/final-v2-manifest.json").read_text())
    m["cases"] = [
        Case.model_validate(c).model_dump(mode="json")
        for c in json.loads((DATA / "cases.json").read_text())
    ]
    m["max_new_calls"] = 202
    m["max_new_cost_micros"] = 202 * 6144
    (tmp_path / "inputs.json").write_text("{}")
    m["inputs_sha256"] = hashlib.sha256(b"{}").hexdigest()
    monkeypatch.setattr(run, "fingerprint", lambda: m["implementation_sha256"])
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(m))
    assert run.verify(tmp_path)["max_new_calls"] == 202
    for cap, cost in [(271, 271 * 6144), (202, 202 * 6144 + 1), (0, 0)]:
        path.write_text(json.dumps({**m, "max_new_calls": cap, "max_new_cost_micros": cost}))
        with pytest.raises(DomainError):
            run.verify(tmp_path)


def test_postgres_remaining_group_cap_does_not_refill_profile_calls(engine, trigger):
    from datetime import UTC, datetime, timedelta

    from mgar_v3.benchmark.domain import EvaluationAuthorization

    engine.settings.allow_paid = True
    manifest = {
        "repository": engine.settings.repository,
        "profiles": [],
        "max_new_calls": 2,
        "max_new_cost_micros": 12288,
        "prior_cost_micros": 0,
    }
    grant = EvaluationAuthorization(
        manifest_hash=digest(manifest),
        approved_by="owner",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        max_new_calls=2,
        max_new_cost_micros=12288,
        allow_paid=True,
    )
    budget = engine.benchmarks.authorize(manifest, grant)
    run = engine.ingest(trigger).run_id
    with engine.transaction() as conn:
        conn.execute(
            "INSERT INTO app.benchmark_runs VALUES (%s,%s,%s,%s)",
            (run, digest(manifest), "bounded", "A"),
        )
    for _ in range(2):
        with engine.transaction() as conn:
            assert engine.benchmarks.reserve(conn, run, 6144) == budget
    with pytest.raises(DomainError, match="shared grant exhausted"):
        with engine.transaction() as conn:
            engine.benchmarks.reserve(conn, run, 6144)
    with engine.transaction() as conn:
        row = conn.execute(
            "SELECT * FROM app.benchmark_budgets WHERE budget_hash=%s", (budget,)
        ).fetchone()
        assert row["used_calls"] == row["call_limit"] == 2
        assert row["reserved_micros"] == 12288
