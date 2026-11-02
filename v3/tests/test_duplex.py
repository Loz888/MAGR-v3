import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from uuid import UUID

import pytest

from mgar_v3.coordination.engine import Engine
from mgar_v3.domain import DomainError
from mgar_v3.effects.dispatcher import Dispatcher
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.graph.runner import FixtureInvestigator, Runner


@pytest.fixture
def r2_settings(settings):
    return settings.model_copy(update={"graph_version": "r2-mailbox-v1"})


@pytest.fixture
def r2(r2_settings):
    service = Engine(r2_settings, ArtifactStore(r2_settings.artifact_root))
    yield service
    service.close()


def test_followup_during_investigation_replans_at_boundary_without_new_ticket(r2, trigger):
    receipt = r2.ingest(trigger)
    questions = []

    class Investigator(FixtureInvestigator):
        def investigate(self, question, content, evidence_id):
            questions.append(question)
            if len(questions) == 1:
                incoming = r2.follow_up(receipt.run_id, "followup-1", "Also list missing evidence")
                assert incoming.route == "direct" and incoming.ticket_id == receipt.ticket_id
                assert r2.follow_up(
                    receipt.run_id, "followup-1", "Also list missing evidence"
                ).duplicate
            return super().investigate(question, content, evidence_id)

    action = Runner(r2, Investigator()).execute(receipt.run_id)
    assert action is not None and len(questions) == 2
    assert "Also list missing evidence" in questions[-1]
    run = r2.get_run(receipt.run_id)
    assert run["input_revision"] == 2 and run["thread_revision"] == 2
    assert run["question"] == trigger.body.removeprefix("/mgar investigate ")
    assert len(r2.list_runs()) == 1
    with r2.transaction() as conn:
        messages = conn.execute("SELECT * FROM app.mailbox").fetchall()
        assert len(messages) == 3
        assert all(m["delivered_at"] and m["read_at"] and m["consumed_at"] for m in messages)
        assert conn.execute("SELECT count(*) AS n FROM app.tickets").fetchone()["n"] == 1
        assert conn.execute("SELECT count(*) AS n FROM app.router_inbox").fetchone()["n"] == 0
        assert conn.execute("SELECT count(*) AS n FROM app.read_jobs").fetchone()["n"] == 1


def test_followup_invalidates_approval_and_only_current_result_can_publish(r2, trigger):
    from test_effects import FakeRemote

    receipt = r2.ingest(trigger)
    first = Runner(r2).execute(receipt.run_id)
    old = r2.actions(receipt.run_id)[0]
    r2.approve(first, old["payload_hash"], "owner")
    r2.follow_up(receipt.run_id, "new-question", "State the remaining uncertainties")
    assert r2.claim_effect("fixture") is None
    with pytest.raises(DomainError):
        r2.approve(first, old["payload_hash"], "owner")
    second = Runner(r2).execute(receipt.run_id)
    assert second != first
    latest = r2.actions(receipt.run_id)[-1]
    remote = FakeRemote()
    dispatcher = Dispatcher(r2, remote)
    assert dispatcher.tick() is None
    r2.approve(second, latest["payload_hash"], "owner")
    assert dispatcher.tick() == second
    assert dispatcher.tick() is None and len(remote.posts) == 1


@pytest.mark.parametrize("control", ["pause", "cancel"])
def test_long_read_allows_control_and_rejects_late_completion(r2, trigger, control):
    receipt = r2.ingest(trigger)
    started, release = Event(), Event()

    class SlowReader(FixtureInvestigator):
        def read(self):
            started.set()
            assert release.wait(5)
            return super().read()

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(Runner(r2, SlowReader()).execute, receipt.run_id)
        try:
            assert started.wait(3)
            getattr(r2, control)(receipt.run_id)
            assert not future.done()
        finally:
            release.set()
        with pytest.raises(DomainError) as error:
            future.result(timeout=5)
        assert error.value.code == "FENCED"
    assert r2.get_run(receipt.run_id)["status"] == ("paused" if control == "pause" else "cancelled")
    assert not [e for e in r2.events(receipt.run_id) if e["kind"] == "tool_completed"]
    if control == "pause":
        assert r2.resume(receipt.run_id) == receipt.run_id
        assert Runner(r2).execute(receipt.run_id) is not None
    else:
        with pytest.raises(DomainError):
            r2.follow_up(receipt.run_id, "late-input", "Please continue")


def test_pause_approval_requires_new_proposal_and_preserves_same_run(r2, trigger):
    receipt = r2.ingest(trigger)
    first = Runner(r2).execute(receipt.run_id)
    r2.pause(receipt.run_id)
    epoch = r2.get_run(receipt.run_id)["execution_epoch"]
    r2.pause(receipt.run_id)
    assert r2.get_run(receipt.run_id)["execution_epoch"] == epoch
    assert r2.resume(receipt.run_id) == receipt.run_id
    second = Runner(r2).execute(receipt.run_id)
    assert second is not None and second != first
    assert [a["status"] for a in r2.actions(receipt.run_id)] == ["expired", "proposed"]


def test_followup_during_unknown_write_waits_for_reconciliation(r2, trigger):
    from test_effects import FakeRemote

    receipt = r2.ingest(trigger)
    first = Runner(r2).execute(receipt.run_id)
    action = r2.actions(receipt.run_id)[0]
    r2.approve(first, action["payload_hash"], "owner")
    claimed = r2.claim_effect("fixture")
    r2.follow_up(receipt.run_id, "followup-inflight", "Explain the evidence limits")
    assert r2.get_run(receipt.run_id)["status"] == "waiting_approval"
    r2.effect_result(first, claimed["lease_token"], unknown=True)
    remote = FakeRemote()
    dispatcher = Dispatcher(r2, remote)
    assert dispatcher.tick() is None and not dispatcher.reconcile(first)
    r2.pause(receipt.run_id)
    with pytest.raises(DomainError, match="Unresolved"):
        r2.resume(receipt.run_id)
    assert r2.reconcile_effect(first, "confirmed-existing-comment")
    assert r2.get_run(receipt.run_id)["status"] == "paused"
    assert r2.resume(receipt.run_id) == receipt.run_id
    second = Runner(r2).execute(receipt.run_id)
    assert second is not None and second != first
    assert dispatcher.tick() is None and remote.posts == []


def test_known_effect_with_followup_queues_continuation_without_losing_input(r2, trigger):
    receipt = r2.ingest(trigger)
    first = Runner(r2).execute(receipt.run_id)
    old = r2.actions(receipt.run_id)[0]
    r2.approve(first, old["payload_hash"], "owner")
    claimed = r2.claim_effect("fixture")
    r2.follow_up(receipt.run_id, "during-send", "What else is unknown?")
    r2.effect_result(first, claimed["lease_token"], external_id="remote-1")
    assert r2.get_run(receipt.run_id)["status"] == "queued"
    assert Runner(r2).execute(receipt.run_id) != first
    assert r2.get_run(receipt.run_id)["input_revision"] == 2


def test_pause_does_not_resurrect_after_remote_receipt(r2, trigger):
    receipt = r2.ingest(trigger)
    action_id = Runner(r2).execute(receipt.run_id)
    action = r2.actions(receipt.run_id)[0]
    r2.approve(action_id, action["payload_hash"], "owner")
    claimed = r2.claim_effect("fixture")
    r2.pause(receipt.run_id)
    r2.effect_result(action_id, claimed["lease_token"], external_id="remote-1")
    assert r2.get_run(receipt.run_id)["status"] == "paused"
    r2.resume(receipt.run_id)
    assert r2.get_run(receipt.run_id)["status"] == "succeeded"
    assert len(r2.actions(receipt.run_id)) == 1


def test_prepared_input_and_consumption_replay_use_business_snapshot(r2, trigger):
    receipt = r2.ingest(trigger)
    run = r2.begin_run(receipt.run_id)
    first = r2.prepare_intake(receipt.run_id, run["execution_epoch"])
    assert r2.get_run(receipt.run_id)["mailbox_cursor"] == 0
    r2.acknowledge_intake(receipt.run_id, run["execution_epoch"], first["snapshot_id"])
    r2.follow_up(receipt.run_id, "next", "Additional data")
    second = r2.prepare_intake(receipt.run_id, run["execution_epoch"])
    r2.acknowledge_intake(receipt.run_id, run["execution_epoch"], second["snapshot_id"])
    replay = r2.acknowledge_intake(receipt.run_id, run["execution_epoch"], first["snapshot_id"])
    assert replay["snapshot_id"] == second["snapshot_id"]
    assert r2.get_run(receipt.run_id)["mailbox_cursor"] == 2
    assert len([e for e in r2.events(receipt.run_id) if e["kind"] == "input_consumed"]) == 2


@pytest.mark.parametrize(
    "phase",
    [
        "checkpoint_before_cursor",
        "cursor_before_checkpoint",
        "tool_running",
        "tool_completion_before_checkpoint",
    ],
)
def test_sigkill_mailbox_and_read_job_recovery(r2_settings, trigger, phase):
    environment = {
        **os.environ,
        "MGAR_V3_DATABASE_URL": r2_settings.database_url.get_secret_value(),
        "MGAR_V3_ARTIFACT_ROOT": str(r2_settings.artifact_root),
        "MGAR_V3_REPOSITORY": r2_settings.repository,
        "MGAR_V3_LOCAL_ACTOR": r2_settings.local_actor,
        "MGAR_V3_ALLOWED_ACTORS": '["owner"]',
        "MGAR_V3_GRAPH_VERSION": "r2-mailbox-v1",
        "MGAR_TEST_EVENT": trigger.model_dump_json(),
    }
    process = subprocess.run(
        [sys.executable, str(Path(__file__).parent / "fixtures/r2_crash_worker.py"), phase],
        env=environment,
        capture_output=True,
        timeout=20,
    )
    assert process.returncode == -9, process.stderr.decode()
    engine = Engine(r2_settings, ArtifactStore(r2_settings.artifact_root))
    try:
        recovery = engine.recover()
        assert recovery["paused_runs"] == 1
        receipt = engine.ingest(trigger)
        assert receipt.duplicate
        assert engine.resume(receipt.run_id) == receipt.run_id
        action_id = Runner(engine).execute(receipt.run_id)
        assert action_id is not None and len(engine.list_runs()) == 1
        with engine.transaction() as conn:
            rows = conn.execute("SELECT * FROM app.mailbox").fetchall()
            assert len(rows) == 2 and all(r["consumed_at"] for r in rows)
            assert (
                conn.execute(
                    "SELECT count(*) AS n FROM app.read_jobs WHERE status='succeeded'"
                ).fetchone()["n"]
                == 1
            )
        assert engine.get_run(receipt.run_id)["mailbox_cursor"] == 2
        assert engine.claim_effect("fixture") is None
    finally:
        engine.close()


def test_mailbox_byte_backpressure_rolls_back_and_expiry_keeps_input(r2, trigger):
    receipt = r2.ingest(trigger)
    r2.follow_up(receipt.run_id, "large-one", "字" * 16000)
    with pytest.raises(DomainError) as error:
        r2.follow_up(receipt.run_id, "large-two", "字" * 16000)
    assert error.value.code == "MAILBOX_FULL"
    assert r2.get_run(receipt.run_id)["thread_revision"] == 2
    run = r2.begin_run(receipt.run_id)
    with r2.transaction() as conn:
        conn.execute("UPDATE app.mailbox SET expires_at=now()-interval '1 second'")
    with pytest.raises(DomainError) as error:
        r2.prepare_intake(receipt.run_id, run["execution_epoch"])
    assert error.value.code == "MAILBOX_EXPIRED"
    with r2.transaction() as conn:
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM app.mailbox WHERE consumed_at IS NULL"
            ).fetchone()["n"]
            == 2
        )


def test_replan_limit_keeps_new_input_unconsumed(r2, trigger):
    receipt = r2.ingest(trigger)
    run = r2.begin_run(receipt.run_id)
    for index in range(3):
        snapshot = r2.prepare_intake(receipt.run_id, run["execution_epoch"])
        r2.acknowledge_intake(receipt.run_id, run["execution_epoch"], snapshot["snapshot_id"])
        r2.follow_up(receipt.run_id, f"version-{index}", f"More context {index}")
    with pytest.raises(DomainError) as error:
        r2.prepare_intake(receipt.run_id, run["execution_epoch"])
    assert error.value.code == "REPLAN_LIMIT"
    r2.fail_run(receipt.run_id, error.value.code, run["execution_epoch"])
    assert r2.get_run(receipt.run_id)["status"] == "waiting_input"
    with r2.transaction() as conn:
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM app.mailbox WHERE consumed_at IS NULL"
            ).fetchone()["n"]
            == 1
        )


def test_input_between_last_checkpoint_and_report_is_requeued(r2, trigger, monkeypatch):
    receipt = r2.ingest(trigger)
    original = r2.commit_report
    inserted = False

    def add_input(*args):
        nonlocal inserted
        if not inserted:
            inserted = True
            r2.follow_up(receipt.run_id, "last-moment", "Check this extra context")
        return original(*args)

    monkeypatch.setattr(r2, "commit_report", add_input)
    assert Runner(r2).execute(receipt.run_id) is None
    assert r2.get_run(receipt.run_id)["status"] == "queued"
    assert r2.actions(receipt.run_id) == []
    assert Runner(r2).execute(receipt.run_id) is not None
    assert r2.get_run(receipt.run_id)["input_revision"] == 2


def test_parallel_reads_use_independent_checkpoints_and_engine_writer(r2, trigger):
    first = r2.ingest(trigger)
    second = r2.ingest(
        trigger.model_copy(update={"external_event_id": "second-issue", "issue_number": 2})
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(Runner(r2).execute, receipt.run_id) for receipt in [first, second]]
        assert all(f.result(timeout=10) is not None for f in futures)
    assert all(r["status"] == "waiting_approval" for r in r2.list_runs())


def test_api_receives_messages_pause_and_progress_while_read_is_blocked(
    r2, r2_settings, monkeypatch
):
    from fastapi.testclient import TestClient

    from mgar_v3.api import create_app

    started, release = Event(), Event()
    original = FixtureInvestigator.read

    def read(self):
        started.set()
        assert release.wait(5)
        return original(self)

    monkeypatch.setattr(FixtureInvestigator, "read", read)
    with TestClient(
        create_app(r2_settings, r2),
        base_url="http://127.0.0.1",
        headers={"X-MGAR-Token": r2_settings.local_token.get_secret_value()},
    ) as client:
        try:
            created = client.post(
                "/api/v3/runs",
                json={
                    "project_id": "orders-demo",
                    "question": "Document symptoms",
                    "issue_number": 1,
                    "client_request_id": "async-api",
                },
            )
            assert created.status_code == 202
            run_id = created.json()["run_id"]
            assert started.wait(3)
            base = f"/api/v3/runs/{run_id}"
            message = {"body": "Additional context", "client_request_id": "api-followup"}
            assert client.post(base + "/messages", json=message).status_code == 202
            assert client.post(base + "/messages", json=message).json()["duplicate"]
            assert client.post(base + "/pause").json()["status"] == "paused"
            events = client.get(base + "/events").text
            assert "tool_started" in events and "run_paused" in events
            assert not release.is_set()
        finally:
            release.set()
    assert r2.get_run(UUID(run_id))["status"] == "paused"


def test_signed_publication_echo_does_not_become_followup(r2, r2_settings, trigger):
    import hashlib
    import hmac
    import json

    from pydantic import SecretStr
    from test_api import client_for

    receipt = r2.ingest(trigger)
    action_id = Runner(r2).execute(receipt.run_id)
    action = r2.actions(receipt.run_id)[0]
    r2.approve(action_id, action["payload_hash"], "owner")
    claimed = r2.claim_effect("fixture")
    r2.effect_result(action_id, claimed["lease_token"], external_id="fixture-comment")
    settings = r2_settings.model_copy(update={"github_webhook_secret": SecretStr("fixture-secret")})
    payload = {
        "action": "created",
        "repository": {"full_name": trigger.repository},
        "issue": {"number": 1},
        "sender": {"login": "owner"},
        "comment": {"body": action["payload"]["body"]},
    }
    raw = json.dumps(payload).encode()
    with client_for(settings, r2) as client:
        response = client.post(
            "/api/v3/webhooks/github",
            content=raw,
            headers={
                "X-GitHub-Delivery": "our-result-echo",
                "X-GitHub-Event": "issue_comment",
                "X-Hub-Signature-256": "sha256="
                + hmac.new(b"fixture-secret", raw, hashlib.sha256).hexdigest(),
            },
        )
        assert response.json() == {"status": "ignored", "reason": "published_result"}
    assert len(r2.list_runs()) == 1
    assert r2.get_run(receipt.run_id)["thread_revision"] == 1


def test_read_job_reserves_mailbox_capacity_for_its_completion(r2, trigger):
    receipt = r2.ingest(trigger)
    run = r2.begin_run(receipt.run_id)
    snapshot = r2.prepare_intake(receipt.run_id, run["execution_epoch"])
    r2.acknowledge_intake(receipt.run_id, run["execution_epoch"], snapshot["snapshot_id"])
    job = r2.submit_read_job(receipt.run_id, run["execution_epoch"], "read-source:v1")
    claimed = r2.claim_read_job(job["job_id"], run["execution_epoch"])
    for index in range(63):
        r2.follow_up(receipt.run_id, f"queued-{index}", f"Context {index}")
    with pytest.raises(DomainError) as error:
        r2.follow_up(receipt.run_id, "over-limit", "More context")
    assert error.value.code == "MAILBOX_FULL"
    r2.finish_read_job(
        job["job_id"], claimed["lease_token"], b"Observation\n", "fixture://notes", "version-1"
    )
    with r2.transaction() as conn:
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM app.mailbox WHERE consumed_at IS NULL"
            ).fetchone()["n"]
            == 64
        )
    run = r2.get_run(receipt.run_id)
    assert run["thread_revision"] == 64
    snapshot = r2.prepare_intake(receipt.run_id, run["execution_epoch"])
    r2.acknowledge_intake(receipt.run_id, run["execution_epoch"], snapshot["snapshot_id"])
    assert r2.get_run(receipt.run_id)["mailbox_cursor"] == 65
