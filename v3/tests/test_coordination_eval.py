import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr

from mgar_v3.benchmark.domain import EvaluationAuthorization
from mgar_v3.coordination.engine import Engine
from mgar_v3.coordination_eval.domain import Event, Route
from mgar_v3.coordination_eval.experiment import manifest_for
from mgar_v3.coordination_eval.metrics import grade, summarize
from mgar_v3.coordination_eval.provider import Slots
from mgar_v3.coordination_eval.runtime import Coordinator
from mgar_v3.coordination_eval.store import DATA, Journal
from mgar_v3.domain import DomainError, digest
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.settings import ROOT


def controlled_response(request):
    body = json.loads(request.content)
    payload = json.loads(body["messages"][1]["content"])
    if "events" in payload:
        routes = []
        for event in payload["events"]:
            target = next(
                (r for r in payload["primary_runs"] if r["issue"] == event["issue"]), None
            )
            action = (
                "deliver"
                if target
                else "start"
                if event["body"].startswith("/mgar investigate ")
                else "clarify"
                if event["body"].startswith("@mgar ")
                else "ignore"
            )
            routes.append(
                {
                    "event_key": event["key"],
                    "action": action,
                    "target_run_id": target["run_id"] if target else None,
                }
            )
        result = {"routes": routes}
    else:
        word = "cache TTL" if "cache TTL" in payload["question"] else "retry limit"
        quote = next(
            line
            for line in payload["source"].splitlines()
            if line.startswith("Observation:") and word in line
        )
        result = {
            "claims": [{"statement": quote, "quote": quote, "evidence_id": payload["evidence_id"]}],
            "open_questions": [],
        }
    return httpx.Response(
        200,
        json={
            "model": body["model"],
            "choices": [{"message": {"content": json.dumps(result)}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 20,
                "prompt_cache_hit_tokens": 0,
                "prompt_cache_miss_tokens": 10,
            },
        },
    )


def setup_case(settings, tmp_path, arm, warm=False, fault=False, namespace=None):
    cfg = settings.model_copy(
        update={
            "allow_paid": True,
            "allow_external_write": False,
            "graph_version": "r2-mailbox-v1",
            "deepseek_api_key": SecretStr("controlled-no-provider-request"),
        }
    )
    manifest = manifest_for(
        namespace or uuid4().hex, "a" * 40, cfg, {"calls": 0, "cost_micros": 0, "unknown": 0}
    )
    trial = next(
        t
        for t in manifest["trials"]
        if t["arm"] == arm and t["repetition"] == 0 and t["cache"] == ("warm" if warm else "cold")
    )
    trial["fault_after_known"] = fault
    cfg.project_id = trial["project_id"]
    cfg.artifact_root = tmp_path / "artifacts"
    auth = EvaluationAuthorization(
        manifest_hash=digest(manifest),
        approved_by=cfg.local_actor,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        allow_paid=True,
        max_new_calls=68,
        max_new_cost_micros=208896,
    )
    return cfg, manifest, trial, auth


@pytest.mark.parametrize("arm,router_calls", [("D", 0), ("E", 8), ("F", 3)])
@pytest.mark.parametrize("warm", [False, True])
def test_real_engine_fixed_worker_and_independent_grade(
    settings, tmp_path, arm, router_calls, warm
):
    cfg, manifest, trial, auth = setup_case(settings, tmp_path, arm, warm)
    engine = Engine(cfg, ArtifactStore(cfg.artifact_root))
    try:
        engine.benchmarks.authorize(manifest, auth)
        journal = Journal(engine, trial, digest(manifest))
        with httpx.Client(transport=httpx.MockTransport(controlled_response)) as client:
            raw = Coordinator(journal, Slots(journal, manifest, auth, client)).execute()
        verdict = grade(raw, json.loads((DATA / "oracle.json").read_text()))
        assert verdict["normal_passed"] == 2 and verdict["negative_passed"] == 3
        assert verdict["router_calls"] == router_calls and verdict["worker_calls"] == 2
        assert verdict["route_passed"] == 9 and verdict["physical_deliveries"] == 10
        assert verdict["duplicates"] == 1 and verdict["lost_accepted_events"] == 0
        assert verdict["forbidden_effects"] == 0 and verdict["unknown"] == 0
        assert verdict["citation_valid"] == verdict["citation_total"] == 2
        assert all(r["all_user_input_consumed"] for r in verdict["normal"])
        if warm:
            assert all(o["hit"] for o in verdict["cache_observations"])
        else:
            assert any(not o["hit"] for o in verdict["cache_observations"])
        if arm == "F":
            assert verdict["batch_sizes"] == [2, 1, 1]
            assert sum(a["kind"] == "subscription_direct" for a in raw["audit"]) == 4
        if arm == "E":
            assert verdict["batch_sizes"] == [1] * 8
        totals = summarize([verdict])[arm]
        assert totals["normal_total"] == 2 and totals["router_calls"] == router_calls
        with engine.transaction() as conn:
            # DEF must never be relabelled as ABC to bypass the old arm constraint.
            assert conn.execute("SELECT count(*) AS n FROM app.benchmark_runs").fetchone()["n"] == 0
            budget = conn.execute("SELECT * FROM app.benchmark_budgets").fetchone()
            assert budget["used_calls"] == router_calls + 2 and budget["reserved_micros"] == 0
        first_call = next(c for c in raw["calls"] if c.get("result", {}).get("claims"))
        first_call["result"]["claims"][0]["evidence_id"] = str(uuid4())
        corrupted = grade(raw, json.loads((DATA / "oracle.json").read_text()))
        assert corrupted["normal_passed"] == 1 and corrupted["citation_valid"] == 1
    finally:
        engine.close()


def test_unknown_response_retains_hold_and_never_sends_again(settings, tmp_path):
    cfg, manifest, trial, auth = setup_case(settings, tmp_path, "F")
    engine = Engine(cfg, ArtifactStore(cfg.artifact_root))
    sent = []

    def lose(request):
        sent.append(request)
        raise httpx.ReadTimeout("controlled response loss")

    try:
        engine.benchmarks.authorize(manifest, auth)
        journal = Journal(engine, trial, digest(manifest))
        with httpx.Client(transport=httpx.MockTransport(lose)) as client:
            owner = Coordinator(journal, Slots(journal, manifest, auth, client))
            with pytest.raises(DomainError) as error:
                owner.execute()
            assert error.value.code == "USAGE_UNKNOWN" and len(sent) == 1
            rows = journal.export()["calls"]
            assert len(rows) == 1 and rows[0]["status"] == "unknown"
            assert rows[0]["reserved_micros"] == 3072 and rows[0]["settled_micros"] is None
            retry = Coordinator(journal, Slots(journal, manifest, auth, client), resume=True)
            with pytest.raises(DomainError) as error:
                retry.execute()
            assert error.value.code == "USAGE_UNKNOWN" and len(sent) == 1
        with engine.transaction() as conn:
            budget = conn.execute("SELECT * FROM app.benchmark_budgets").fetchone()
            assert budget["used_calls"] == 1 and budget["reserved_micros"] == 3072
    finally:
        engine.close()


def test_stale_and_cross_issue_route_cannot_commit(settings, tmp_path):
    cfg, manifest, trial, auth = setup_case(settings, tmp_path, "F")
    engine = Engine(cfg, ArtifactStore(cfg.artifact_root))
    try:
        engine.benchmarks.authorize(manifest, auth)
        journal = Journal(engine, trial, digest(manifest))
        slots = Slots(journal, manifest, auth)
        events = [
            Event.model_validate(e)
            for e in json.loads((DATA / "events.json").read_text())["events"]
        ]
        first = events[0]
        journal.accept(first, 0)
        run_id = journal.apply(
            first,
            Route(event_key=first.key, action="start"),
            slots.permit("worker:e01"),
            digest(manifest),
            slots.item("worker:e01")["case_id"],
        )
        assert run_id is not None
        current = journal.target(first)
        engine.pause(run_id)
        with pytest.raises(DomainError) as error:
            journal.apply(
                events[2],
                Route(event_key="e03", action="deliver", target_run_id=run_id),
                slots.permit("worker:e01"),
                digest(manifest),
                None,
                current,
            )
        assert error.value.code == "COORD_TARGET_STALE"
        with pytest.raises(DomainError) as error:
            journal.apply(
                events[3],
                Route(event_key="e04", action="deliver", target_run_id=run_id),
                slots.permit("worker:e02"),
                digest(manifest),
                None,
            )
        assert error.value.code == "COORD_TARGET_DENIED"
        assert len([m for m in journal.export()["mailbox"] if m["kind"] == "user"]) == 1
    finally:
        engine.close()


def test_shared_budget_and_mismatched_binding_fail_before_http(settings, tmp_path):
    cfg, manifest, trial, auth = setup_case(settings, tmp_path, "F")
    engine = Engine(cfg, ArtifactStore(cfg.artifact_root))
    try:
        engine.benchmarks.authorize(manifest, auth)
        journal = Journal(engine, trial, digest(manifest))
        slots = Slots(journal, manifest, auth)
        run_id = slots.control_run("router:0")
        with pytest.raises(DomainError) as error:
            engine.benchmarks.bind(run_id, digest(manifest), slots.item("router:0")["case_id"], "E")
        assert error.value.code == "COORD_SCOPE_CHANGED"
        with engine.transaction() as conn:
            conn.execute("UPDATE app.benchmark_budgets SET used_calls=call_limit")

        def unexpected(request):
            raise AssertionError("Budget exhaustion must precede any HTTP request")

        events = [
            Event.model_validate(e)
            for e in json.loads((DATA / "events.json").read_text())["events"]
        ]
        with httpx.Client(transport=httpx.MockTransport(unexpected)) as client:
            owner = Coordinator(journal, Slots(journal, manifest, auth, client))
            try:
                with pytest.raises(DomainError) as error:
                    owner.decision(events[:2])
                assert error.value.code == "BUDGET_EXHAUSTED"
                assert journal.export()["calls"] == []
            finally:
                owner.pool.shutdown()
    finally:
        engine.close()


def test_incomplete_report_keeps_full_planned_denominators():
    result = summarize([], planned=True)
    assert all(v["normal_total"] == 8 and v["negative_total"] == 12 for v in result.values())
    assert all(v["not_run_trials"] == 4 and v["normal_passed"] == 0 for v in result.values())


@pytest.mark.parametrize("days", [-1, 8])
def test_future_or_expired_price_rejected_without_model_call(monkeypatch, tmp_path, days):
    from mgar_v3.coordination_eval import experiment

    price = json.loads(experiment.PRICE_PATH.read_text())
    price["checked_at"] = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    path = tmp_path / "price.json"
    path.write_text(json.dumps(price))
    monkeypatch.setattr(experiment, "PRICE_PATH", path)
    with pytest.raises(DomainError) as error:
        experiment.reviewed_price()
    assert error.value.code == "COORD_PRICE_REVIEW_REQUIRED"


def test_manifest_and_original_business_outcomes_are_sealed(settings, tmp_path, monkeypatch):
    from mgar_v3.coordination_eval import experiment

    _, manifest, _, _ = setup_case(settings, tmp_path, "D")
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    assert experiment.verify(tmp_path)["max_new_cost_micros"] == 208896
    monkeypatch.setattr(experiment, "hashes", lambda: {"changed": "source"})
    with pytest.raises(DomainError) as error:
        experiment.verify(tmp_path)
    assert error.value.code == "COORD_FROZEN_SOURCE_CHANGED"


def test_real_sigkill_reuses_known_response_and_fences_old_epoch(settings, tmp_path):
    cfg, manifest, trial, auth = setup_case(settings, tmp_path, "F", fault=True)
    manifest_path = tmp_path / "manifest.json"
    auth_path = tmp_path / "authorization.json"
    manifest_path.write_text(json.dumps(manifest))
    auth_path.write_text(auth.model_dump_json())
    script = tmp_path / "crash.py"
    script.write_text("""
import json,os,runpy
from pathlib import Path
from pydantic import SecretStr
from mgar_v3.settings import Settings
from mgar_v3.coordination.engine import Engine
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.coordination_eval.runtime import Coordinator
from mgar_v3.coordination_eval.store import Journal
from mgar_v3.coordination_eval.provider import Slots
from mgar_v3.benchmark.domain import EvaluationAuthorization
from mgar_v3.domain import digest
import httpx
p=Path(os.environ['COORD_TEST_DIR'])
m=json.loads((p/'manifest.json').read_text())
t=next(x for x in m['trials'] if x['fault_after_known'])
c=Settings(database_url=os.environ['COORD_TEST_DSN'],repository='example/mgar-test',local_actor='owner',allowed_actors=['owner'],project_id=t['project_id'],artifact_root=p/'artifacts',graph_version='r2-mailbox-v1',allow_paid=True,allow_external_write=False,deepseek_api_key=SecretStr('controlled'))
a=EvaluationAuthorization.model_validate_json((p/'authorization.json').read_text());e=Engine(c,ArtifactStore(c.artifact_root));e.benchmarks.authorize(m,a)
h=runpy.run_path('tests/test_coordination_eval.py')['controlled_response']
with httpx.Client(transport=httpx.MockTransport(h)) as client:
 j=Journal(e,t,digest(m));Coordinator(j,Slots(j,m,a,client)).execute()
""")
    with (tmp_path / "crash.log").open("wb") as log:
        process = subprocess.Popen(
            [sys.executable, str(script)],
            cwd=ROOT,
            stdout=log,
            stderr=subprocess.STDOUT,
            env={
                **os.environ,
                "COORD_TEST_DIR": str(tmp_path),
                "COORD_TEST_DSN": cfg.database_url.get_secret_value(),
            },
        )
        assert process.wait(timeout=30) == -9, (tmp_path / "crash.log").read_text()
    engine = Engine(cfg, ArtifactStore(cfg.artifact_root))
    try:
        journal = Journal(engine, trial, digest(manifest))
        before = journal.export()["calls"]
        assert len(before) == 1 and before[0]["status"] == "known"
        with httpx.Client(transport=httpx.MockTransport(controlled_response)) as client:
            raw = Coordinator(
                journal, Slots(journal, manifest, auth, client), resume=True
            ).execute()
        assert len(raw["calls"]) == 5
        assert len([a for a in raw["audit"] if a["kind"] == "known_router_reused"]) == 1
        assert len([a for a in raw["audit"] if a["kind"] == "old_epoch_fenced"]) == 1
        verdict = grade(raw, json.loads((DATA / "oracle.json").read_text()))
        assert verdict["normal_passed"] == 2 and verdict["router_calls"] == 3
        assert verdict["duplicates"] == 1 and verdict["physical_deliveries"] == 10
    finally:
        engine.close()
