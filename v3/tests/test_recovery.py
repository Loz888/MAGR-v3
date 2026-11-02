import os
import subprocess
import sys
from uuid import UUID

import pytest

from mgar_v3.coordination.engine import Engine
from mgar_v3.domain import DomainError
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.graph.runner import Runner


def test_real_process_exit_preserves_accepted_event_and_fences_old_epoch(settings, trigger):
    code = """
import os
from mgar_v3.coordination.engine import Engine
from mgar_v3.settings import Settings
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.domain import TriggerInput
settings=Settings()
engine=Engine(settings,ArtifactStore(settings.artifact_root))
event=TriggerInput.model_validate_json(os.environ['MGAR_TEST_EVENT'])
receipt=engine.ingest(event)
engine.begin_run(receipt.run_id)
os._exit(23)
"""
    environment = {
        **os.environ,
        "MGAR_V3_DATABASE_URL": settings.database_url.get_secret_value(),
        "MGAR_V3_ARTIFACT_ROOT": str(settings.artifact_root),
        "MGAR_V3_REPOSITORY": settings.repository,
        "MGAR_V3_LOCAL_ACTOR": settings.local_actor,
        "MGAR_V3_ALLOWED_ACTORS": '["owner"]',
        "MGAR_TEST_EVENT": trigger.model_dump_json(),
    }
    process = subprocess.run(
        [sys.executable, "-c", code], env=environment, capture_output=True, timeout=20
    )
    assert process.returncode == 23
    recovered = Engine(settings, ArtifactStore(settings.artifact_root))
    try:
        assert recovered.recover()["paused_runs"] == 1
        original = recovered.list_runs()[0]
        assert original["status"] == "paused" and original["execution_epoch"] == 2
        receipt = recovered.ingest(trigger)
        assert receipt.duplicate
        replacement = recovered.resume(original["run_id"])
        assert replacement != original["run_id"]
        Runner(recovered).execute(replacement)
        assert recovered.get_run(replacement)["status"] == "waiting_approval"
        with pytest.raises(DomainError):
            recovered.resume(original["run_id"])
    finally:
        recovered.close()


def test_crash_after_effect_claim_becomes_unknown_and_old_receipt_is_fenced(
    engine, trigger, settings
):
    receipt = engine.ingest(trigger)
    action_id = Runner(engine).execute(receipt.run_id)
    action = engine.actions(receipt.run_id)[0]
    engine.approve(action_id, action["payload_hash"], "owner")
    claim = engine.claim_effect("fixture")
    engine.close()
    replacement = Engine(settings, ArtifactStore(settings.artifact_root))
    try:
        assert replacement.recover()["unknown_actions"] == 1
        assert replacement.claim_effect("fixture") is None
        with pytest.raises(DomainError, match="no longer current"):
            replacement.effect_result(action_id, UUID(str(claim["lease_token"])), external_id="old")
        assert not replacement.reconcile_effect(action_id, None)
        assert replacement.reconcile_effect(action_id, "confirmed-existing-comment")
    finally:
        replacement.close()


@pytest.mark.parametrize(
    "phase",
    [
        "event_committed",
        "run_started",
        "checkpoint_before_report",
        "report_committed",
        "approval_enqueued",
        "effect_claimed",
        "remote_accepted",
    ],
)
def test_sigkill_at_each_handoff_preserves_event_without_republishing(
    settings, trigger, tmp_path, phase
):
    import json
    from pathlib import Path

    from test_effects import FakeRemote

    from mgar_v3.effects.dispatcher import Dispatcher

    remote_ledger = tmp_path / "remote.json"
    environment = {
        **os.environ,
        "MGAR_V3_DATABASE_URL": settings.database_url.get_secret_value(),
        "MGAR_V3_ARTIFACT_ROOT": str(settings.artifact_root),
        "MGAR_V3_REPOSITORY": settings.repository,
        "MGAR_V3_LOCAL_ACTOR": settings.local_actor,
        "MGAR_V3_ALLOWED_ACTORS": '["owner"]',
        "MGAR_TEST_EVENT": trigger.model_dump_json(),
        "MGAR_TEST_REMOTE_LEDGER": str(remote_ledger),
    }
    worker = Path(__file__).parent / "fixtures/crash_worker.py"
    process = subprocess.run(
        [sys.executable, str(worker), phase], env=environment, capture_output=True, timeout=20
    )
    assert process.returncode == -9, process.stderr.decode()
    recovered = Engine(settings, ArtifactStore(settings.artifact_root))
    try:
        summary = recovered.recover()
        assert recovered.ingest(trigger).duplicate
        run = recovered.list_runs()[0]
        remote = FakeRemote()
        if remote_ledger.exists():
            remote.posts = json.loads(remote_ledger.read_text())
        dispatcher = Dispatcher(recovered, remote)
        if phase in {"run_started", "checkpoint_before_report"}:
            assert summary["paused_runs"] == 1
            replacement = recovered.resume(run["run_id"])
            Runner(recovered).execute(replacement)
            assert len(recovered.list_runs()) == 2
        elif phase in {"effect_claimed", "remote_accepted"}:
            assert summary["unknown_actions"] == 1
            assert dispatcher.tick() is None
            action = recovered.actions(run["run_id"])[0]
            assert dispatcher.reconcile(action["action_id"]) == (phase == "remote_accepted")
            assert len(remote.posts) == (1 if phase == "remote_accepted" else 0)
        elif phase == "approval_enqueued":
            assert dispatcher.tick() is not None
            assert dispatcher.tick() is None and len(remote.posts) == 1
        else:
            assert run["status"] == ("queued" if phase == "event_committed" else "waiting_approval")
    finally:
        recovered.close()
