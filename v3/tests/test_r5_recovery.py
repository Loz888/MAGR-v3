import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from mgar_v3.coordination.engine import Engine
from mgar_v3.effects.dispatcher import Dispatcher
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.graph.runner import Runner
from mgar_v3.mcp.client import ReadOnlyGitHubMCP
from mgar_v3.repair.publication import DraftMCPTransport
from mgar_v3.repair.sandbox import Sandbox


@pytest.mark.parametrize(
    "phase", ["sandbox_created", "candidate_committed", "branch_receipt", "pr_accepted"]
)
def test_sigkill_repair_and_publication_do_not_repeat_unknown_writes(settings, tmp_path, phase):
    remote_path = tmp_path / "remote.json"
    env = {
        **os.environ,
        "MGAR_V3_DATABASE_URL": settings.database_url.get_secret_value(),
        "MGAR_V3_ARTIFACT_ROOT": str(settings.artifact_root),
        "MGAR_V3_REPOSITORY": settings.repository,
        "MGAR_V3_LOCAL_ACTOR": "owner",
        "MGAR_V3_ALLOWED_ACTORS": '["owner"]',
    }
    child = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).parent / "fixtures/r5_crash_worker.py"),
            phase,
            str(remote_path),
        ],
        env=env,
        capture_output=True,
        timeout=90,
    )
    assert child.returncode == -9, child.stdout.decode() + child.stderr.decode()
    engine = Engine(settings, ArtifactStore(settings.artifact_root))
    try:
        result = engine.recover()
        run = engine.list_runs()[0]
        run_id = run["run_id"]
        removed = Sandbox(str(run_id)).cleanup()
        if phase == "sandbox_created":
            assert removed == 1
        if phase in {"sandbox_created", "candidate_committed"}:
            assert result["paused_runs"] == 1
            engine.resume(run_id)
            Runner(engine).execute(run_id)
            assert len(engine.actions(run_id)) == 1
            assert len(engine.repairs.detail(run_id)["candidates"]) == 1
        else:
            client = ReadOnlyGitHubMCP(
                settings.repository,
                command=sys.executable,
                args=("-m", "mgar_v3.repair.fixture_server", str(remote_path)),
            )
            dispatcher = Dispatcher(engine, DraftMCPTransport(engine, client, client))
            action = engine.actions(run_id)[0]
            writes = json.loads(remote_path.read_text())["writes"]
            assert action["status"] == "outcome_unknown" and dispatcher.tick() is None
            assert dispatcher.reconcile(action["action_id"]) == (phase == "pr_accepted")
            assert json.loads(remote_path.read_text())["writes"] == writes
    finally:
        engine.close()
