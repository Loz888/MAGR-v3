import os
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from router_helpers import setup_router, success_response
from test_routing import cold
from test_routing import routed as routed

from mgar_v3.coordination.engine import Engine
from mgar_v3.domain import DomainError
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.live import Authorization, LivePermit
from mgar_v3.routing.model import DeepSeekRouter
from mgar_v3.routing.worker import RouterWorker


def test_router_reuses_known_result_and_records_response_artifact(routed, trigger):
    root, permit = setup_router(routed)
    cold(routed, trigger)
    batch = routed.routing.prepare("live")
    calls = []

    def respond(request):
        calls.append(request)
        return success_response(request)

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        model = DeepSeekRouter(routed, permit, client)
        first = model.propose(batch)
        assert model.propose(batch) == first and len(calls) == 1
    with routed.transaction() as conn:
        budget = conn.execute("SELECT * FROM app.authorization_budgets").fetchone()
        assert (
            budget["used_calls"] == 1
            and budget["settled_micros"] == 120
            and budget["reserved_micros"] == 0
        )
        call = conn.execute("SELECT * FROM app.router_calls").fetchone()
        assert call["response_hash"] is not None
        assert b"prompt_tokens" in routed.artifacts.read(call["response_hash"])


def test_router_and_worker_share_the_original_call_and_cost_budget(routed, trigger):
    root, permit = setup_router(routed)
    auth = Authorization(
        profile_hash=root.profile_hash,
        approved_by="owner",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        allow_paid=True,
        allow_external_write=False,
    )
    worker_permit = LivePermit(root, auth, routed.settings)
    live_event = trigger.model_copy(
        update={"execution_mode": "live", "body": "/mgar investigate " + root.question}
    )
    receipt = routed.ingest(live_event, worker_permit)
    run = routed.begin_run(receipt.run_id)
    for step in ["first", "second"]:
        call = routed.reserve_call(receipt.run_id, run["execution_epoch"], step, worker_permit)
        routed.settle_call(call, 1)
    routed.cancel(receipt.run_id)
    cold(routed, trigger, issue=2)
    with httpx.Client(transport=httpx.MockTransport(success_response)) as client:
        assert RouterWorker(routed, DeepSeekRouter(routed, permit, client)).tick() is not None
    receipt = routed.ingest(
        live_event.model_copy(update={"external_event_id": "fourth-attempt"}), worker_permit
    )
    run = routed.begin_run(receipt.run_id)
    with pytest.raises(DomainError) as error:
        routed.reserve_call(receipt.run_id, run["execution_epoch"], "fourth", worker_permit)
    assert error.value.code == "BUDGET_EXHAUSTED"
    with routed.transaction() as conn:
        budget = conn.execute("SELECT * FROM app.authorization_budgets").fetchone()
        assert budget["used_calls"] == budget["call_limit"] == 3
        assert budget["settled_micros"] == 122


@pytest.mark.parametrize("failure", ["unknown", "invalid"])
def test_failed_router_call_is_not_automatically_reissued(routed, trigger, failure):
    root, permit = setup_router(routed)
    cold(routed, trigger)
    calls = []

    def respond(request):
        calls.append(request)
        if failure == "unknown":
            raise httpx.ReadTimeout("controlled timeout")
        return httpx.Response(
            200, json={"usage": {"prompt_tokens": 10, "completion_tokens": 3}, "choices": []}
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        assert RouterWorker(routed, DeepSeekRouter(routed, permit, client)).tick() is not None
        routed.recover()
        with routed.transaction() as conn:
            conn.execute("UPDATE app.router_inbox SET next_attempt_at=now()")
        assert RouterWorker(routed, DeepSeekRouter(routed, permit, client)).tick() is None
    assert len(calls) == 1 and routed.routing.metrics()["pending_events"] == 1
    with routed.transaction() as conn:
        call = conn.execute("SELECT * FROM app.router_calls").fetchone()
        assert call["status"] == ("unknown" if failure == "unknown" else "known")
        budget = conn.execute("SELECT * FROM app.authorization_budgets").fetchone()
        assert budget["used_calls"] == 1
        assert budget["reserved_micros"] == (
            permit.profile.reservation_micros if failure == "unknown" else 0
        )


def test_expired_ready_decision_can_rebase_known_proposals_without_new_charge(routed, trigger):
    routed.settings = routed.settings.model_copy(update={"router_decision_ttl_seconds": 2})
    root, permit = setup_router(routed)
    cold(routed, trigger)
    batch = routed.routing.prepare("live")
    calls = []

    def respond(request):
        calls.append(request)
        return success_response(request)

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        proposals = DeepSeekRouter(routed, permit, client).propose(batch)
        routed.routing.save(batch["batch_id"], routed.routing.wrap(batch, proposals))
        time.sleep(2.1)
        next_batch = routed.routing.prepare("live")
        assert next_batch["batch_id"] != batch["batch_id"] and next_batch["status"] == "ready"
        worker = RouterWorker(routed, DeepSeekRouter(routed, permit, client))
        assert worker.tick() == next_batch["batch_id"] and len(calls) == 1
    assert len(routed.list_runs()) == 1


@pytest.mark.parametrize(
    "phase", ["batch_prepared", "model_result_settled", "decision_saved", "decision_applied"]
)
def test_sigkill_router_handoffs_preserve_input_and_do_not_pay_again(settings, trigger, phase):
    settings = settings.model_copy(update={"graph_version": "r2-mailbox-v1"})
    event = trigger.model_copy(update={"body": "@mgar Investigate documented symptoms"})
    environment = {
        **os.environ,
        "MGAR_V3_DATABASE_URL": settings.database_url.get_secret_value(),
        "MGAR_V3_ARTIFACT_ROOT": str(settings.artifact_root),
        "MGAR_V3_REPOSITORY": settings.repository,
        "MGAR_V3_LOCAL_ACTOR": settings.local_actor,
        "MGAR_V3_ALLOWED_ACTORS": '["owner"]',
        "MGAR_V3_GRAPH_VERSION": "r2-mailbox-v1",
        "MGAR_TEST_EVENT": event.model_dump_json(),
    }
    process = subprocess.run(
        [sys.executable, str(Path(__file__).parent / "fixtures/router_crash_worker.py"), phase],
        env=environment,
        capture_output=True,
        timeout=20,
    )
    assert process.returncode == -9, process.stderr.decode()
    engine = Engine(settings, ArtifactStore(settings.artifact_root))
    try:
        engine.recover()
        root, permit = setup_router(engine)
        assert engine.ingest(event).duplicate
        calls = []

        def respond(request):
            calls.append(request)
            assert phase == "batch_prepared", "Known result must survive a process restart"
            return success_response(request)

        with httpx.Client(transport=httpx.MockTransport(respond)) as client:
            RouterWorker(engine, DeepSeekRouter(engine, permit, client)).tick()
        assert len(engine.list_runs()) == 1
        with engine.transaction() as conn:
            ledger = conn.execute("SELECT * FROM app.router_calls").fetchall()
            assert len(ledger) == 1 and ledger[0]["status"] == "known"
            assert conn.execute("SELECT count(*) AS n FROM app.router_inbox").fetchone()["n"] == 0
    finally:
        engine.close()


def test_router_authorization_mismatch_fails_before_network(routed, trigger):
    root, permit = setup_router(routed)
    cold(routed, trigger)
    batch = routed.routing.prepare("live")
    permit.authorization = permit.authorization.model_copy(update={"profile_hash": "0" * 64})
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: pytest.fail("no call authorized"))
    ) as client:
        with pytest.raises(DomainError) as error:
            DeepSeekRouter(routed, permit, client).propose(batch)
    assert error.value.code == "ROUTER_AUTHORIZATION_INVALID"
    assert routed.routing.metrics()["physical_router_calls"] == 0
