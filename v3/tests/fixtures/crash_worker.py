import json
import os
import signal
import sys

from mgar_v3.coordination.engine import Engine
from mgar_v3.domain import TriggerInput
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.graph.runner import Runner
from mgar_v3.settings import Settings

phase = sys.argv[1]
settings = Settings()
engine = Engine(settings, ArtifactStore(settings.artifact_root))
event = TriggerInput.model_validate_json(os.environ["MGAR_TEST_EVENT"])
receipt = engine.ingest(event)


def crash():
    os.kill(os.getpid(), signal.SIGKILL)


if phase == "event_committed":
    crash()
if phase == "run_started":
    engine.begin_run(receipt.run_id)
    crash()
if phase == "checkpoint_before_report":
    engine.commit_report = lambda *args: crash()
action_id = Runner(engine).execute(receipt.run_id)
if phase == "report_committed":
    crash()
action = engine.actions(receipt.run_id)[0]
engine.approve(action_id, action["payload_hash"], "owner")
if phase == "approval_enqueued":
    crash()
claimed = engine.claim_effect("fixture")
if phase == "remote_accepted":
    with open(os.environ["MGAR_TEST_REMOTE_LEDGER"], "w") as stream:
        json.dump([claimed["payload"]], stream)
        stream.flush()
        os.fsync(stream.fileno())
crash()
