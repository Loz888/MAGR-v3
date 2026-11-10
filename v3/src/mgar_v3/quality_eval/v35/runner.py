"""Runnable V3.5 end-to-end harness.

One task execution follows the product chain rather than a bespoke one-shot
script:

``create`` (byte-exact snapshot + real baseline reproduction)
→ ``run`` (``AuthorizedRepairOrchestrator`` over the real R03 sandbox)
→ ``patch`` (R02 authorized worktree + patch limits)
→ ``test`` (public suite, then independent hidden verification)
→ ``verify`` (sealed review bundle waiting for approval, never published).

Fixture mode uses a scripted structured provider and is engineering evidence
only. Live mode requires an explicit authorization file and a caller-supplied
real provider; without them every task is recorded as ``NOT_RUN``.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid5

from mgar_v3.domain import digest
from mgar_v3.findings.model import FindingEvidence
from mgar_v3.quality_eval.v35 import grader
from mgar_v3.quality_eval.v35 import tasks as tasks_mod
from mgar_v3.quality_eval.v35.tasks import Task
from mgar_v3.repair.handoff import (
    HANDOFF_SCHEMA,
    RepairAuthorization,
    RepairHandoff,
    ReproductionProof,
)
from mgar_v3.repair.repair_agent import PatchPlan, RepairBudget
from mgar_v3.repair.runtime import SnapshotRepairRuntime
from mgar_v3.repair.sandbox_executor import PythonAdapter, SandboxExecutor, SandboxLimits
from mgar_v3.repair.workspace import tree
from mgar_v3.repair.worktree import FileEdit, PatchLimits
from mgar_v3.repository_analysis.domain import AnalysisLimits
from mgar_v3.repository_analysis.workspace import (
    SNAPSHOT_VERSION,
    RepositorySnapshot,
)
from mgar_v3.workbench.orchestrator import (
    AuthorizedRepairJob,
    AuthorizedRepairOrchestrator,
    JsonRepairLedger,
    RepairCheckpoint,
)

V3_ROOT = Path(__file__).resolve().parents[4]
EXPERIMENT = V3_ROOT / "experiments" / "v35-harness"
NAMESPACE = UUID("9f2d9a5c-6d70-5f4d-9c4a-3b1c1b0f7e01")
ACTOR = "v35-e01-harness"
AUTHORIZATION_SECONDS = 600

SANDBOX_LIMITS = SandboxLimits(
    max_files=256,
    max_total_bytes=4_194_304,
    max_file_bytes=1_048_576,
    workspace_mb=128,
    memory_mb=1024,
    timeout_seconds=120,
)
PATCH_LIMITS = PatchLimits(
    max_files=4,
    max_changed_lines=400,
    max_file_bytes=524_288,
    max_total_bytes=1_048_576,
    max_deleted_files=0,
)


class LiveNotAuthorized(RuntimeError):
    """Live evaluation requires an explicit authorization file."""


class ScriptedRepairProvider:
    """Deterministic structured provider used for engineering-only evidence."""

    def __init__(self, plans: Sequence[PatchPlan], *, label: str = "scripted") -> None:
        if not plans:
            raise ValueError("a scripted provider needs at least one plan")
        self.plans = list(plans)
        self.label = label
        self.calls: list[dict[str, Any]] = []

    def propose(self, prompt: Any) -> PatchPlan:
        index = len(self.calls)
        self.calls.append(
            {
                "attempt": int(getattr(prompt, "attempt", index + 1)),
                "feedback": _feedback_payload(getattr(prompt, "feedback", None)),
                "visible_files": len(getattr(prompt, "files", {}) or {}),
                "summary": None,
            }
        )
        plan = self.plans[min(index, len(self.plans) - 1)]
        self.calls[-1]["summary"] = plan.summary
        return plan.model_copy(deep=True)

    def usage(self) -> dict[str, Any]:
        return {
            "model_calls": len(self.calls),
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "cost_micros": 0,
            "usage_source": "scripted",
        }


class TracingProvider:
    """Wrap a provider and record the exact observation each attempt received."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.calls: list[dict[str, Any]] = []

    def propose(self, prompt: Any) -> PatchPlan:
        files = dict(getattr(prompt, "files", {}) or {})
        leaked = [path for path in files if path.startswith("verification/")]
        if leaked:
            raise RuntimeError(f"hidden verification leaked into the prompt: {leaked}")
        plan = self.inner.propose(prompt)
        self.calls.append(
            {
                "attempt": int(getattr(prompt, "attempt", len(self.calls) + 1)),
                "feedback": _feedback_payload(getattr(prompt, "feedback", None)),
                "visible_files": len(files),
                "summary": plan.summary,
            }
        )
        return plan  # type: ignore[no-any-return]

    def usage(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"model_calls": len(self.calls)}
        if hasattr(self.inner, "usage"):
            payload.update(dict(self.inner.usage()))
        else:
            calls = getattr(self.inner, "calls", [])
            prompt_tokens = sum(int(getattr(call, "prompt_tokens", 0) or 0) for call in calls)
            completion_tokens = sum(
                int(getattr(call, "completion_tokens", 0) or 0) for call in calls
            )
            cost = sum(int(getattr(call, "cost_micros", 0) or 0) for call in calls)
            payload.update(
                {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "cost_micros": cost,
                    "usage_source": "actual_provider_usage" if cost else "provider_reported",
                }
            )
        return payload


def _feedback_payload(feedback: Any) -> list[dict[str, Any]]:
    if not feedback:
        return []
    items = feedback if isinstance(feedback, (list, tuple)) else [feedback]
    payload = []
    for item in items:
        if isinstance(item, Mapping):
            payload.append(dict(item))
            continue
        payload.append(
            {
                "attempt": getattr(item, "attempt", None),
                "stage": getattr(item, "stage", None),
                "code": getattr(item, "code", None),
                "message": getattr(item, "message", None),
                "failed_tests": list(getattr(item, "failed_tests", []) or []),
            }
        )
    return payload


def default_sandbox() -> SandboxExecutor:
    return SandboxExecutor(limits=SANDBOX_LIMITS)


def snapshot_for(task: Task) -> RepositorySnapshot:
    files: dict[str, str] = {}
    binary_paths: list[str] = []
    for path in tasks_mod.snapshot_files(task.snapshot_dir):
        relative = path.relative_to(task.snapshot_dir).as_posix()
        try:
            files[relative] = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            binary_paths.append(relative)
    files.update(task.public_sources())
    encoded = {path: content.encode("utf-8") for path, content in files.items()}
    file_sha256 = {
        path: hashlib.sha256(content).hexdigest() for path, content in encoded.items()
    }
    digest_tree = tree(files)
    manifest_hash = digest(
        {
            "task_id": task.task_id,
            "tree": digest_tree,
            "files": file_sha256,
        }
    )
    snapshot = RepositorySnapshot(
        snapshot_version=SNAPSHOT_VERSION,
        repository=task.repository,
        commit=task.base_commit,
        files=files,
        binary_paths=sorted(binary_paths),
        file_sha256=file_sha256,
        total_bytes=sum(len(content) for content in encoded.values()),
        manifest_hash=manifest_hash,
        tree=digest_tree,
        snapshot_hash="0" * 64,
    )
    return snapshot.model_copy(update={"snapshot_hash": snapshot.computed_hash()})


def _analysis_limits(task: Task) -> AnalysisLimits:
    return AnalysisLimits(
        max_model_calls=int(task.budget["max_model_calls"]),
        max_tool_calls=20,
        max_input_tokens=120_000,
        max_cost_micros=int(task.budget["max_cost_micros"]),
        max_seconds=int(task.budget["deadline_seconds"]),
    )


def _repair_budget(task: Task) -> RepairBudget:
    return RepairBudget(
        max_attempts=int(task.budget["max_attempts"]),
        max_test_runs=int(task.budget["max_test_runs"]),
        deadline_seconds=float(task.budget["deadline_seconds"]),
    )


def _task_id(task: Task) -> UUID:
    return uuid5(NAMESPACE, task.task_id)


def _finding_id(task: Task) -> str:
    return digest({"task": task.task_id, "commit": task.base_commit, "kind": task.kind})


def build_handoff(
    task: Task,
    snapshot: RepositorySnapshot,
    *,
    baseline_receipt_id: str,
    baseline_log_sha256: str,
    mode: str,
) -> RepairHandoff:
    limits = _analysis_limits(task)
    authorization = RepairAuthorization(
        approved_by=ACTOR,
        expires_at=datetime(2030, 1, 1, tzinfo=UTC),
        allowed_repositories=[task.repository],
        limits=limits,
        allow_repair=True,
    )
    evidence = FindingEvidence(
        evidence_id=digest({"task": task.task_id, "public": task.public_targets}),
        kind="test_run",
        source_uri=f"receipt://{baseline_receipt_id}",
        description="reproduced: /usr/bin/python3 /opt/mgar/runner.py --root /workspace",
        content_hash=baseline_receipt_id,
        location=None,
    )
    payload = dict(
        schema_version=HANDOFF_SCHEMA,
        handoff_id="0" * 64,
        repository=task.repository,
        base_commit=task.base_commit,
        finding_id=_finding_id(task),
        parent_run_id=_task_id(task),
        parent_report_hash=digest({"task": task.task_id, "snapshot": snapshot.snapshot_hash}),
        evidence=[evidence],
        reproduction=ReproductionProof(
            kind="reproduced",
            commit=task.base_commit,
            evidence_ids=[evidence.evidence_id],
            receipt_id=baseline_receipt_id,
            reproduce_command="python -m pytest -q tests",
        ),
        objective=task.prompt,
        allowed_paths=list(task.allowed_paths),
        execution_budget=limits,
        authorization=authorization,
        authorization_hash=authorization.authorization_hash,
        execution_mode="live" if mode == "live" else "fixture",
        finding_lifecycle="reproduced",
    )
    handoff = RepairHandoff(**payload)
    return handoff.model_copy(update={"handoff_id": handoff.computed_id()})


def _gold_plan(task: Task, snapshot: RepositorySnapshot, *, summary: str) -> PatchPlan:
    edits = []
    for target, content in task.gold_sources().items():
        current = snapshot.files[target]
        edits.append(
            FileEdit(
                path=target,
                operation="update",
                content=content,
                expected_sha256=hashlib.sha256(current.encode()).hexdigest(),
            )
        )
    return PatchPlan(summary=summary, edits=edits)


def _partial_gold_plan(task: Task, snapshot: RepositorySnapshot) -> PatchPlan:
    """A plausible first attempt that leaves the decisive fix for a retry."""

    scenario = task.first_patch or {}
    replacements = scenario.get("replacements") or []
    gold = task.gold_sources()
    contents: dict[str, str] = {}
    for replacement in replacements:
        path = str(replacement["path"])
        find = str(replacement["find"])
        replace = str(replacement["replace"])
        content = contents.get(path, gold[path])
        if find not in content:
            raise RuntimeError(f"adversarial fixture could not revert {path}")
        contents[path] = content.replace(find, replace, 1)
    edits = []
    for path, content in contents.items():
        current = snapshot.files[path]
        edits.append(
            FileEdit(
                path=path,
                operation="update",
                content=content,
                expected_sha256=hashlib.sha256(current.encode()).hexdigest(),
            )
        )
    return PatchPlan(
        summary=str(scenario.get("summary") or "Apply a partial repair."),
        edits=edits,
    )


def scripted_provider(task: Task, snapshot: RepositorySnapshot) -> ScriptedRepairProvider:
    gold = _gold_plan(
        task,
        snapshot,
        summary=f"Apply the complete reference repair for {task.task_id}.",
    )
    if task.adversarial:
        return ScriptedRepairProvider(
            [_partial_gold_plan(task, snapshot), gold],
            label="scripted_adversarial_retry",
        )
    return ScriptedRepairProvider([gold])


def _preflight(
    task: Task,
    snapshot: RepositorySnapshot,
    sandbox: SandboxExecutor,
) -> Any:
    receipt, _ = sandbox.run(snapshot.files, PythonAdapter())
    return receipt


def _rounds(
    checkpoint: RepairCheckpoint,
    ledger: JsonRepairLedger,
    snapshot: RepositorySnapshot,
) -> list[dict[str, Any]]:
    receipts = {
        (str(item.stage), int(item.attempt)): item for item in checkpoint.receipts
    }
    patches = {int(item.attempt): item for item in checkpoint.patches}
    plans = {int(item.attempt): item for item in checkpoint.plans}
    rounds = []
    for attempt in checkpoint.attempts:
        number = int(attempt.attempt)
        planned = plans.get(number)
        plan_payload: dict[str, Any] = {}
        if planned is not None:
            plan = PatchPlan.model_validate_json(
                ledger.read_artifact(planned.artifact_hash)
            )
            plan_payload = {
                "summary": plan.summary,
                "edits": [edit.path for edit in plan.edits],
            }
        public = receipts.get(("public_tests", number))
        hidden = receipts.get(("verification", number))
        patch = patches.get(number)
        public_receipt = (public.receipt or {}) if public is not None else {}
        public_test_count = (
            int(public_receipt.get("passed_tests") or 0)
            + len(public_receipt.get("failed_tests") or [])
            if public is not None
            else None
        )
        feedback = None
        if not attempt.accepted:
            feedback = {
                "stage": attempt.feedback_stage,
                "code": attempt.feedback_code,
                "message": attempt.feedback_message,
            }
        rounds.append(
            {
                "attempt": number,
                "reason": plan_payload.get("summary"),
                "tool_calls": [
                    {"tool": "repair.provider.propose", "attempt": number},
                    {"tool": "repair.worktree.apply_patch", "attempt": number},
                    {
                        "tool": "sandbox.run_public_tests",
                        "attempt": number,
                        "tests": public_test_count,
                    },
                    *(
                        [
                            {
                                "tool": "sandbox.run_hidden_verification",
                                "attempt": number,
                            }
                        ]
                        if public is not None and public.passed
                        else []
                    ),
                ],
                "observation": {
                    "visible_files": len(snapshot.files),
                    "hidden_files_visible": 0,
                    "feedback": _round_feedback(feedback),
                },
                "patch": {
                    "patch_id": patch.patch_id if patch else None,
                    "tree_before": patch.tree_before if patch else None,
                    "tree_after": patch.tree_after if patch else None,
                    "files": plan_payload.get("edits", []),
                },
                "test": {
                    "public": _receipt_payload(public),
                    "hidden": _receipt_payload(hidden),
                },
                "feedback": feedback,
                "retry": any(
                    int(item.attempt) > number for item in checkpoint.attempts
                ),
                "accepted": bool(attempt.accepted),
            }
        )
    return rounds


def _round_feedback(feedback: Mapping[str, Any] | None) -> list[str]:
    if not feedback:
        return []
    return [
        item
        for item in (
            feedback.get("message"),
            feedback.get("code"),
        )
        if item
    ]


def _receipt_payload(record: Any) -> dict[str, Any] | None:
    if record is None:
        return None
    receipt = record.receipt or {}
    return {
        "receipt_id": record.receipt_id,
        "status": record.status,
        "passed": record.passed,
        "failed_tests": list(receipt.get("failed_tests", [])),
        "passed_tests": receipt.get("passed_tests"),
        "exit_code": receipt.get("exit_code"),
        "log_sha256": record.log_sha256,
    }


def run_task(
    task: Task,
    *,
    experiment: Path = EXPERIMENT,
    run_dir: Path,
    mode: str = "fixture",
    sandbox: SandboxExecutor | None = None,
    provider: Any | None = None,
) -> dict[str, Any]:
    """Execute one task through the real orchestration chain and grade it."""

    if mode not in {"fixture", "live"}:
        raise ValueError(f"unknown evaluation mode: {mode}")
    if mode == "live" and provider is None:
        raise LiveNotAuthorized(
            "live mode requires an explicit authorization and a real model provider"
        )
    run_dir.mkdir(parents=True, exist_ok=True)
    executor = sandbox or default_sandbox()
    snapshot = snapshot_for(task)

    started = time.monotonic()
    baseline = _preflight(task, snapshot, executor)
    handoff = build_handoff(
        task,
        snapshot,
        baseline_receipt_id=baseline.receipt_id,
        baseline_log_sha256=baseline.log_sha256,
        mode=mode,
    )
    tracing = TracingProvider(provider) if provider is not None else None
    live_provider = tracing if tracing is not None else scripted_provider(task, snapshot)
    ledger = JsonRepairLedger(run_dir / "ledger")
    orchestrator = AuthorizedRepairOrchestrator(
        ledger=ledger,
        allowed_actors=[ACTOR],
        runtime=SnapshotRepairRuntime(executor=executor),
    )
    job = AuthorizedRepairJob(
        task_id=_task_id(task),
        finding_id=_finding_id(task),
        finding_title=task.task_id,
        finding_commit=task.base_commit,
        handoff=handoff,
        snapshot=snapshot,
        verification_files=task.hidden_sources(),
        verification_tests=list(task.hidden_tests),
        budget=_repair_budget(task),
        patch_limits=PATCH_LIMITS,
    )
    outcome = orchestrator.run(
        job,
        live_provider,
        allow_unknown_repeat=False,
    )
    checkpoint = ledger.load(_task_id(task), _finding_id(task))
    rounds = _rounds(checkpoint, ledger, snapshot) if checkpoint is not None else []
    latency_ms = (time.monotonic() - started) * 1000
    payload = outcome.model_dump(mode="json")
    usage = live_provider.usage()
    result = grader.grade(
        task,
        mode=mode,
        outcome=payload,
        checkpoint=checkpoint.model_dump(mode="json") if checkpoint else None,
        rounds=rounds,
        usage=usage,
        latency_ms=latency_ms,
    )
    result["execution"] = {
        "entrypoint": "AuthorizedRepairOrchestrator",
        "runtime": "SnapshotRepairRuntime",
        "sandbox_adapter": "python-unittest-json-v1",
        "snapshot_hash": snapshot.snapshot_hash,
        "handoff_id": handoff.handoff_id,
        "preflight": {
            "receipt_id": baseline.receipt_id,
            "status": baseline.status,
            "passed": baseline.status == "passed",
            "failed_tests": list(baseline.failed_tests),
            "passed_tests": baseline.passed_tests,
            "exit_code": baseline.exit_code,
            "log_sha256": baseline.log_sha256,
            "image_id": baseline.image_id,
            "adapter": baseline.adapter,
        },
    }
    return result


def simulate_interrupted_resume(
    task: Task,
    *,
    run_dir: Path,
    experiment: Path = EXPERIMENT,
    mode: str = "fixture",
) -> dict[str, Any]:
    """Reopen a checkpoint at ``proposing`` and prove no paid call is repeated."""

    ledger = JsonRepairLedger(run_dir / "ledger")
    task_id = _task_id(task)
    finding_id = _finding_id(task)
    checkpoint = ledger.load(task_id, finding_id)
    if checkpoint is None:
        raise RuntimeError("resume simulation requires a completed checkpoint")
    interrupted_attempt = max(1, checkpoint.attempt)
    interrupted = checkpoint.model_copy(
        update={
            "phase": "proposing",
            "node": "provider",
            "attempt": interrupted_attempt,
            "plans": [item for item in checkpoint.plans if item.attempt < interrupted_attempt],
            "rejections": [
                item for item in checkpoint.rejections if item.attempt < interrupted_attempt
            ],
            "patches": [
                item for item in checkpoint.patches if item.attempt < interrupted_attempt
            ],
            "receipts": [
                item
                for item in checkpoint.receipts
                if item.stage == "baseline" or item.attempt < interrupted_attempt
            ],
            "attempts": [
                item for item in checkpoint.attempts if item.attempt < interrupted_attempt
            ],
            "outcome": None,
            "review_bundle": None,
            "bundle_artifact": None,
        }
    )
    ledger.save(interrupted)

    class ForbiddenProvider:
        calls = 0

        def propose(self, prompt: Any) -> PatchPlan:
            self.calls += 1
            raise AssertionError("provider must not be called after an unknown outcome")

    forbidden = ForbiddenProvider()
    snapshot = snapshot_for(task)
    baseline = checkpoint.baseline or {}
    handoff = build_handoff(
        task,
        snapshot,
        baseline_receipt_id=str(baseline.get("receipt_id") or "0" * 64),
        baseline_log_sha256=str(baseline.get("log_sha256") or "0" * 64),
        mode=mode,
    )
    job = AuthorizedRepairJob(
        task_id=task_id,
        finding_id=finding_id,
        finding_title=task.task_id,
        finding_commit=task.base_commit,
        handoff=handoff,
        snapshot=snapshot,
        verification_files=task.hidden_sources(),
        verification_tests=list(task.hidden_tests),
        budget=_repair_budget(task),
        patch_limits=PATCH_LIMITS,
    )
    outcome = AuthorizedRepairOrchestrator(
        ledger=ledger,
        allowed_actors=[ACTOR],
        runtime=SnapshotRepairRuntime(executor=default_sandbox()),
    ).run(job, forbidden)
    return {
        "provider_calls": forbidden.calls,
        "status": outcome.status,
        "failure_code": outcome.failure_code,
        "reason": outcome.reason,
        "interrupted_attempt": interrupted_attempt,
    }


def write_report(
    results: Sequence[Mapping[str, Any]],
    *,
    out_dir: Path,
    mode: str,
    suite: str,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Write one immutable report directory; existing reports are never replaced."""

    results_path = out_dir / "results.json"
    summary_path = out_dir / "summary.json"
    if results_path.exists() or summary_path.exists():
        raise FileExistsError(f"report already exists: {out_dir}")
    out_dir.mkdir(parents=True, exist_ok=True)
    summary = grader.summarize(results, mode=mode)
    summary["suite"] = suite
    summary["generated_at"] = datetime.now(UTC).isoformat()
    if extra:
        summary.update(dict(extra))
    (out_dir / "results.json").write_text(
        json.dumps(list(results), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (out_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return summary
