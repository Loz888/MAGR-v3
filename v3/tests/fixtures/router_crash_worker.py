import os
import signal
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from router_helpers import setup_router, success_response

from mgar_v3.coordination.engine import Engine
from mgar_v3.domain import TriggerInput
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.routing.budget import RouterBudget
from mgar_v3.routing.model import DeepSeekRouter
from mgar_v3.routing.worker import RouterWorker
from mgar_v3.settings import Settings

phase = sys.argv[1]
settings = Settings()
engine = Engine(settings, ArtifactStore(settings.artifact_root))
root, permit = setup_router(engine)
engine.ingest(TriggerInput.model_validate_json(os.environ["MGAR_TEST_EVENT"]))


def kill():
    os.kill(os.getpid(), signal.SIGKILL)


settle = RouterBudget.settle
save = engine.routing.save
commit = engine.routing.commit


def crash_settle(self, *args, **kwargs):
    result = settle(self, *args, **kwargs)
    if phase == "model_result_settled":
        kill()
    return result


def crash_save(*args):
    result = save(*args)
    if phase == "decision_saved":
        kill()
    return result


def crash_commit(*args, **kwargs):
    result = commit(*args, **kwargs)
    if phase == "decision_applied":
        kill()
    return result


RouterBudget.settle = crash_settle
engine.routing.save = crash_save
engine.routing.commit = crash_commit
if phase == "batch_prepared":
    engine.routing.prepare("live")
    kill()
with httpx.Client(transport=httpx.MockTransport(success_response)) as client:
    RouterWorker(engine, DeepSeekRouter(engine, permit, client)).tick()
raise RuntimeError("Expected crash point was not reached")
