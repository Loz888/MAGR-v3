"""Grading for the V3.5 end-to-end evaluation harness.

The grader only consumes evidence produced by the real runtime: the baseline
reproduction receipt, the public and hidden receipts of the accepted attempt,
the final diff/tree and the sealed review bundle. It never re-runs a model and
never upgrades fixture evidence into a live-agent score.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from .tasks import Task, score_signature

PASS = "pass"
FAIL = "fail"
NOT_REPRODUCED = "not_reproducible"
INFRASTRUCTURE = "infrastructure_error"

_INFRASTRUCTURE_STATUSES = {
    "waiting_for_dependencies",
    "sandbox_error",
}
_NON_REPRODUCED_STATUSES = {
    "baseline_not_reproduced",
}


def _receipt_status(receipt: Mapping[str, Any] | None) -> str | None:
    return None if receipt is None else str(receipt.get("status"))


def _receipt_map(outcome: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {
        str(record.get("receipt_id")): record
        for record in outcome.get("receipts", [])
        if isinstance(record, Mapping)
    }


def _accepted_attempt(outcome: Mapping[str, Any]) -> Mapping[str, Any] | None:
    for attempt in reversed(list(outcome.get("attempts", []))):
        if isinstance(attempt, Mapping) and attempt.get("accepted"):
            return attempt
    return None


def _changed_files(proposal: Mapping[str, Any]) -> list[str]:
    paths = []
    for item in proposal.get("changed_files", []):
        if isinstance(item, Mapping) and item.get("path"):
            paths.append(str(item["path"]))
        elif isinstance(item, str):
            paths.append(item)
    return sorted(set(paths))


def classify(outcome: Mapping[str, Any]) -> str:
    """Separate business failure, infrastructure failure and non-reproduction."""

    status = str(outcome.get("status", ""))
    if status == "waiting_approval":
        return PASS
    if status in _NON_REPRODUCED_STATUSES:
        return NOT_REPRODUCED
    if status in _INFRASTRUCTURE_STATUSES:
        return INFRASTRUCTURE
    return FAIL


def grade(
    task: Task,
    *,
    mode: str,
    outcome: Mapping[str, Any],
    checkpoint: Mapping[str, Any] | None,
    rounds: Sequence[Mapping[str, Any]],
    usage: Mapping[str, Any],
    latency_ms: float,
    publication_executed: bool = False,
) -> dict[str, Any]:
    """Produce one result record; ``mode`` keeps fixture and live evidence apart."""

    baseline = outcome.get("baseline") or {}
    receipts = _receipt_map(outcome)
    accepted = _accepted_attempt(outcome)
    proposal = outcome.get("proposal") or {}
    changed = _changed_files(proposal)
    public = receipts.get(str(accepted.get("public_receipt_id"))) if accepted else None
    hidden = receipts.get(str(accepted.get("verification_receipt_id"))) if accepted else None
    public_passed = _receipt_status(public) == "passed"
    hidden_passed = _receipt_status(hidden) == "passed"
    reproduced = _receipt_status(baseline) == "test_failed"

    expected = set(task.expected_changed_paths)
    allowed = set(task.allowed_paths)
    unrelated = sorted(set(changed) - expected)
    out_of_scope = sorted(set(changed) - allowed)
    missing_changes = sorted(expected - set(changed))
    correct_fix = (
        public_passed
        and hidden_passed
        and not out_of_scope
        and not missing_changes
        and not unrelated
    )

    bundle = outcome.get("review_bundle") or {}
    bundle_diff = str(bundle.get("diff") or "")
    diff_sha256 = hashlib.sha256(bundle_diff.encode()).hexdigest() if bundle else None
    publication_correct = bool(
        bundle
        and not publication_executed
        and bundle.get("base_sha") == task.base_commit
        and diff_sha256 == proposal.get("diff_sha256")
        and bundle_diff == str(outcome.get("diff") or "")
    )
    bundle_files = {
        str(item.get("path"))
        for item in bundle.get("files", [])
        if isinstance(item, Mapping) and item.get("path")
    }
    publication_scope_ok = bundle_files <= allowed

    attempts = list(outcome.get("attempts", []))
    retried = any(
        isinstance(item, Mapping) and item.get("accepted") is False for item in attempts
    )
    first_rejection = next(
        (
            item
            for item in attempts
            if isinstance(item, Mapping) and item.get("accepted") is False
        ),
        None,
    )

    status = classify(outcome)
    if status == PASS and not correct_fix:
        status = FAIL
    if status == PASS and not publication_correct:
        status = FAIL
    if status == PASS and not publication_scope_ok:
        status = FAIL

    failure = None
    if status != PASS:
        failure = {
            "classification": status,
            "outcome_status": outcome.get("status"),
            "stage": outcome.get("failure_stage"),
            "code": outcome.get("failure_code"),
            "reason": outcome.get("reason"),
        }

    return {
        "schema": "mgar-v35-eval-result-v1",
        "mode": mode,
        "gate": "engineering_gate" if mode == "fixture" else "live_agent_gate",
        "task_id": task.task_id,
        "kind": task.kind,
        "adversarial": task.adversarial,
        "resume": task.resume,
        "status": status,
        "attempts": len(attempts),
        "reproduction": {
            "reproduced": reproduced,
            "baseline_status": baseline.get("status"),
            "baseline_failed_tests": list(baseline.get("failed_tests", [])),
            "expected_public_tests": list(task.public_targets),
        },
        "fix": {
            "public_passed": public_passed,
            "hidden_passed": hidden_passed,
            "public_status": _receipt_status(public),
            "hidden_status": _receipt_status(hidden),
            "correct_fix": correct_fix,
            "changed_files": changed,
            "expected_changed_files": list(task.expected_changed_paths),
            "missing_changes": missing_changes,
            "unrelated_changes": unrelated,
            "out_of_scope": out_of_scope,
            "required_paths_satisfied": not missing_changes,
        },
        "publication": {
            "correct": publication_correct,
            "executed": publication_executed,
            "tree_hash": bundle.get("tree_hash"),
            "proposal_id": bundle.get("proposal_id"),
            "diff_sha256": diff_sha256,
            "files": sorted(bundle_files),
            "scope_ok": publication_scope_ok,
        },
        "proposal": {
            "proposal_id": proposal.get("proposal_id"),
            "tested_tree": proposal.get("tested_tree"),
            "diff_sha256": proposal.get("diff_sha256"),
            "attempts": len(attempts),
        },
        "adversarial_observation": {
            "retry_observed": retried,
            "first_rejection_stage": (
                first_rejection.get("feedback_stage") if first_rejection else None
            ),
            "first_rejection_code": (
                first_rejection.get("feedback_code") if first_rejection else None
            ),
            "accepted_attempt": (
                accepted.get("attempt") if accepted is not None else None
            ),
        },
        "rounds": list(rounds),
        "usage": {
            "model_calls": int(usage.get("model_calls", 0)),
            "prompt_tokens": int(usage.get("prompt_tokens", 0)),
            "completion_tokens": int(usage.get("completion_tokens", 0)),
            "cost_micros": int(usage.get("cost_micros", 0)),
            "usage_source": str(usage.get("usage_source", "scripted")),
            "latency_ms": round(float(latency_ms), 3),
        },
        "checkpoint_phase": (checkpoint or {}).get("phase"),
        "failure": failure,
    }


def summarize(results: Sequence[Mapping[str, Any]], *, mode: str) -> dict[str, Any]:
    """Aggregate one mode only; fixture results can never feed the live gate."""

    statuses = Counter(str(item.get("status")) for item in results)
    executed = len(results)
    passed = statuses.get(PASS, 0)
    failures = Counter(
        str((item.get("failure") or {}).get("reason") or (item.get("failure") or {}).get("code"))
        for item in results
        if item.get("status") != PASS
    )
    calls = sum(int((item.get("usage") or {}).get("model_calls", 0)) for item in results)
    costs = sum(int((item.get("usage") or {}).get("cost_micros", 0)) for item in results)
    latency = sum(float((item.get("usage") or {}).get("latency_ms", 0.0)) for item in results)
    by_kind: dict[str, dict[str, Any]] = {}
    for item in results:
        bucket = by_kind.setdefault(
            str(item.get("kind")), {"executed": 0, "passed": 0}
        )
        bucket["executed"] += 1
        if item.get("status") == PASS:
            bucket["passed"] += 1

    live_gate = "NOT_RUN"
    if mode == "live":
        live_gate = PASS if passed == executed and executed > 0 else FAIL

    return {
        "schema": "mgar-v35-eval-summary-v1",
        "mode": mode,
        "gate": "engineering_gate" if mode == "fixture" else "live_agent_gate",
        "live_agent_gate": live_gate,
        "executed": executed,
        "passed": passed,
        "success_rate": (passed / executed) if executed else 0.0,
        "status_counts": dict(sorted(statuses.items())),
        "failure_reasons": {
            str(key): value
            for key, value in sorted(failures.items(), key=lambda pair: str(pair[0]))
        },
        "by_kind": by_kind,
        "avg_model_calls": (calls / executed) if executed else 0.0,
        "total_model_calls": calls,
        "avg_cost_micros": (costs / executed) if executed else 0.0,
        "total_cost_micros": costs,
        "avg_latency_ms": (latency / executed) if executed else 0.0,
        "score_signature": score_signature(results),
    }


def merge_summaries(summaries: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Keep suites separate: a holdout summary is never merged into development."""

    grouped: dict[str, Mapping[str, Any]] = {}
    for summary in summaries:
        mode = str(summary.get("mode"))
        grouped.setdefault(mode, summary)
    return grouped
