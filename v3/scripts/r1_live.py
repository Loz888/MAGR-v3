"""Operator-run, bounded R1 experiment. Crash phases intentionally exit via SIGKILL.

Requires a separately supplied, reviewed live authorization file. No credentials are
written to evidence. Run each phase separately and inspect the proposal before send.
"""

import argparse
import hashlib
import json
import os
import signal
from pathlib import Path
from uuid import UUID

from mgar_v3.coordination.engine import Engine
from mgar_v3.domain import Investigation, TriggerInput
from mgar_v3.effects.dispatcher import Dispatcher
from mgar_v3.effects.github import GitHubMCPTransport
from mgar_v3.evidence.acceptance import r1_coverage
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.graph.live_investigator import LiveInvestigator
from mgar_v3.graph.runner import Runner
from mgar_v3.live import load_permit
from mgar_v3.settings import ROOT, Settings


def main() -> None:
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("phase", choices=["model-crash", "resume", "send-crash", "reconcile"])
    parser.add_argument("--authorization", type=Path, required=True)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--profile", type=Path, default=ROOT / "config/live-r1-proposed.json")
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--execute-paid", action="store_true")
    parser.add_argument("--event-id")
    parser.add_argument("--approved-payload-hash")
    args = parser.parse_args()
    args.directory.mkdir(parents=True, exist_ok=True)
    settings = Settings(
        allow_paid=True,
        allow_external_write=True,
        github_use_gh_token=True,
        github_mcp_binary=args.binary,
        graph_version="r1-quotes-v1",
    )
    permit = load_permit(args.profile, args.authorization, settings)
    permit.validate()
    manifest = args.directory / "experiment.json"
    data = json.loads(manifest.read_text()) if manifest.exists() else {}

    def save() -> None:
        manifest.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str) + "\n")

    event = TriggerInput(
        source="cli",
        external_event_id=args.event_id
        or data.get("event_id", "r1-live:" + permit.profile.profile_hash),
        project_id=permit.profile.project_id,
        repository=permit.profile.repository,
        issue_number=permit.profile.issue_number,
        actor=permit.profile.actor,
        body="/mgar investigate " + permit.profile.question,
        execution_mode="live",
    )
    engine = Engine(settings, ArtifactStore(settings.artifact_root))
    try:
        if args.phase == "model-crash":
            if not args.execute_paid or manifest.exists():
                raise RuntimeError("Explicit paid execution and a new experiment are required")
            receipt = engine.ingest(event, permit)
            if receipt.duplicate or receipt.route != "created" or receipt.run_id is None:
                raise RuntimeError(
                    "Existing delivery must be recovered, never automatically resent"
                )
            run_id = receipt.run_id
            data.update(
                profile_hash=permit.profile.profile_hash,
                run_id=str(run_id),
                event_id=event.external_event_id,
                ticket_id=str(receipt.ticket_id),
                oracle_sha256=hashlib.sha256(
                    (ROOT / "config/r1-live-oracle.json").read_bytes()
                ).hexdigest(),
                components={
                    "postgres": "real",
                    "checkpoint": "real",
                    "github_mcp": "real",
                    "deepseek": "real",
                },
            )
            save()

            def crash_after_checkpoint(run_id, epoch, report, verdict):
                data["before_model_crash"] = {
                    "epoch": epoch,
                    "report_hash": hashlib.sha256(report).hexdigest(),
                    "verification": verdict.model_dump(),
                }
                save()
                os.kill(os.getpid(), signal.SIGKILL)

            engine.commit_report = crash_after_checkpoint
            Runner(engine, LiveInvestigator(engine, run_id, permit)).execute(run_id)
            raise RuntimeError("Crash hook was not reached")

        run_id = UUID(data["run_id"])
        if args.phase == "resume":
            data["model_recovery"] = engine.recover()
            assert engine.resume(run_id) == run_id
            action_id = Runner(engine, LiveInvestigator(engine, run_id, permit)).execute(run_id)
            assert action_id is not None
            with engine.transaction() as conn:
                calls = conn.execute(
                    "SELECT * FROM app.usage_calls WHERE run_id=%s", (run_id,)
                ).fetchall()
            assert len(calls) == 1 and calls[0]["status"] == "known"
            finding = Investigation.model_validate(calls[0]["result"])
            coverage = r1_coverage(finding, permit.profile.expected_source_hash)
            data["coverage"] = coverage
            data["calls"] = [
                {key: row[key] for key in ["call_id", "status", "settled_micros", "usage"]}
                for row in calls
            ]
            action = engine.actions(run_id)[0]
            data["action_id"] = str(action_id)
            data["payload_hash"] = action["payload_hash"]
            data["report_hash"] = engine.get_run(run_id)["report_hash"]
            (args.directory / "proposal.md").write_text(action["payload"]["body"])
            save()
            assert coverage["passed"], "Independent frozen coverage oracle failed"
        elif args.phase == "send-crash":
            assert data["coverage"]["passed"]
            if args.approved_payload_hash != data["payload_hash"]:
                raise RuntimeError("Operator must inspect and approve the exact proposal digest")
            engine.approve(
                UUID(data["action_id"]), data["payload_hash"], permit.authorization.approved_by
            )
            action = engine.claim_effect("live")
            assert action is not None and str(action["action_id"]) == data["action_id"]
            # Actual MCP write occurs after the durable dispatch lease, outside a PG transaction.
            # SIGKILL prevents receipt persistence; startup must reconcile instead of reposting.
            external_id = GitHubMCPTransport(settings, permit).send(action)
            data["before_write_crash"] = {"external_id_observed_by_harness": external_id}
            save()
            os.kill(os.getpid(), signal.SIGKILL)
        elif args.phase == "reconcile":
            data["write_recovery"] = engine.recover()
            dispatcher = Dispatcher(engine, GitHubMCPTransport(settings, permit))
            assert dispatcher.tick() is None, "Unknown write must never be reposted"
            assert dispatcher.reconcile(UUID(data["action_id"]))
            assert dispatcher.tick() is None
            action = engine.actions(run_id)[0]
            assert action["status"] == "succeeded"
            assert engine.get_run(run_id)["status"] == "succeeded"
            replay = engine.ingest(event, permit)
            assert replay.duplicate and replay.run_id == run_id
            with engine.transaction() as conn:
                budget = conn.execute(
                    "SELECT * FROM app.authorization_budgets WHERE profile_hash=%s",
                    (permit.profile.profile_hash,),
                ).fetchone()
                assert budget is not None
                assert (
                    budget["used_calls"] <= 3
                    and budget["settled_micros"] + budget["reserved_micros"] <= 2000000
                )
            data.update(
                status="PASS",
                actual_model_calls=budget["used_calls"],
                conservative_cost_micros=budget["settled_micros"],
                reserved_micros=budget["reserved_micros"],
                external_id=action["external_id"],
                comment_url=f"https://github.com/{permit.profile.repository}/issues/{permit.profile.issue_number}#issuecomment-{action['external_id']}",
                duplicate_delivery=True,
                no_unknown_write_retry=True,
            )
            save()
        print(json.dumps(data, ensure_ascii=False, default=str))
    finally:
        engine.close()


if __name__ == "__main__":
    main()
