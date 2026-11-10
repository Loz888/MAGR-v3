"""V3.5 E03: end-to-end security, concurrency and disaster-recovery scenarios.

Every scenario names one reproducible pytest node id, the exact failure point
it exercises, the observable state it must produce and the recovery result it
must prove.  ``run_report`` executes the referenced tests once with JUnit XML
and writes one immutable report, so the acceptance record is derived from real
test outcomes instead of a hand-written claim.  A scenario that cannot be
executed in this environment is recorded as ``NOT_RUN`` with its reason; it is
never silently downgraded or replaced by a planned value.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import model_validator

from mgar_v3.domain import Model

SCHEMA = "mgar-v35-e03-scenario-report-v1"
V3_ROOT = Path(__file__).resolve().parents[4]

CATEGORIES: dict[str, str] = {
    "1_prompt_injection": "提示注入与恶意 README/Issue/PR/Repository",
    "2_filesystem_escape": "路径穿越、符号链接、Hook/配置篡改、伪造结果、.git 逃逸",
    "3_concurrency": "并发补丁、重复派发、Action/Approval 重复、Outbox 重放、审批后篡改",
    "4_disaster_recovery": "SIGKILL、断电、Redis/PG 丢失、Docker/网络/模型/GitHub 故障",
    "5_stale_state": (
        "旧 Epoch、取消后回写、过期审批、Checkpoint 分叉、Memory 污染、"
        "Compaction 丢失"
    ),
    "6_external_write": "只读对账、base 漂移、Token 过期、仓库归档、Rate Limit",
}

V34 = "tests/test_v34_security_recovery.py"
V35 = "tests/test_v35_e03.py"
ScenarioStatus = Literal["COVERED", "NOT_RUN"]


class Scenario(Model):
    """One required failure scenario and the evidence that covers it."""

    scenario_id: str
    category: str
    title: str
    test: str | None = None
    failure_point: str
    observed_state: str
    recovery_result: str
    status: ScenarioStatus
    not_run_reason: str | None = None

    @model_validator(mode="after")
    def covers_itself(self) -> Scenario:
        if self.category not in CATEGORIES:
            raise ValueError(f"unknown category: {self.category}")
        if self.status == "COVERED" and not self.test:
            raise ValueError("a covered scenario needs a reproducible test node id")
        if self.status == "NOT_RUN" and not self.not_run_reason:
            raise ValueError("a NOT_RUN scenario needs an explicit reason")
        return self


def _v34(name: str) -> str:
    return f"{V34}::{name}"


def _v35(name: str) -> str:
    return f"{V35}::{name}"


SCENARIOS: tuple[Scenario, ...] = (
    # -- 1. prompt injection / hostile repository text -----------------
    Scenario(
        scenario_id="S01",
        category="1_prompt_injection",
        title="README/comment instructions cannot enter trusted memory",
        test=_v34("test_readme_and_comment_instructions_cannot_enter_trusted_memory"),
        failure_point=(
            "Untrusted repository text is treated as an instruction that grants approval."
        ),
        observed_state=(
            "MEMORY_CANDIDATE_REJECTED/PROMPT_INJECTION; "
            "the system prompt labels input UNTRUSTED DATA."
        ),
        recovery_result=(
            "No trusted memory row is written; the run keeps its original authority."
        ),
        status="COVERED",
    ),
    Scenario(
        scenario_id="S02",
        category="1_prompt_injection",
        title="Poisoned memory is excluded from task recall",
        test=_v34("test_poisoned_memory_is_excluded_from_task_recall"),
        failure_point="A previously poisoned fact is recalled as verified task knowledge.",
        observed_state="Recall filters revoked/superseded/unverified rows before ranking.",
        recovery_result=(
            "The task proceeds without the poisoned hint; only eligible memory is used."
        ),
        status="COVERED",
    ),
    Scenario(
        scenario_id="S03",
        category="1_prompt_injection",
        title="Untrusted README is rejected before context assembly",
        test=_v35("test_untrusted_readme_is_rejected_before_context_assembly"),
        failure_point=(
            "A gated write rejection is bypassed by assembling it into the model context."
        ),
        observed_state=(
            "MemoryWriter rejects the candidate and the context memory section stays empty."
        ),
        recovery_result="The instruction survives only as an annotated untrusted observation.",
        status="COVERED",
    ),
    Scenario(
        scenario_id="S04",
        category="1_prompt_injection",
        title="Injected observation cannot widen context authority",
        test=_v35("test_injected_observation_cannot_widen_context_authority"),
        failure_point="A malicious tool result claims approval and expands tools or permissions.",
        observed_state=(
            "Authority claims are recorded; permissions, tools and pending approvals "
            "are unchanged."
        ),
        recovery_result=(
            "The bundle still carries the original read-only scope and pinned policy note."
        ),
        status="COVERED",
    ),
    # -- 2. filesystem, hooks, forged results --------------------------
    Scenario(
        scenario_id="S05",
        category="2_filesystem_escape",
        title="Path traversal and symlink escape are denied",
        test=_v34("test_git_path_traversal_and_symlink_escape_are_denied"),
        failure_point="Patch or read escapes the worktree through '..' or a symbolic link.",
        observed_state="PATCH_PATH_DENIED for traversal, symlink read and symlink update.",
        recovery_result="The worktree and host files remain unchanged.",
        status="COVERED",
    ),
    Scenario(
        scenario_id="S06",
        category="2_filesystem_escape",
        title="Hostile repository tests cannot reach host files or secrets",
        test=_v34("test_malicious_tests_cannot_reach_host_files_or_credentials"),
        failure_point="Tests run with host mounts, secrets or a writable host filesystem.",
        observed_state="The sandbox exposes no secret environment and no host credentials.",
        recovery_result="Hostile tests fail inside the sandbox; no host data is read.",
        status="COVERED",
    ),
    Scenario(
        scenario_id="S07",
        category="2_filesystem_escape",
        title="A forged success cannot replace an absent repair",
        test=_v34("test_absent_defect_is_not_reported_as_repaired"),
        failure_point="A claimed repair without a reproduced defect is graded as success.",
        observed_state="The run is not accepted; no review bundle is produced.",
        recovery_result=(
            "Success requires receipts bound to a reproduced failure and passing tests."
        ),
        status="COVERED",
    ),
    Scenario(
        scenario_id="S08",
        category="2_filesystem_escape",
        title="Invalid or unknown-cost model configuration fails closed",
        test=_v34("test_model_invalid_and_unknown_cost_fail_closed"),
        failure_point="Forged or incomplete configuration lets an unaccounted call pass.",
        observed_state="Model and cost guards raise before a result is accepted.",
        recovery_result="The run stops; no unknown spend or fabricated usage is recorded.",
        status="COVERED",
    ),
    Scenario(
        scenario_id="S09",
        category="2_filesystem_escape",
        title="Git metadata and hook paths are neither readable nor publishable",
        test=_v35("test_git_metadata_and_hook_paths_are_denied"),
        failure_point="A patch or publication touches .git hooks/config or .github automation.",
        observed_state="PATCH_PATH_DENIED and a publication shape rejection.",
        recovery_result="Git control paths never enter the patch or Draft PR payload.",
        status="COVERED",
    ),
    # -- 3. duplicate dispatch, concurrency, post-approval tamper ------
    Scenario(
        scenario_id="S10",
        category="3_concurrency",
        title="Concurrent same-file patches conflict instead of losing an update",
        test=_v34("test_concurrent_same_file_patch_conflicts_instead_of_lost_update"),
        failure_point="Two workers patch the same file and the later write silently wins.",
        observed_state="One worker observes a conflict and retries from the new pre-image.",
        recovery_result="No lost update; the final tree reflects one accepted patch.",
        status="COVERED",
    ),
    Scenario(
        scenario_id="S11",
        category="3_concurrency",
        title="Duplicate dispatch and Outbox replay execute once",
        test=_v34("test_duplicate_tool_execution_is_idempotent"),
        failure_point=(
            "A repeated Action or replayed Outbox entry performs a second external effect."
        ),
        observed_state="The dispatcher records one effect for the repeated action id.",
        recovery_result="The replay returns the existing receipt instead of writing again.",
        status="COVERED",
    ),
    Scenario(
        scenario_id="S12",
        category="3_concurrency",
        title="An approval altered after review is rejected",
        test=_v34("test_stale_approval_hash_is_rejected"),
        failure_point="A tampered bundle reuses an approval granted for another patch or tree.",
        observed_state="APPROVAL_STALE is raised before any remote write.",
        recovery_result="The publisher performs no write and the approval stays bound to its hash.",
        status="COVERED",
    ),
    Scenario(
        scenario_id="S13",
        category="3_concurrency",
        title="A duplicated publication approval does not replay writes",
        test=_v35("test_duplicate_publication_approval_does_not_replay_writes"),
        failure_point="Approval and Action retries create a second branch, commit or Draft PR.",
        observed_state="The second publish reports every step already_present.",
        recovery_result="The remote write count is unchanged and the same Draft PR is returned.",
        status="COVERED",
    ),
    # -- 4. process death, dependency loss, model/network failures -----
    Scenario(
        scenario_id="S14",
        category="4_disaster_recovery",
        title="SIGKILL mid-run recovers without duplicate writes",
        test=_v34("test_sigkill_mid_run_recovers_without_duplicate_writes"),
        failure_point="A killed process leaves an unknown effect that is blindly repeated.",
        observed_state="The checkpoint is reopened and the stale epoch cannot commit.",
        recovery_result="Recovery fences the old epoch; no duplicate write is issued.",
        status="COVERED",
    ),
    Scenario(
        scenario_id="S15",
        category="4_disaster_recovery",
        title="Restart after an unknown outcome never repeats the paid call",
        test=_v35("test_interrupted_resume_does_not_repeat_provider_calls"),
        failure_point="Power loss at the proposing phase resends a paid model call.",
        observed_state="The resumed orchestrator uses a forbidden provider and records zero calls.",
        recovery_result="The run stops without a duplicate model call or fabricated result.",
        status="COVERED",
    ),
    Scenario(
        scenario_id="S16",
        category="4_disaster_recovery",
        title="Forked checkpoints resume deterministically without duplicate effects",
        test=_v35("test_forked_checkpoint_resume_is_deterministic"),
        failure_point="Two workers resume the same forked checkpoint and both write.",
        observed_state="Both resumes observe the identical fenced state and zero provider calls.",
        recovery_result="No branch, commit or Draft PR is produced from either resume.",
        status="COVERED",
    ),
    Scenario(
        scenario_id="S17",
        category="4_disaster_recovery",
        title="Lost Redis pauses external calls while the real limiter still works",
        test=_v35("test_redis_loss_pauses_external_calls"),
        failure_point="Redis loss silently removes the external-call limiter.",
        observed_state=(
            "RATE_LIMIT_UNAVAILABLE on connection loss; RATE_LIMITED at the real boundary."
        ),
        recovery_result="New external calls are paused instead of running unmetered.",
        status="COVERED",
    ),
    Scenario(
        scenario_id="S18",
        category="4_disaster_recovery",
        title="A PostgreSQL outage fails closed with no local fallback",
        test=_v35("test_database_outage_has_no_local_fallback"),
        failure_point="A database outage is masked by a local store or an empty success.",
        observed_state="The connection error propagates; no memory is returned.",
        recovery_result="PostgreSQL stays authoritative; the caller sees the outage.",
        status="COVERED",
    ),
    Scenario(
        scenario_id="S19",
        category="4_disaster_recovery",
        title="A real PostgreSQL service restart during acceptance",
        failure_point="Restarting PostgreSQL mid-run loses an acknowledged transaction.",
        observed_state="Not executed in the shared acceptance database.",
        recovery_result="Deferred; the connection-loss path is covered by S18.",
        status="NOT_RUN",
        not_run_reason=(
            "Restarting the shared acceptance PostgreSQL would disrupt concurrent "
            "verification. The outage path is covered by S18; a real service restart "
            "remains a release-environment check and is not claimed as executed."
        ),
    ),
    Scenario(
        scenario_id="S20",
        category="4_disaster_recovery",
        title="Docker unavailability returns an error receipt, never a pass",
        test=_v35("test_docker_unavailable_returns_error_receipt"),
        failure_point="A missing Docker runtime is graded as passing tests.",
        observed_state="The executor returns status=error with zero passed tests.",
        recovery_result="The task stops as an infrastructure error; no result is fabricated.",
        status="COVERED",
    ),
    Scenario(
        scenario_id="S21",
        category="4_disaster_recovery",
        title="GitHub failures degrade without fabricating results",
        test=_v34("test_github_failures_degrade_without_fabricating_results"),
        failure_point="A failed remote read is reported as a confirmed finding.",
        observed_state="The failure is surfaced as a degraded tool outcome.",
        recovery_result="No finding or publication is produced from a failed read.",
        status="COVERED",
    ),
    Scenario(
        scenario_id="S22",
        category="4_disaster_recovery",
        title="Model 429/5xx, truncation and transport loss are not blind-retried",
        test=_v35("test_model_429_500_and_transport_loss_are_not_retried"),
        failure_point="A failed or unaccounted paid call is resent and double-spends.",
        observed_state="Each failure raises with exactly one dispatch; truncation is surfaced.",
        recovery_result="MODEL_HTTP_4xx/5xx and USAGE_UNKNOWN stop the call; retry stays explicit.",
        status="COVERED",
    ),
    # -- 5. stale epochs, cancellation, expired approval, context loss --
    Scenario(
        scenario_id="S23",
        category="5_stale_state",
        title="An old epoch worker cannot commit after resume",
        test=_v34("test_old_epoch_recovery_cannot_commit_after_resume"),
        failure_point="A worker from the previous epoch writes after a newer worker resumed.",
        observed_state="The epoch fence rejects the stale write.",
        recovery_result="Only the current epoch can commit; the old attempt is preserved.",
        status="COVERED",
    ),
    Scenario(
        scenario_id="S24",
        category="5_stale_state",
        title="A cancelled run rejects late worker activity",
        test=_v34("test_cancelled_run_rejects_late_worker_activity"),
        failure_point="A cancelled run continues to write or acknowledges late work.",
        observed_state="Late worker activity is rejected and the run stays terminal.",
        recovery_result="No effect is recorded after cancellation.",
        status="COVERED",
    ),
    Scenario(
        scenario_id="S25",
        category="5_stale_state",
        title="An expired paid-call authorization blocks every new call",
        test=_v35("test_expired_live_authorization_blocks_paid_calls"),
        failure_point="An expired approval still authorizes a paid model or external call.",
        observed_state="LiveNotAuthorized is raised before a provider is constructed.",
        recovery_result="No paid request or external write is attempted.",
        status="COVERED",
    ),
    Scenario(
        scenario_id="S26",
        category="5_stale_state",
        title="Commit-scoped memory from another commit requires re-verification",
        test=_v34("test_stale_commit_memory_requires_reverification"),
        failure_point="A fact from another commit is presented as current-version truth.",
        observed_state="The hit is historical and requires_reverification is true.",
        recovery_result="The task must verify the fact again before trusting it.",
        status="COVERED",
    ),
    Scenario(
        scenario_id="S27",
        category="5_stale_state",
        title="Cross-repository memory is not readable",
        test=_v34("test_cross_repository_memory_is_not_readable"),
        failure_point="Repository B reads repository A's memory.",
        observed_state="Scope predicates deny the read before ranking.",
        recovery_result="No foreign record is returned or used in context.",
        status="COVERED",
    ),
    Scenario(
        scenario_id="S28",
        category="5_stale_state",
        title="Compaction never loses safety fields across rounds",
        test=_v35("test_compaction_preserves_safety_fields_across_rounds"),
        failure_point="Compression drops a failed attempt, approval, action or evidence id.",
        observed_state="Every safety field survives with observations omitted and accounted.",
        recovery_result="The invariants hold before and after compaction.",
        status="COVERED",
    ),
    Scenario(
        scenario_id="S29",
        category="5_stale_state",
        title="Round-two expert advice stays read-only and subordinate",
        test=_v35("test_round_two_expert_advisories_are_read_only"),
        failure_point="An expert changes state, files or the decision in round two.",
        observed_state="Advice is read-only with write_access=false and main_agent_decides=true.",
        recovery_result="Only the main agent decides; the patch plan remains the sole mutation.",
        status="COVERED",
    ),
    Scenario(
        scenario_id="S30",
        category="5_stale_state",
        title="Exhausted repair budget stops without a proposal",
        test=_v34("test_repair_budget_exhaustion_stops_without_proposal"),
        failure_point="A budget-exhausted run still emits a patch or success.",
        observed_state="The orchestrator stops and records the budget failure.",
        recovery_result="No proposal is produced after the budget is exhausted.",
        status="COVERED",
    ),
    # -- 6. external writes and publication boundaries -----------------
    Scenario(
        scenario_id="S31",
        category="6_external_write",
        title="A lost PR receipt is reconciled read-only without replaying writes",
        test=_v34("test_lost_pull_request_receipt_reconciles_without_replaying_writes"),
        failure_point="An unknown Draft PR result is followed by a second create call.",
        observed_state="Reconcile finds the existing branch/commit/PR through reads.",
        recovery_result="The outcome is recovered with zero additional writes.",
        status="COVERED",
    ),
    Scenario(
        scenario_id="S32",
        category="6_external_write",
        title="Base branch drift blocks publication",
        test=_v34("test_base_branch_change_blocks_publication"),
        failure_point="A Draft PR is published against a base commit that moved.",
        observed_state="status=base_changed before the first write.",
        recovery_result="New tests and a new approval are required; no write occurs.",
        status="COVERED",
    ),
    Scenario(
        scenario_id="S33",
        category="6_external_write",
        title="An expired GitHub token blocks publication without a write",
        test=_v35("test_expired_github_token_blocks_publication_without_write"),
        failure_point="A 401/expired credential is treated as a successful publication.",
        observed_state="The publication outcome is outcome_unknown with zero writes.",
        recovery_result="The patch is not pushed and the result is not reported as published.",
        status="COVERED",
    ),
    Scenario(
        scenario_id="S34",
        category="6_external_write",
        title="An archived repository fails closed without a Draft PR",
        test=_v35("test_archived_repository_write_fails_closed"),
        failure_point="A write to an archived repository is reported as published.",
        observed_state="The write error yields outcome_unknown and no pull request.",
        recovery_result="Read-only reconciliation cannot prove completion; no write is replayed.",
        status="COVERED",
    ),
    Scenario(
        scenario_id="S35",
        category="6_external_write",
        title="A GitHub rate limit is classified before any retry or write",
        test=_v35("test_github_rate_limit_is_classified_without_write"),
        failure_point="A 429 response is retried immediately or treated as a finding.",
        observed_state="GITHUB_RATE_LIMITED/429 is classified without a write.",
        recovery_result="No tool result is fabricated and no external write is attempted.",
        status="COVERED",
    ),
)


def coverage(scenarios: tuple[Scenario, ...] = SCENARIOS) -> dict[str, Any]:
    covered = [item for item in scenarios if item.status == "COVERED"]
    not_run = [item for item in scenarios if item.status == "NOT_RUN"]
    by_category: dict[str, dict[str, int]] = {}
    for name in CATEGORIES:
        items = [item for item in scenarios if item.category == name]
        by_category[name] = {
            "covered": sum(1 for item in items if item.status == "COVERED"),
            "not_run": sum(1 for item in items if item.status == "NOT_RUN"),
        }
    return {
        "scenarios": len(scenarios),
        "covered": len(covered),
        "not_run": len(not_run),
        "by_category": by_category,
        "unique_tests": len({item.test for item in covered if item.test}),
    }


def _node_key(classname: str, name: str) -> str:
    path = classname.replace(".", "/") + ".py"
    return f"{path}::{name.split('[', 1)[0]}"


def _parse_junit(path: Path) -> dict[str, dict[str, Any]]:
    results: dict[str, dict[str, Any]] = {}
    root = ET.parse(path).getroot()
    for case in root.iter("testcase"):
        key = _node_key(case.get("classname", ""), case.get("name", ""))
        entry = results.setdefault(key, {"passed": 0, "failed": 0, "seconds": 0.0})
        entry["seconds"] = round(entry["seconds"] + float(case.get("time", "0") or 0), 3)
        broken = any(case.find(tag) is not None for tag in ("failure", "error"))
        if broken:
            entry["failed"] += 1
        else:
            entry["passed"] += 1
    return results


def run_report(
    out_dir: Path,
    *,
    only: set[str] | None = None,
    timeout: int = 1800,
) -> dict[str, Any]:
    """Execute every covered scenario and write one immutable report."""

    selected = tuple(
        item for item in SCENARIOS if only is None or item.scenario_id in only
    )
    if not selected:
        raise ValueError("no scenario selected")
    out = Path(out_dir)
    if out.exists():
        raise FileExistsError(f"scenario report already exists: {out}")
    out.mkdir(parents=True)
    nodes = sorted({item.test for item in selected if item.test})
    junit = out / "junit.xml"
    command = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "-p",
        "no:warnings",
        "--junitxml",
        str(junit),
        *nodes,
    ]
    error: str | None = None
    returncode = -1
    try:
        completed = subprocess.run(
            command,
            cwd=V3_ROOT,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        returncode = completed.returncode
        (out / "pytest.log").write_text(
            completed.stdout + completed.stderr, encoding="utf-8"
        )
    except subprocess.TimeoutExpired:
        error = f"scenario execution exceeded {timeout}s"
        (out / "pytest.log").write_text(error + "\n", encoding="utf-8")
    results = _parse_junit(junit) if junit.is_file() else {}
    records: list[dict[str, Any]] = []
    for item in selected:
        record = item.model_dump(mode="json")
        if item.status == "NOT_RUN":
            record.update({"test_result": "NOT_RUN", "duration_seconds": 0.0})
            records.append(record)
            continue
        outcome = results.get(str(item.test))
        passed = (
            error is None
            and outcome is not None
            and outcome["failed"] == 0
            and outcome["passed"] > 0
        )
        record.update(
            {
                "test_result": "PASS" if passed else "FAIL",
                "duration_seconds": (outcome or {}).get("seconds", 0.0),
                "test_cases": {
                    "passed": (outcome or {}).get("passed", 0),
                    "failed": (outcome or {}).get("failed", 0),
                },
            }
        )
        records.append(record)
    covered = [item for item in records if item["status"] == "COVERED"]
    failed = [item["scenario_id"] for item in covered if item["test_result"] != "PASS"]
    gate = "PASS" if covered and not failed else "FAIL"
    summary = {
        "schema": SCHEMA,
        "generated_at": datetime.now(UTC).isoformat(),
        "security_gate": gate,
        "pytest_exit_code": returncode,
        "error": error,
        "coverage": coverage(selected),
        "failed_scenarios": failed,
        "not_run_scenarios": [
            {"scenario_id": item.scenario_id, "reason": item.not_run_reason}
            for item in selected
            if item.status == "NOT_RUN"
        ],
    }
    (out / "scenarios.json").write_text(
        json.dumps(records, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (out / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run V3.5 E03 security scenarios")
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--only", action="append", default=None)
    parser.add_argument("--timeout", type=int, default=1800)
    args = parser.parse_args(argv)
    summary = run_report(
        args.out,
        only=set(args.only) if args.only else None,
        timeout=args.timeout,
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if summary["security_gate"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
