import json
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError
from test_coordination_eval import controlled_response, setup_case

from mgar_v3.coordination.engine import Engine
from mgar_v3.coordination_eval.candidates import Selection, candidates, resolve
from mgar_v3.coordination_eval.domain import Event, Route
from mgar_v3.coordination_eval.metrics import grade
from mgar_v3.coordination_eval.provider import Slots
from mgar_v3.coordination_eval.runtime import Coordinator
from mgar_v3.coordination_eval.store import DATA, Journal
from mgar_v3.domain import DomainError, digest
from mgar_v3.evidence.artifacts import ArtifactStore


def events():
    return [
        Event.model_validate(e) for e in json.loads((DATA / "events.json").read_text())["events"]
    ]


@pytest.mark.parametrize(
    "body,action",
    [
        ("/mgar investigate a new unfamiliar question", "start"),
        ("/mgar investigate   ", "clarify"),
        ("@mgar help", "clarify"),
        ("unrelated message", "ignore"),
    ],
)
def test_policy_uses_public_context_not_event_keys(body, action):
    snapshot = {
        "events": [
            {
                "key": "unseen-" + uuid4().hex,
                "issue": 73,
                "actor": "owner",
                "source": "web",
                "body": body,
            }
        ],
        "primary_runs": [],
    }
    payload, bindings = candidates(snapshot)
    key = payload["events"][0]["key"]
    plan = resolve(Selection(choices=[{"event_key": key, "candidate_id": "c0"}]), bindings)
    assert plan.routes[0].action == action
    assert "run_id" not in json.dumps(payload)
    snapshot["primary_runs"] = [{"issue": 73, "run_id": str(uuid4())}]
    _, bindings = candidates(snapshot)
    assert bindings[key].action == "deliver"
    snapshot["primary_runs"] *= 2
    with pytest.raises(DomainError, match="One issue"):
        candidates(snapshot)


@pytest.mark.parametrize("arm,routers", [("E", 8), ("F", 3)])
@pytest.mark.parametrize("warm", [False, True])
def test_candidate_matrix_real_pg_worker(settings, tmp_path, arm, routers, warm):
    cfg, manifest, trial, auth = setup_case(settings, tmp_path, arm, warm=warm, repair=True)
    engine = Engine(cfg, ArtifactStore(cfg.artifact_root))
    try:
        engine.benchmarks.authorize(manifest, auth)
        journal = Journal(engine, trial, digest(manifest))
        with httpx.Client(transport=httpx.MockTransport(controlled_response)) as client:
            raw = Coordinator(journal, Slots(journal, manifest, auth, client)).execute()
        verdict = grade(raw, json.loads((DATA / "oracle.json").read_text()))
        assert (verdict["normal_passed"], verdict["negative_passed"], verdict["route_passed"]) == (
            2,
            3,
            9,
        )
        assert verdict["router_calls"] == routers and verdict["worker_calls"] == 2
        assert (
            verdict["forbidden_effects"]
            == verdict["unknown"]
            == verdict["lost_accepted_events"]
            == 0
        )
    finally:
        engine.close()


@pytest.mark.parametrize(
    "failure", ["schema", "candidate", "twice", "unknown0", "unknown1", "budget"]
)
def test_bounded_attempts_known_failure_and_unknown_stop(settings, tmp_path, failure):
    cfg, manifest, trial, auth = setup_case(settings, tmp_path, "F", repair=True)
    engine = Engine(cfg, ArtifactStore(cfg.artifact_root))
    sent = []

    def respond(request):
        sent.append(request)
        n = len(sent)
        if (failure == "unknown0" and n == 1) or (failure == "unknown1" and n == 2):
            raise httpx.ReadTimeout("controlled lost response")
        response = controlled_response(request)
        if n == 1 or failure == "twice":
            body = response.json()
            body["choices"][0]["message"]["content"] = (
                '{"routes":[]}'
                if failure == "schema"
                else '{"choices":[{"event_key":"e01","candidate_id":"wrong"}]}'
            )
            return httpx.Response(200, json=body)
        return response

    owner = None
    try:
        engine.benchmarks.authorize(manifest, auth)
        journal = Journal(engine, trial, digest(manifest))
        if failure == "budget":
            with engine.transaction() as conn:
                conn.execute("UPDATE app.benchmark_budgets SET used_calls=call_limit-1")
        with httpx.Client(transport=httpx.MockTransport(respond)) as client:
            owner = Coordinator(journal, Slots(journal, manifest, auth, client))
            if failure in {"schema", "candidate"}:
                plan, _ = owner.decision(events()[:2])
                assert [r.action for r in plan.routes] == ["start", "start"]
                assert len(sent) == 2
            else:
                with pytest.raises(DomainError) as err:
                    owner.decision(events()[:2])
                assert err.value.code == (
                    "COORD_SELECTION_INVALID"
                    if failure == "twice"
                    else "BUDGET_EXHAUSTED"
                    if failure == "budget"
                    else "USAGE_UNKNOWN"
                )
                assert len(sent) == (1 if failure in {"unknown0", "budget"} else 2)
            raw = journal.export()
            assert not any(e["action"] for e in raw["events"])
            assert sum(a["kind"] == "candidate_attempt_rejected" for a in raw["audit"]) == (
                0 if failure == "unknown0" else 2 if failure == "twice" else 1
            )
            if failure.startswith("unknown"):
                with pytest.raises(DomainError) as err:
                    Coordinator(
                        journal, Slots(journal, manifest, auth, client), resume=True
                    ).decision(events()[:2])
                assert err.value.code == "USAGE_UNKNOWN"
                assert len(sent) == (1 if failure == "unknown0" else 2)
    finally:
        if owner:
            owner.pool.shutdown()
        engine.close()


def test_revision_actor_and_rollback_guards(settings, tmp_path, monkeypatch):
    cfg, manifest, trial, auth = setup_case(settings, tmp_path, "F", repair=True)
    engine = Engine(cfg, ArtifactStore(cfg.artifact_root))
    try:
        engine.benchmarks.authorize(manifest, auth)
        journal = Journal(engine, trial, digest(manifest))
        slots = Slots(journal, manifest, auth)
        first, follow = events()[0], events()[2]
        journal.accept(first, 0)
        journal.accept(follow, 2)
        permit = slots.permit("worker:e01")
        run_id = journal.apply(
            first,
            Route(event_key=first.key, action="start"),
            permit,
            digest(manifest),
            slots.item("worker:e01")["case_id"],
        )
        target = journal.target(follow)
        with engine.transaction() as conn:
            conn.execute(
                "UPDATE app.threads SET revision=revision+1 WHERE thread_id=%s",
                (target["thread_id"],),
            )
        route = Route(event_key=follow.key, action="deliver", target_run_id=run_id)
        with pytest.raises(DomainError) as err:
            journal.apply(follow, route, permit, digest(manifest), None, target)
        assert err.value.code == "COORD_TARGET_STALE"
        target = journal.target(follow)
        cfg.allowed_actors = []
        with pytest.raises(DomainError) as err:
            journal.apply(follow, route, permit, digest(manifest), None, target)
        assert err.value.code == "ACTOR_DENIED"
        cfg.allowed_actors = [cfg.local_actor]
        original = journal.finish

        def fail_finish(*args, **kwargs):
            raise RuntimeError("controlled post-ingest failure")

        monkeypatch.setattr(journal, "finish", fail_finish)
        with pytest.raises(RuntimeError):
            journal.apply(follow, route, permit, digest(manifest), None, target)
        assert journal.target(follow)["revision"] == target["revision"]
        assert len(journal.export()["mailbox"]) == 1
        monkeypatch.setattr(journal, "finish", original)
        journal.apply(follow, route, permit, digest(manifest), None, target)
        assert len(journal.export()["mailbox"]) == 2
    finally:
        engine.close()


def test_old_bad_plans_cannot_satisfy_new_contract():
    # These are historical wire shapes, never converted into successful new choices.
    bad = [
        {"routes": [{"event_key": "e01", "action": "deliver", "target_run_id": None}]},
        {"choices": [{"event_key": "e01", "candidate_id": "c0", "target_run_id": str(uuid4())}]},
        {"choices": [{"event_key": "e01", "candidate_id": "c0", "action": "start"}]},
    ]
    for value in bad:
        with pytest.raises(ValidationError):
            Selection.model_validate(value)


def test_all_historical_bad_initial_proposals_are_rejected():
    from mgar_v3.settings import ROOT

    rejected = 0
    for path in (ROOT / "docs/experiments/coordination-v1/trials").glob("*.json"):
        raw = json.loads(path.read_text())
        if raw["trial"]["arm"] == "D":
            continue
        for batch in raw["batches"]:
            plan = batch.get("plan")
            if not plan:
                continue
            wrong = [
                r
                for r in plan["routes"]
                if r["event_key"] in {"e01", "e02"}
                and (r["action"] != "start" or r["target_run_id"] is not None)
            ]
            for route in wrong:
                with pytest.raises(ValidationError):
                    Selection.model_validate({"routes": [route]})
                rejected += 1
    assert rejected == 11
