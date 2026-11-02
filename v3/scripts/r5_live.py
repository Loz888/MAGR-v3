"""Operator stages for a bounded real GitHub/DeepSeek repair. No implicit authorization."""

import argparse
import asyncio
import hashlib
import json
import os
import signal
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from fastapi.encoders import jsonable_encoder

from mgar_v3.coordination.engine import Engine
from mgar_v3.domain import DomainError
from mgar_v3.effects.dispatcher import Dispatcher
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.graph.runner import Runner
from mgar_v3.live import Authorization, LiveProfile
from mgar_v3.mcp.factory import github_client
from mgar_v3.repair.domain import RepairInput, RepairScope
from mgar_v3.repair.graph import acknowledge_approval
from mgar_v3.repair.live import repair_permit
from mgar_v3.repair.publication import LiveDraftMCPTransport
from mgar_v3.repair.source import freeze_github
from mgar_v3.settings import ROOT, Settings

ROOTS = [
    "5502fc0230f61f8658fef64adb891729cf668da83c1fcdc2ef440c9b31a740e1",
    "f106d904b00cfb3136d5c4ed2fddad0519d25ba3b5fffeace3dff842f9909315",
]
QUESTION = (
    "修复 PaidRevenueCents：只统计 paid 状态，按订单 ID 去重，保留每个 ID 第一次 paid 记录；"
    "更早的 pending/cancelled 不应阻止后续 paid。金额为整数分，可以为零或负数。"
)


def configured(directory: Path) -> Settings:
    return Settings(
        github_use_gh_token=True,
        github_mcp_binary=ROOT / ".local/tools/github-mcp-server/v2.0.3/github-mcp-server",
        repair_profile_path=directory / "profile.json",
        repair_authorization_path=directory / "authorization.json",
        repair_snapshot_path=directory / "snapshot.json",
    )


def prepare(directory: Path, issue: int, ref: str | None) -> None:
    settings = configured(directory)
    repository = settings.repository.lower()
    prefix = "v3/examples/repair-target"
    if ref is None:

        async def head() -> str:
            client = github_client(settings, repository, tool_group="r5_read")
            owner, repo = repository.split("/", 1)
            async with client.session() as session:
                result = await session.call_tool(
                    "get_commit", {"owner": owner, "repo": repo, "sha": "main", "detail": "none"}
                )
            if result.isError:
                raise DomainError("SOURCE_READ_FAILED", "Cannot freeze the GitHub base")
            values = [b.text for b in result.content if b.type == "text"]
            return str(json.loads(values[0])["sha"])

        ref = asyncio.run(head())
    snapshot = freeze_github(repository, ref, prefix, "main", github_client(settings, repository))
    raw = snapshot.model_dump_json().encode()
    price = json.loads((ROOT / "config/live-r1-proposed.json").read_text())
    profile = LiveProfile(
        investigation_version="r5-repair-v1",
        case_id="r5-live-paid-revenue",
        project_id=settings.project_id,
        repository=repository,
        issue_number=issue,
        actor=settings.local_actor,
        question=QUESTION,
        source_kind="file",
        source_path=prefix + "/orders.go",
        source_ref=ref,
        expected_source_hash=hashlib.sha256(
            snapshot.files[prefix + "/orders.go"].encode()
        ).hexdigest(),
        model_id=price["model_id"],
        price_version=price["price_version"],
        input_micros_per_million=price["input_micros_per_million"],
        output_micros_per_million=price["output_micros_per_million"],
        context_tokens=8192,
        max_output_tokens=2048,
        max_source_bytes=16384,
        max_model_calls=3,
        max_cost_micros=20000,
        action_type="draft_pr",
        repair=RepairScope(
            snapshot_sha256=hashlib.sha256(raw).hexdigest(),
            base_tree=snapshot.base_tree,
            workspace_prefix=prefix,
            predecessor_budget_hashes=ROOTS,
        ),
    )
    profile = profile.model_copy(update={"max_cost_micros": profile.reservation_micros * 3})
    proposed = Authorization(
        profile_hash=profile.profile_hash,
        approved_by=settings.local_actor,
        expires_at=datetime.now(UTC) + timedelta(days=1),
        allow_paid=False,
        allow_external_write=False,
    )
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if any(
        (directory / p).exists() for p in ("profile.json", "snapshot.json", "authorization.json")
    ):
        raise DomainError(
            "PREPARE_EXISTS", "Use a new directory; frozen inputs cannot be overwritten"
        )
    (directory / "snapshot.json").write_bytes(raw)
    (directory / "profile.json").write_text(profile.model_dump_json(indent=2) + "\n")
    (directory / "authorization-proposed.json").write_text(
        proposed.model_dump_json(indent=2) + "\n"
    )
    plan = {
        "profile_hash": profile.profile_hash,
        "repository": repository,
        "issue_number": issue,
        "base_sha": ref,
        "base_tree": snapshot.base_tree,
        "editable_paths": snapshot.editable_paths,
        "max_new_paid_calls": 3,
        "max_new_cost_micros": profile.max_cost_micros,
        "cumulative_limit_micros": 2000000,
        "cumulative_predecessors": ROOTS,
        "max_draft_prs": 1,
        "merge": False,
        "authorization_created": False,
    }
    (directory / "plan.json").write_text(json.dumps(plan, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps(plan, indent=2, ensure_ascii=False))


def execute(
    directory: Path,
    stage: str,
    paid: bool,
    approved: str | None,
    crash_model: bool = False,
    crash_pr: bool = False,
) -> None:
    settings = configured(directory)
    permit = repair_permit(settings)
    if stage == "model" and not paid:
        raise DomainError("EXPLICIT_PAID_REQUIRED", "model requires --execute-paid", 403)
    engine = Engine(settings, ArtifactStore(settings.artifact_root))
    try:
        engine.recover()
        if crash_model:
            settle = engine.settle_call

            def settle_then_kill(*args, **kwargs):
                settle(*args, **kwargs)
                if len(args) > 3 and args[3] is not None:
                    os.kill(os.getpid(), signal.SIGKILL)

            engine.settle_call = settle_then_kill
        case_path = directory / "case.json"
        if stage == "model":
            permit.validate()
            if case_path.exists():
                run_id = UUID(json.loads(case_path.read_text())["run_id"])
                run = engine.get_run(run_id)
                if run["status"] == "paused":
                    engine.resume(run_id)
                elif run["status"] != "queued":
                    raise DomainError(
                        "RUN_ALREADY_EXECUTED",
                        "Inspect the stored result; model work is not replayed",
                    )
            else:
                p = permit.profile
                receipt = engine.repairs.create(
                    RepairInput(
                        project_id=p.project_id,
                        issue_number=p.issue_number,
                        question=p.question,
                        client_request_id="r5-live:" + str(uuid4()),
                        execution_mode="live",
                    ),
                    permit,
                )
                run_id = UUID(receipt["run_id"])
                case_path.write_text(json.dumps({"run_id": str(run_id)}) + "\n")
            runner = Runner(engine)
            try:
                runner.execute(run_id)
            except DomainError as exc:
                engine.fail_run(run_id, exc.code, getattr(runner, "execution_epoch", None))
                raise
        else:
            if not case_path.exists():
                raise DomainError("RUN_REQUIRED", "Complete the model/test stage first")
            run_id = UUID(json.loads(case_path.read_text())["run_id"])
            current_run = engine.get_run(run_id)
            actions = [
                a
                for a in engine.actions(run_id)
                if a["base_revision"] == current_run["thread_revision"]
                and a["status"] not in {"cancelled", "expired", "failed"}
            ]
            if len(actions) != 1:
                raise DomainError(
                    "ACTION_REQUIRED", "Exactly one current repair proposal is required"
                )
            action = actions[0]
            dispatcher = Dispatcher(engine, LiveDraftMCPTransport(engine, permit))
            if crash_pr:
                call = dispatcher.transport.call

                def call_then_kill(tool, arguments):
                    response = call(tool, arguments)
                    if tool == "create_pull_request":
                        os.kill(os.getpid(), signal.SIGKILL)
                    return response

                dispatcher.transport.call = call_then_kill
            if stage == "continue":
                if approved != action["payload_hash"]:
                    raise DomainError(
                        "APPROVAL_HASH_MISMATCH",
                        "Continue needs the exact approved payload hash",
                        403,
                    )
                permit.validate(external_write=True)
                engine.repairs.continue_publication(
                    action["action_id"], approved, settings.local_actor
                )
                dispatcher.tick()
            elif stage == "publish":
                if approved != action["payload_hash"]:
                    raise DomainError(
                        "APPROVAL_HASH_MISMATCH",
                        "Review and approve the exact complete proposal",
                        403,
                    )
                permit.validate(external_write=True)
                engine.approve(action["action_id"], approved, settings.local_actor)
                acknowledge_approval(engine, run_id, approved)
                dispatcher.tick()
            else:
                dispatcher.reconcile(action["action_id"])
        result = {
            "run": engine.get_run(run_id),
            "actions": engine.actions(run_id),
            "repair": engine.repairs.detail(run_id),
        }
        (directory / (stage + "-result.json")).write_text(
            json.dumps(jsonable_encoder(result), indent=2, ensure_ascii=False) + "\n"
        )
        for action in result["actions"]:
            (directory / "proposal.json").write_text(
                json.dumps(jsonable_encoder(action), indent=2, ensure_ascii=False) + "\n"
            )
            (directory / "proposal.diff").write_text(action["payload"]["diff"])
            (directory / "proposal.md").write_text(
                "# R5 Draft PR proposal\n\n"
                + action["payload"]["body"]
                + "\n\nPayload hash: "
                + action["payload_hash"]
                + "\n\n"
                + json.dumps(jsonable_encoder(action["payload"]), indent=2, ensure_ascii=False)
                + "\n"
            )
        print(
            json.dumps(
                {
                    "run_id": str(run_id),
                    "status": result["run"]["status"],
                    "actions": [
                        {
                            "action_id": str(a["action_id"]),
                            "status": a["status"],
                            "payload_hash": a["payload_hash"],
                            "external_id": a["external_id"],
                        }
                        for a in result["actions"]
                    ],
                },
                ensure_ascii=False,
            )
        )
    finally:
        engine.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("stage", choices=["prepare", "model", "publish", "reconcile", "continue"])
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--base-sha")
    parser.add_argument("--issue", type=int, default=1)
    parser.add_argument("--execute-paid", action="store_true")
    parser.add_argument("--approved-payload-hash")
    parser.add_argument("--crash-after-settlement", action="store_true")
    parser.add_argument("--crash-after-pr-accepted", action="store_true")
    args = parser.parse_args()
    if args.stage == "prepare":
        prepare(args.directory, args.issue, args.base_sha)
    else:
        execute(
            args.directory,
            args.stage,
            args.execute_paid,
            args.approved_payload_hash,
            args.crash_after_settlement,
            args.crash_after_pr_accepted,
        )


if __name__ == "__main__":
    main()
