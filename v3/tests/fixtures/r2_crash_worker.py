import os
import signal
import sys

from mgar_v3.coordination.engine import Engine
from mgar_v3.domain import TriggerInput
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.graph.runner import FixtureInvestigator, Runner
from mgar_v3.settings import Settings

phase = sys.argv[1]
settings = Settings()
engine = Engine(settings, ArtifactStore(settings.artifact_root))
event = TriggerInput.model_validate_json(os.environ["MGAR_TEST_EVENT"])
receipt = engine.ingest(event)


def kill():
    os.kill(os.getpid(), signal.SIGKILL)


acknowledge = engine.acknowledge_intake
complete = engine.finish_read_job


def crash_ack(*args):
    if phase == "checkpoint_before_cursor":
        kill()
    result = acknowledge(*args)
    if phase == "cursor_before_checkpoint":
        kill()
    return result


def crash_complete(*args):
    result = complete(*args)
    if phase == "tool_completion_before_checkpoint":
        kill()
    return result


class Reader(FixtureInvestigator):
    def read(self):
        if phase == "tool_running":
            kill()
        return super().read()


engine.acknowledge_intake = crash_ack
engine.finish_read_job = crash_complete
Runner(engine, Reader()).execute(receipt.run_id)
raise RuntimeError("Expected crash point was not reached")
