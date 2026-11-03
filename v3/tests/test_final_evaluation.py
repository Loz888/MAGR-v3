import ast
import difflib
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr

from mgar_v3.benchmark.domain import Case, EvaluationAuthorization, Source
from mgar_v3.benchmark.executor import Executor
from mgar_v3.benchmark.metrics import summarize
from mgar_v3.benchmark.oracle import cross_expected, grade, hidden_suite
from mgar_v3.benchmark.prepare import DATA
from mgar_v3.domain import DomainError, digest
from mgar_v3.live import Authorization, LivePermit, LiveProfile
from mgar_v3.models.structured import StructuredModel
from mgar_v3.proactivity.domain import Activation, Policy, ProactiveEvent
from mgar_v3.repair.sandbox import Sandbox
from mgar_v3.repair.workspace import apply_diff, fixture_snapshot


def assignments(status="NOT_RUN"):
    cases = json.loads((DATA / "cases.json").read_text())
    return [
        {
            "case_id": c["id"],
            "arm": a,
            "category": c["category"],
            "status": status,
            "write_invariant_checked": True,
            "forbidden_actions": 0,
            "citations": 1 if c["category"] != "repair" else 0,
            "valid_citations": 1 if c["category"] != "repair" else 0,
        }
        for c in cases
        for a in "ABC"
    ]


def test_fixed_denominators_unknowns_and_no_tasks_can_never_pass():
    assert summarize(assignments())["status"] == "NOT_RUN"
    with pytest.raises(ValueError):
        summarize([])
    with pytest.raises(ValueError):
        summarize(assignments()[:-1])
    full = assignments("PASS")
    assert summarize(full)["quality_gate_passed"]
    full[0]["status"] = "UNKNOWN"
    assert not summarize(full)["quality_gate_passed"]
    bad = assignments("PASS")
    for r in bad:
        if r["arm"] == "C" and r["category"] == "negative":
            r["status"] = "FAIL"
    summary = summarize(bad)
    assert summary["status"] == "FAIL" and summary["arms"]["C"]["negative_total"] == 5
    assert summary["arms"]["C"]["normal_total"] == 25


def test_independent_oracle_detects_wrong_numbers_refs_and_hidden_tests():
    sources = [{"id": "read-1", "role": "knowledge", "text": "The cap is 4."}]
    answer = {
        "status": "supported",
        "values": {"limit": "4"},
        "citations": [{"source_id": "read-1", "quote": "cap is 4"}],
    }
    expected = {"status": "supported", "values": {"limit": "4"}}
    assert grade(answer, expected, sources, ["knowledge"])["passed"]
    assert not grade({**answer, "values": {"limit": "5"}}, expected, sources, ["knowledge"])[
        "passed"
    ]
    assert not grade(
        {**answer, "citations": [{"source_id": "unread", "quote": "cap is 4"}]},
        expected,
        sources,
        ["knowledge"],
    )["passed"]
    assert not grade(answer, expected, sources, ["knowledge"], False)["passed"]
    assert (
        cross_expected(
            [
                {"event_id": 1, "order_id": "a", "status": "paid", "amount": "7"},
                {"event_id": 2, "order_id": "a", "status": "paid", "amount": "9"},
            ]
        )["dedup_amount"]
        == "7"
    )
    tree = ast.parse((DATA.parents[1] / "src/mgar_v3/benchmark/executor.py").read_text())
    assert not any(
        isinstance(n, ast.ImportFrom) and n.module and "oracle" in n.module for n in ast.walk(tree)
    )


def test_three_real_strategy_protocols_share_caps_and_never_receive_oracle(engine, monkeypatch):
    engine.settings.allow_paid = True
    engine.settings.deepseek_api_key = SecretStr("controlled-final-model-key")
    frozen = []
    for arm in "ABC":
        p = LiveProfile(
            case_id="r6-eval:controlled:" + arm,
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
        frozen.append(
            {
                "case_id": "qa-controlled",
                "arm": arm,
                "profile": p.model_dump(mode="json"),
                "profile_hash": p.profile_hash,
            }
        )
    manifest = {
        "repository": engine.settings.repository,
        "max_new_calls": 9,
        "max_new_cost_micros": 55296,
        "prior_cost_micros": 0,
        "profiles": frozen,
        "ablation": "none",
    }
    grant = EvaluationAuthorization(
        manifest_hash=digest(manifest),
        approved_by="owner",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        max_new_calls=9,
        max_new_cost_micros=55296,
        allow_paid=True,
    )
    root = engine.benchmarks.authorize(manifest, grant)
    source = Source(id=str(uuid4()), role="knowledge", text="The cap is 4.", version="controlled")
    monkeypatch.setattr(
        "mgar_v3.benchmark.executor.collect",
        lambda *a, **k: ([source], [], {"tool_calls": 1, "recall_at5": 1}),
    )
    seen = []

    def response(request):
        body = json.loads(request.content)
        seen.append(body)
        encoded = json.dumps(body)
        assert "oracles.json" not in encoded and "TestR6Hidden" not in encoded
        reply = (
            {"notes": "The cap is 4.", "need_more": False}
            if "Extract facts from this role only" in body["messages"][0]["content"]
            else {
                "status": "supported",
                "values": {"limit": "4"},
                "citations": [{"source_id": source.id, "quote": "cap is 4"}],
                "explanation": "Controlled protocol, not semantic model evidence.",
                "diff": None,
            }
        )
        return httpx.Response(
            200,
            json={
                "model": "controlled",
                "usage": {"prompt_tokens": 100, "completion_tokens": 100},
                "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(reply)}}],
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(response))
    counts = []
    for entry in frozen:
        profile = LiveProfile.model_validate(entry["profile"])
        permit = LivePermit(
            profile,
            Authorization(
                profile_hash=profile.profile_hash,
                approved_by="owner",
                expires_at=grant.expires_at,
                allow_paid=True,
                allow_external_write=False,
            ),
            engine.settings,
        )
        policy = Policy(
            repository=engine.settings.repository,
            kinds=["pr_comment"],
            allowed_actors=["owner"],
            source_ref="a" * 40,
            question=profile.question,
            expires_at=grant.expires_at,
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
                external_event_id=entry["arm"],
                repository=engine.settings.repository,
                actor="owner",
                kind="pr_comment",
                issue_number=1,
                source_ref="a" * 40,
            ),
            permit,
        )[0]["run_id"]
        engine.benchmarks.bind(run, digest(manifest), "qa-controlled", entry["arm"])
        epoch = engine.begin_run(run)["execution_epoch"]
        executor = Executor(
            engine,
            permit,
            run,
            epoch,
            Case(
                id="qa-controlled",
                category="qa",
                question=profile.question,
                required_roles=["knowledge"],
                source_path="public.md",
            ),
            entry["arm"],
            {},
        )
        executor.model = StructuredModel(engine, permit, client)
        before = len(seen)
        answer, _, _ = executor.execute()
        counts.append(len(seen) - before)
        assert answer.values == {"limit": "4"}
    assert counts == [1, 3, 2]
    with engine.transaction() as conn:
        assert (
            conn.execute(
                "SELECT used_calls FROM app.benchmark_budgets WHERE budget_hash=%s", (root,)
            ).fetchone()["used_calls"]
            == 6
        )
        assert not conn.execute("SELECT 1 FROM app.outbox").fetchone()
    changed = grant.model_copy(update={"max_new_calls": 10})
    with pytest.raises(DomainError):
        engine.benchmarks.authorize(manifest, changed)
    with pytest.raises(DomainError):
        engine.benchmarks.require_ablation(run, "no_reranker")


BODIES = [
    "values:=make(map[string]int);for _,o:=range orders {if o.Sta"
    'tus=="paid"{values[o.ID]=o.Amount}};total:=0;for _,v:=range '
    "values{total+=v};return total",
    "values:=make(map[string]int);for _,o:=range orders {if o.Sta"
    'tus=="paid"{values[o.ID]=o.Amount}else{values[o.ID]=0}};tota'
    "l:=0;for _,v:=range values{total+=v};return total",
    "values:=make(map[string]int);for _,o:=range orders {if o.Sta"
    'tus=="paid"&&o.Amount>0{if _,ok:=values[o.ID];!ok{values[o.I'
    "D]=o.Amount}}};total:=0;for _,v:=range values{total+=v};retu"
    "rn total",
    "values:=make(map[string]int);for _,o:=range orders {if o.Sta"
    'tus=="paid"{values[o.ID]+=o.Amount}};total:=0;for _,v:=range'
    " values{if v>100{v=100};total+=v};return total",
    "values:=make(map[string]int);for _,o:=range orders {if o.Sta"
    'tus=="paid"{v,ok:=values[o.ID];if !ok||o.Amount>v{values[o.I'
    "D]=o.Amount}}};total:=0;for _,v:=range values{total+=v};retu"
    "rn total",
]


@pytest.mark.parametrize("variant", range(5))
def test_five_new_repair_oracles_in_real_isolated_go(variant):
    prefix = DATA / "public" / ("repair-" + str(variant))
    files = {p.name: p.read_text() for p in prefix.iterdir() if p.is_file()}
    before = files["orders.go"]
    after = (
        before[: before.index("func PaidRevenueCents")]
        + "func PaidRevenueCents(orders []Order) int {\n"
        + BODIES[variant]
        + "\n}\n"
    )
    snapshot = fixture_snapshot("example/mgar-test").model_copy(update={"files": files})
    patch = "".join(
        difflib.unified_diff(
            before.splitlines(True),
            after.splitlines(True),
            fromfile="a/orders.go",
            tofile="b/orders.go",
        )
    )
    candidate = apply_diff(snapshot, patch)
    proof, raw = Sandbox("r6-oracle-" + str(variant)).run(
        {**candidate, "zz_r6_hidden_test.go": hidden_suite(variant)},
        ["TestPaidRevenueCents", "TestR6Hidden"],
    )
    assert proof["passed"], raw.decode()
    assert len([name for name in proof["tests"] if name.startswith("TestR6Hidden/")]) == 28


def test_final_270_call_grant_uses_isolated_budget_and_preserves_legacy_limits(engine):
    engine.settings.allow_paid = True
    manifest = {
        "repository": engine.settings.repository,
        "profiles": [],
        "max_new_calls": 270,
        "max_new_cost_micros": 1658880,
        "prior_cost_micros": 0,
    }
    auth = EvaluationAuthorization(
        manifest_hash=digest(manifest),
        approved_by="owner",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        max_new_calls=270,
        max_new_cost_micros=1658880,
        allow_paid=True,
    )
    budget = engine.benchmarks.authorize(manifest, auth)
    with engine.transaction() as conn:
        row = conn.execute(
            "SELECT * FROM app.benchmark_budgets WHERE budget_hash=%s", (budget,)
        ).fetchone()
        assert row["call_limit"] == 270 and row["used_calls"] == 0
        assert not conn.execute("SELECT 1 FROM app.usage_calls").fetchone()
        assert not conn.execute("SELECT 1 FROM app.authorization_budgets").fetchone()
        constraint = conn.execute(
            "SELECT pg_get_constraintdef(oid) AS def FROM pg_constraint "
            "WHERE conname='authorization_budgets_call_limit_check'"
        ).fetchone()
        assert "100" in constraint["def"]
