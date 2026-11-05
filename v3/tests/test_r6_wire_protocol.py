import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from mgar_v3.benchmark.domain import Case, EvaluationAuthorization, Source, Summary
from mgar_v3.benchmark.executor import Executor, bounded
from mgar_v3.benchmark.oracle import grade, hidden_suite
from mgar_v3.benchmark.prepare import DATA
from mgar_v3.benchmark.wire import QuoteChoice, WireAnswer, WirePatch, decode, quote_sources
from mgar_v3.domain import DomainError, digest
from mgar_v3.live import Authorization, LivePermit, LiveProfile
from mgar_v3.models.structured import StructuredModel
from mgar_v3.proactivity.domain import Activation, Policy, ProactiveEvent
from mgar_v3.repair.body import body_diff
from mgar_v3.repair.sandbox import Sandbox
from mgar_v3.repair.workspace import apply_diff, fixture_snapshot, tree


def test_quote_selection_resolves_exact_entire_text_and_rejects_forgery():
    text = "Paid only. The cap is 4. Earliest event per order. Include zero. Check implementation."
    source = Source(id="read-1", role="knowledge", text=text, version="fixed")
    shown, catalog = quote_sources(bounded([source]))
    assert shown[0]["quote_segments"] == [{"quote_id": "s0q0", "text": text}]
    wire = WireAnswer(status="supported", values={"limit": "4"}, citations=[{"quote_id": "s0q0"}])
    answer = decode(wire, catalog, None)
    assert answer.citations[0].quote == text
    assert grade(
        answer.model_dump(mode="json"),
        {"status": "supported", "values": {"limit": "4"}},
        [source.model_dump(mode="json")],
        ["knowledge"],
    )["passed"]
    wire = wire.model_copy(update={"citations": [QuoteChoice(quote_id="s99q0")]})
    with pytest.raises(DomainError, match="supplied quote_id"):
        decode(wire, catalog, None)
    with pytest.raises(ValidationError):
        QuoteChoice.model_validate({"quote_id": "s0q0", "quote": "Joined text that was never read"})
    with pytest.raises(ValidationError):
        WirePatch.model_validate({"status": "supported", "diff": "model-controlled hunk"})


def test_quote_segments_keep_all_unicode_text_with_bounded_exact_spans():
    text = "零金额也必须保留。" * 350
    source = Source(id="read-1", role="data", text=text, version="fixed")
    shown, catalog = quote_sources([source.model_dump(mode="json")])
    assert "".join(s["text"] for s in shown[0]["quote_segments"]) == text
    assert all(0 < len(c.quote) <= 1200 and c.quote in text for c in catalog.values())


BODIES = [
    (
        'v:=map[string]int{};for _,o:=range orders{if o.Status=="paid"{v[o.ID]='
        "o.Amount}};total:=0;for _,n:=range v{total+=n};return total"
    ),
    (
        'v:=map[string]int{};for _,o:=range orders{if o.Status=="paid"{v[o.ID]='
        "o.Amount}else{v[o.ID]=0}};total:=0;for _,n:=range v{total+=n};return t"
        "otal"
    ),
    (
        'v:=map[string]int{};for _,o:=range orders{if o.Status=="paid"&&o.Amoun'
        "t>0{if _,ok:=v[o.ID];!ok{v[o.ID]=o.Amount}}};total:=0;for _,n:=range v"
        "{total+=n};return total"
    ),
    (
        'v:=map[string]int{};for _,o:=range orders{if o.Status=="paid"{v[o.ID]+'
        "=o.Amount}};total:=0;for _,n:=range v{if n>100{n=100};total+=n};return"
        " total"
    ),
    (
        'v:=map[string]int{};for _,o:=range orders{if o.Status=="paid"{n,ok:=v['
        "o.ID];if !ok||o.Amount>n{v[o.ID]=o.Amount}}};total:=0;for _,n:=range v"
        "{total+=n};return total"
    ),
]


@pytest.mark.parametrize("variant", range(5))
def test_body_only_contract_generates_exact_patch_and_passes_real_independent_go(variant):
    prefix = DATA / "public" / ("repair-" + str(variant))
    files = {p.name: p.read_text() for p in prefix.iterdir() if p.is_file()}
    snapshot = fixture_snapshot("example/mgar-test").model_copy(
        update={"files": files, "base_tree": tree(files)},
    )
    source = Source(id="read-repo", role="repo", text=files["orders.go"], version="frozen")
    _, catalog = quote_sources(bounded([source]))
    proposal = WirePatch(
        status="supported", function_body=BODIES[variant], citations=[{"quote_id": "s0q0"}]
    )
    canonical = decode(proposal, catalog, snapshot)
    assert canonical.values == {} and canonical.diff.startswith("--- a/orders.go\n")
    candidate = apply_diff(snapshot, canonical.diff)
    proof, log = Sandbox("v5-body-" + str(variant)).run(
        {**candidate, "zz_r6_hidden_test.go": hidden_suite(variant)},
        ["TestPaidRevenueCents", "TestR6Hidden"],
    )
    assert proof["passed"], log.decode()
    assert len([t for t in proof["tests"] if t.startswith("TestR6Hidden/")]) == 28


def test_body_contract_still_rejects_outside_function_injection_and_unsupported_frames():
    snapshot = fixture_snapshot("example/mgar-test")
    patch = body_diff(snapshot, "return 0\n}\nfunc Injected() int { return 1")
    with pytest.raises(DomainError):
        apply_diff(snapshot, patch)
    for body in ["", "return 1\r\n", "a" * 16385, "return 1\x00"]:
        with pytest.raises(DomainError):
            body_diff(snapshot, body)
    unsupported = snapshot.model_copy(
        update={
            "files": {
                **snapshot.files,
                "orders.go": snapshot.files["orders.go"] + "// trailing function unknown\n",
            }
        }
    )
    with pytest.raises(DomainError, match="frame is unsupported"):
        body_diff(unsupported, "return 1")


def test_known_malformed_expert_is_charged_once_then_master_uses_original_evidence(
    engine, monkeypatch
):
    engine.settings.allow_paid = True
    engine.settings.deepseek_api_key = SecretStr("controlled-model-wire-key")
    profile = LiveProfile(
        case_id="r6-eval:wire-controlled",
        project_id=engine.settings.project_id,
        repository=engine.settings.repository,
        issue_number=1,
        actor="owner",
        question="What is the cap?",
        source_ref="a" * 40,
        model_id="controlled",
        price_version="controlled",
        input_micros_per_million=300000,
        output_micros_per_million=1200000,
        context_tokens=12288,
        max_output_tokens=2048,
        max_model_calls=3,
        max_cost_micros=18432,
    )
    manifest = {
        "repository": engine.settings.repository,
        "max_new_calls": 3,
        "max_new_cost_micros": 18432,
        "prior_cost_micros": 0,
        "profiles": [
            {
                "case_id": "wire-qa",
                "arm": "C",
                "profile": profile.model_dump(mode="json"),
                "profile_hash": profile.profile_hash,
            }
        ],
    }
    expires = datetime.now(UTC) + timedelta(hours=1)
    grant = EvaluationAuthorization(
        manifest_hash=digest(manifest),
        approved_by="owner",
        expires_at=expires,
        max_new_calls=3,
        max_new_cost_micros=18432,
        allow_paid=True,
    )
    budget = engine.benchmarks.authorize(manifest, grant)
    permit = LivePermit(
        profile,
        Authorization(
            profile_hash=profile.profile_hash,
            approved_by="owner",
            expires_at=expires,
            allow_paid=True,
        ),
        engine.settings,
    )
    policy = Policy(
        repository=engine.settings.repository,
        kinds=["pr_comment"],
        allowed_actors=["owner"],
        source_ref="a" * 40,
        question=profile.question,
        expires_at=expires,
        execution_mode="live",
        model_profile_hash=profile.profile_hash,
        max_model_calls=3,
        max_cost_micros=18432,
    )
    engine.proactivity.create(policy, "owner", permit)
    engine.proactivity.activate(
        Activation(policy_hash=policy.policy_hash, mode="auto"), "owner", permit
    )
    run = engine.proactivity.observe(
        ProactiveEvent(
            external_event_id="wire-once",
            repository=engine.settings.repository,
            actor="owner",
            kind="pr_comment",
            issue_number=1,
            source_ref="a" * 40,
        ),
        permit,
    )[0]["run_id"]
    engine.benchmarks.bind(run, digest(manifest), "wire-qa", "C")
    epoch = engine.begin_run(run)["execution_epoch"]
    source = Source(
        id=str(uuid4()),
        role="knowledge",
        text="The cap is 45.",
        version="fixed",
        document_id="cap",
        revision="current",
    )
    monkeypatch.setattr(
        "mgar_v3.benchmark.executor.collect", lambda *a, **k: ([source], [], {"tool_calls": 1})
    )
    original = json.loads(
        (
            DATA.parents[1] / "docs/repairs/R6-20261103/expert-malformed-reproduction.json"
        ).read_text()
    )
    seen = []

    def respond(request):
        body = json.loads(request.content)
        assert any("json" in str(m["content"]).lower() for m in body["messages"])
        seen.append(body)
        if len(seen) == 1:
            content = original["raw_content"]
        else:
            payload = json.loads(body["messages"][1]["content"])
            assert (
                payload["expert_notes"] == []
                and payload["expert_failures"][0]["code"] == "MODEL_INVALID_RESPONSE"
            )
            assert "The cap is 45." in json.dumps(payload["sources"])
            content = json.dumps(
                {
                    "status": "supported",
                    "values": {"limit": "45"},
                    "citations": [{"quote_id": "s0q0"}],
                    "explanation": "Original evidence.",
                }
            )
        return httpx.Response(
            200,
            json={
                "model": "controlled",
                "usage": {"prompt_tokens": 100, "completion_tokens": 100},
                "choices": [{"finish_reason": "stop", "message": {"content": content}}],
            },
        )

    executor = Executor(
        engine,
        permit,
        run,
        epoch,
        Case(
            id="wire-qa",
            category="qa",
            question=profile.question,
            required_roles=["knowledge"],
            document_id="cap",
            required_revision="current",
            source_path="public.md",
            output_contract={"fields": {"limit": "decimal"}},
        ),
        "C",
        {},
    )
    executor.model = StructuredModel(
        engine, permit, httpx.Client(transport=httpx.MockTransport(respond))
    )
    answer, _, stats = executor.execute()
    assert answer.values == {"limit": "45"} and len(seen) == executor.calls == 2
    assert stats["expert_failures"] == [{"role": "knowledge", "code": "MODEL_INVALID_RESPONSE"}]
    with engine.transaction() as conn:
        calls = conn.execute(
            "SELECT status,result FROM app.usage_calls ORDER BY step_key"
        ).fetchall()
        assert (
            len(calls) == 2
            and all(c["status"] == "known" for c in calls)
            and calls[0]["result"] is None
        )
        assert (
            conn.execute(
                "SELECT used_calls FROM app.benchmark_budgets WHERE budget_hash=%s", (budget,)
            ).fetchone()["used_calls"]
            == 2
        )
        assert not conn.execute("SELECT 1 FROM app.outbox").fetchone()


def test_unknown_expert_outcome_never_falls_back_to_another_paid_call(engine, monkeypatch):
    source = Source(
        id="read-1",
        role="knowledge",
        text="cap45",
        version="fixed",
        document_id="cap",
        revision="current",
    )
    monkeypatch.setattr(
        "mgar_v3.benchmark.executor.collect", lambda *a, **k: ([source], [], {"tool_calls": 1})
    )

    class Unknown:
        def complete(self, *args):
            assert args[-1] is Summary
            raise DomainError("USAGE_UNKNOWN", "Do not send anything else")

    executor = Executor(
        engine,
        None,
        uuid4(),
        1,
        Case(
            id="unknown",
            category="qa",
            question="cap?",
            required_roles=["knowledge"],
            document_id="cap",
            required_revision="current",
            source_path="public.md",
            output_contract={"fields": {"limit": "decimal"}},
        ),
        "C",
        {},
    )
    executor.model = Unknown()
    with pytest.raises(DomainError, match="Do not send anything else"):
        executor.execute()
    assert executor.calls == 1


def test_supervisor_returns_success_after_exactly_ninety_settled_assignments(
    tmp_path, monkeypatch, capsys
):
    import copy
    import runpy
    import shutil
    import subprocess
    import sys

    namespace = runpy.run_path(str(DATA.parents[1] / "scripts/final_eval_supervisor.py"))
    main = namespace["main"]
    globs = main.__globals__
    rows = [{"case_id": str(i), "arm": "A", "status": "NOT_RUN"} for i in range(90)]
    manifest = {"assignments": copy.deepcopy(rows)}
    (tmp_path / "results.json").write_text(json.dumps(rows))
    monkeypatch.setitem(globs, "verify", lambda *a: manifest)
    monkeypatch.setitem(globs, "resume_state", lambda *a: (rows, {}))
    monkeypatch.setattr(shutil, "disk_usage", lambda *a: type("Space", (), {"free": 2 * 1024**3})())
    invocations = []

    def settle(command, **kwargs):
        index = len(invocations)
        invocations.append(command)
        rows[index]["status"] = "PASS"
        (tmp_path / "execution.json").write_text("{}")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", settle)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "supervisor",
            "--directory",
            str(tmp_path),
            "--authorization",
            str(tmp_path / "auth.json"),
            "--execute-paid",
        ],
    )
    main()
    assert len(invocations) == 90
    assert json.loads(capsys.readouterr().out.splitlines()[-1]) == {
        "status": "completed",
        "assignments": 90,
    }


def test_provider_json_mode_precondition_is_checked_before_any_call_or_reservation(engine):
    from mgar_v3.benchmark.executor import SYSTEM_V5
    from mgar_v3.benchmark.wire import require_json_mode

    require_json_mode(SYSTEM_V5, {"answer_schema": WireAnswer.model_json_schema()})
    require_json_mode("Return JSON", {})
    require_json_mode("Use the requested format", {"format": "json"})

    class NeverSend:
        def complete(self, *args):
            pytest.fail("Invalid local JSON-mode request reached the model transport")

    executor = Executor(
        engine,
        None,
        uuid4(),
        1,
        Case(
            id="preflight",
            category="qa",
            question="cap?",
            required_roles=["knowledge"],
            source_path="public.md",
            output_contract={"fields": {"limit": "decimal"}},
        ),
        "A",
        {},
    )
    executor.model = NeverSend()
    with pytest.raises(DomainError, match="requires JSON"):
        executor.complete(
            "Answer the bounded task", {"answer_schema": {"type": "object"}}, WireAnswer
        )
    assert executor.calls == 0
    with engine.transaction() as conn:
        assert not conn.execute("SELECT 1 FROM app.usage_calls").fetchone()
        assert not conn.execute(
            "SELECT 1 FROM app.run_events WHERE kind='model_input_recorded'"
        ).fetchone()


def test_invalid_master_contract_stops_before_experts_or_tools(engine, monkeypatch):
    monkeypatch.setattr("mgar_v3.benchmark.executor.SYSTEM_V5", "Use the requested output format")

    def forbidden(*args, **kwargs):
        pytest.fail("Invalid producer contract must stop before tools or paid expert branches")

    monkeypatch.setattr("mgar_v3.benchmark.executor.collect", forbidden)
    executor = Executor(
        engine,
        None,
        uuid4(),
        1,
        Case(
            id="contract-guard",
            category="qa",
            question="cap?",
            required_roles=["knowledge"],
            source_path="public.md",
            output_contract={"fields": {"limit": "decimal"}},
        ),
        "C",
        {},
    )
    with pytest.raises(DomainError, match="requires JSON"):
        executor.execute()
    assert executor.calls == 0
