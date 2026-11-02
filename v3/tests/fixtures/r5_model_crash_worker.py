import os
import runpy
import signal
import sys
from pathlib import Path

from mgar_v3.coordination.engine import Engine
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.graph.runner import Runner
from mgar_v3.repair.graph import execute_repair
from mgar_v3.repair.live import DeepSeekPatcher
from mgar_v3.settings import Settings

helpers = runpy.run_path(str(Path(__file__).resolve().parents[1] / "test_r5_live.py"))
live_case, model_client = helpers["live_case"], helpers["model_client"]
engine = Engine(Settings(), ArtifactStore(Settings().artifact_root))
root = Path(sys.argv[1])
invalid = sys.argv[2] == "invalid-first"
run_id, permit, snapshot = live_case(engine, root)
client, calls = model_client(snapshot, invalid_first=invalid)
settle = engine.settle_call


def crash_after_known_result(*args, **kwargs):
    settle(*args, **kwargs)
    (root / "physical-calls.txt").write_text(str(len(calls)))
    if len(args) > 3 and args[3] is not None:
        os.kill(os.getpid(), signal.SIGKILL)


engine.settle_call = crash_after_known_result
execute_repair(Runner(engine), run_id, DeepSeekPatcher(engine, run_id, 1, permit, client))
raise RuntimeError("Expected SIGKILL was not reached")
