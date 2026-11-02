"""One audited recovery of a failed multi-expert arm under the existing trial grant."""

import argparse
import hashlib
import json
import time
from pathlib import Path
from uuid import UUID

from r4_live import configuration, write

from mgar_v3.coordination.engine import COMMAND, Engine
from mgar_v3.domain import TriggerInput, digest
from mgar_v3.evaluation.domain import TrialAuthorization
from mgar_v3.evaluation.frozen import implementation_hash
from mgar_v3.evaluation.oracle import grade
from mgar_v3.evaluation.provider import TrialExperts
from mgar_v3.evaluation.runtime import mcp_authority
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.graph.experts import execute_experts
from mgar_v3.graph.runner import Runner
from mgar_v3.live import Authorization, LivePermit, LiveProfile
from mgar_v3.settings import ROOT


def main() -> None:
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--case", required=True)
    parser.add_argument("--execute-paid", action="store_true")
    args = parser.parse_args()
    if not args.execute_paid:
        raise SystemExit("Explicit paid execution under the existing exact grant is required")
    directory = args.directory.resolve()
    manifest = json.loads((directory / "manifest.json").read_text())
    original = json.loads((directory / "result.json").read_text())
    auth = TrialAuthorization.model_validate_json(
        (directory / "trial-authorization.json").read_bytes()
    )
    if auth.manifest_hash != digest(manifest):
        raise RuntimeError("Original trial grant changed")
    if (
        hashlib.sha256((directory / "oracle.json").read_bytes()).hexdigest()
        != manifest["oracle_sha256"]
    ):
        raise RuntimeError("Frozen oracle changed")
    oracle = json.loads((directory / "oracle.json").read_text())[args.case]
    case = next(c for c in manifest["cases"] if c["id"] == args.case)
    parent = next(
        r for r in original["results"] if r["case_id"] == args.case and r["arm"] == "multi"
    )
    if parent["status"] != "FAIL":
        raise RuntimeError("Recovery cannot cherry-pick an already successful arm")
    destination = directory / args.case / "multi-recovery-1"
    destination.mkdir(exist_ok=True)
    if (destination / "started.json").exists():
        raise RuntimeError("Recorded recovery must be inspected, never silently resent")
    cfg = configuration(directory, case, paid=True)
    engine = Engine(cfg, ArtifactStore(cfg.artifact_root))
    try:
        profile = LiveProfile.model_validate(
            next(
                p["profile"]
                for p in manifest["profiles"]
                if p["case_id"] == args.case and p["arm"] == "multi"
            )
        )
        profile = profile.model_copy(update={"case_id": profile.case_id + ":recovery-1"})
        code_hash = implementation_hash(ROOT)
        engine.trials.bind_recovery(
            UUID(original["trial_id"]),
            UUID(parent["run_id"]),
            profile,
            code_hash,
            (
                "Empty Repo extraction claims caused a retry and exhausted the "
                "per-run three-call cap before synthesis. Scope each model expert "
                "to local source extraction; keep the full business question in "
                "synthesis. Original first-pass result, inputs, oracle, source "
                "commit and shared 24-call cap remain unchanged."
            ),
        )
        authorization = Authorization(
            profile_hash=profile.profile_hash,
            approved_by=auth.approved_by,
            expires_at=auth.expires_at,
            allow_paid=True,
            allow_external_write=False,
        )
        cfg.live_profile_path = destination / "profile.json"
        cfg.live_authorization_path = destination / "authorization.json"
        write(cfg.live_profile_path, profile.model_dump(mode="json"))
        write(cfg.live_authorization_path, authorization.model_dump(mode="json"))
        permit = LivePermit(profile, authorization, cfg)
        receipt = engine.ingest(
            TriggerInput(
                source="cli",
                external_event_id="r4-recovery:" + manifest["trial_token"] + ":" + args.case,
                project_id=cfg.project_id,
                repository=cfg.repository,
                issue_number=1,
                actor=cfg.local_actor,
                body=COMMAND + profile.question,
                execution_mode="live",
            ),
            permit,
        )
        assert receipt.run_id
        engine.tools.requested_snapshots(receipt.run_id, [UUID(case["snapshot_id"])])
        write(
            destination / "started.json",
            {
                "run_id": str(receipt.run_id),
                "parent_run_id": parent["run_id"],
                "manifest_hash": digest(manifest),
                "implementation_sha256": code_hash,
                "oracle_sha256": manifest["oracle_sha256"],
                "shared_grant_changed": False,
            },
        )
        started = time.monotonic()
        with mcp_authority(engine):
            execute_experts(
                Runner(engine), receipt.run_id, TrialExperts(engine, permit, case, "multi")
            )
        resolution = engine.tools.resolution(receipt.run_id)
        with engine.transaction() as conn:
            analysis = conn.execute(
                "SELECT * FROM app.r4_analyses WHERE run_id=%s", (receipt.run_id,)
            ).fetchone()
            reads = conn.execute(
                (
                    "SELECT e.*,r.role,r.task_id,r.origin FROM app.evidence e JOIN "
                    "app.r4_reads r USING(evidence_id) WHERE run_id=%s"
                ),
                (receipt.run_id,),
            ).fetchall()
            calls = conn.execute(
                "SELECT * FROM app.usage_calls WHERE run_id=%s", (receipt.run_id,)
            ).fetchall()
            experts = conn.execute(
                "SELECT * FROM app.expert_results WHERE run_id=%s", (receipt.run_id,)
            ).fetchall()
        judged = grade(
            analysis["result"] if analysis else {},
            oracle,
            {str(r["evidence_id"]): r["role"] for r in reads},
            mechanical=bool(resolution and resolution["verdict"]["passed"]),
        )
        result = {
            "case_id": args.case,
            "arm": "multi",
            "attempt": "recovery-1",
            "status": "PASS" if judged["passed"] else "FAIL",
            "run_id": str(receipt.run_id),
            "parent_run_id": parent["run_id"],
            "adjudication": judged,
            "physical_calls": len(calls),
            "cost_micros": sum(c["settled_micros"] or 0 for c in calls),
            "duration_seconds": round(time.monotonic() - started, 3),
            "assessment": analysis["result"] if analysis else None,
            "implementation_sha256": code_hash,
            "new_github_comments": 0,
        }
        write(
            destination / "execution.json",
            {
                "result": result,
                "reads": reads,
                "calls": calls,
                "experts": experts,
                "events": engine.events(receipt.run_id),
                "resolution": resolution,
            },
        )
        write(destination / "result.json", result)
        run = engine.get_run(receipt.run_id)
        if run["report_hash"]:
            (destination / "report.md").write_bytes(engine.artifact_bytes(run["report_hash"]))
        engine.cancel(receipt.run_id)
        print(
            json.dumps(
                {k: result[k] for k in ("status", "physical_calls", "cost_micros", "adjudication")}
            )
        )
        raise SystemExit(0 if judged["passed"] else 1)
    finally:
        engine.close()


if __name__ == "__main__":
    main()
