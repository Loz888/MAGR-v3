"""Review a real isolated Go repair, optionally publish to the local MCP fixture only."""

import argparse
import json
import sys
from uuid import UUID, uuid4

from fastapi.encoders import jsonable_encoder

from mgar_v3.coordination.engine import Engine
from mgar_v3.effects.dispatcher import Dispatcher
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.graph.runner import Runner
from mgar_v3.mcp.client import ReadOnlyGitHubMCP
from mgar_v3.repair.domain import RepairInput
from mgar_v3.repair.graph import acknowledge_approval
from mgar_v3.repair.publication import DraftMCPTransport
from mgar_v3.settings import ROOT, Settings


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", type=UUID)
    parser.add_argument("--approve-payload-hash")
    parser.add_argument("--issue", type=int, default=5001)
    args = parser.parse_args()
    if args.approve_payload_hash and not args.run:
        parser.error("--approve-payload-hash requires --run")
    settings = Settings()
    engine = Engine(settings, ArtifactStore(settings.artifact_root))
    try:
        if args.run:
            run_id = args.run
        else:
            receipt = engine.repairs.create(
                RepairInput(
                    project_id=settings.project_id,
                    issue_number=args.issue,
                    question="Count the first paid order per ID; ignore other statuses.",
                    client_request_id="r5-demo:" + str(uuid4()),
                )
            )
            run_id = UUID(receipt["run_id"])
            Runner(engine).execute(run_id)
        actions = engine.actions(run_id)
        if not actions:
            print(json.dumps(jsonable_encoder(engine.get_run(run_id)), indent=2))
            return
        action = actions[-1]
        if args.approve_payload_hash:
            engine.approve(action["action_id"], args.approve_payload_hash, settings.local_actor)
            acknowledge_approval(engine, run_id, args.approve_payload_hash)
            snapshot = engine.repairs.snapshot(run_id)
            remote_path = ROOT / ".local/r5" / ("remote-" + str(run_id) + ".json")
            if not remote_path.exists():
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
                settings.repository,
                command=sys.executable,
                args=("-m", "mgar_v3.repair.fixture_server", str(remote_path)),
            )
            dispatcher = Dispatcher(engine, DraftMCPTransport(engine, client, client))
            if action["status"] == "outcome_unknown":
                dispatcher.reconcile(action["action_id"])
            else:
                dispatcher.tick()
        print(
            json.dumps(
                jsonable_encoder(
                    {
                        "run_id": run_id,
                        "mode": "fixture",
                        "actions": engine.actions(run_id),
                        "repair": engine.repairs.detail(run_id),
                    }
                ),
                indent=2,
            )
        )
    finally:
        engine.close()


if __name__ == "__main__":
    main()
