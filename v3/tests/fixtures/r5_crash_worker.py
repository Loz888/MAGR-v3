import json
import os
import signal
import sys
from pathlib import Path
from uuid import uuid4

from mgar_v3.coordination.engine import Engine
from mgar_v3.effects.dispatcher import Dispatcher
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.graph.runner import Runner
from mgar_v3.mcp.client import ReadOnlyGitHubMCP
from mgar_v3.repair.domain import RepairInput
from mgar_v3.repair.publication import DraftMCPTransport
from mgar_v3.repair.sandbox import Sandbox
from mgar_v3.settings import Settings

phase = sys.argv[1]
remote_path = Path(sys.argv[2])
settings = Settings()
engine = Engine(settings, ArtifactStore(settings.artifact_root))
run_id = engine.repairs.create(
    RepairInput(
        project_id=settings.project_id,
        issue_number=5001,
        question="Fix paid revenue: first paid occurrence per ID.",
        client_request_id=str(uuid4()),
    )
)["run_id"]


def kill():
    os.kill(os.getpid(), signal.SIGKILL)


if phase == "sandbox_created":
    original = Sandbox.run

    def crash_box(self, files, expected, timeout=30, after_created=None):
        return original(self, files, expected, timeout, after_created=lambda _: kill())

    Sandbox.run = crash_box
if phase == "candidate_committed":
    original_candidate = engine.repairs.candidate

    def crash_candidate(*args, **kwargs):
        result = original_candidate(*args, **kwargs)
        kill()
        return result

    engine.repairs.candidate = crash_candidate
Runner(engine).execute(run_id)
action = engine.actions(run_id)[0]
snapshot = engine.repairs.snapshot(run_id)
remote_path.write_text(
    json.dumps(
        {
            "repository": snapshot.repository,
            "branches": {"main": snapshot.base_sha},
            "commits": {
                snapshot.base_sha: {
                    "sha": snapshot.base_sha,
                    "parents": [],
                    "commit": {"tree": {"sha": snapshot.base_tree}},
                    "files": [],
                    "source_files": snapshot.files,
                }
            },
            "prs": [],
            "writes": [],
        }
    )
)
client = ReadOnlyGitHubMCP(
    snapshot.repository,
    command=sys.executable,
    args=("-m", "mgar_v3.repair.fixture_server", str(remote_path)),
)
remote = DraftMCPTransport(engine, client, client)
engine.approve(action["action_id"], action["payload_hash"], "owner")
if phase == "branch_receipt":
    original_receipt = engine.repairs.publication_receipt

    def crash_receipt(*args, **kwargs):
        original_receipt(*args, **kwargs)
        if args[2] == 0:
            kill()

    engine.repairs.publication_receipt = crash_receipt
if phase == "pr_accepted":
    original_call = remote.call

    def crash_call(tool, args):
        value = original_call(tool, args)
        if tool == "create_pull_request":
            kill()
        return value

    remote.call = crash_call
Dispatcher(engine, remote).tick()
raise RuntimeError("SIGKILL point was not reached")
