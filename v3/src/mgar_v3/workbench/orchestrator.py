"""Authorized multi-round audit-and-repair orchestration (V3.5 R03).

The orchestrator is the only place where a Workbench finding may become a
repository write.  It is strict by construction:

* a handoff must carry an unexpired, actor-scoped ``RepairAuthorization`` with
  ``allow_repair`` enabled, otherwise nothing is executed;
* every attempt crosses the R02 ``SnapshotRepairRuntime``, so tests only run
  inside the restricted R03 container and hidden tests never reach the model;
* a checkpoint is appended to the Task/Run ledger at every node and transaction
  boundary, together with the plan, patch id, receipts, failure classification
  and content-addressed artifact hash;
* a restart replays the recorded plans instead of calling the provider again,
  and an interrupted provider call is refused rather than repeated blindly;
* success only yields a ``waiting_approval`` Review Bundle.  No Draft PR is
  created here.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol, cast
from uuid import UUID

from pydantic import Field

from mgar_v3.domain import DomainError, Model, digest
from mgar_v3.repair.handoff import RepairHandoff
from mgar_v3.repair.pr_delivery import PublicationFile, seal_bundle
from mgar_v3.repair.repair_agent import (
    PatchPlan,
    PatchProposalRejected,
    RepairAttempt,
    RepairBudget,
    RepairProvider,
    RepairReport,
)
from mgar_v3.repair.runtime import SnapshotRepairRuntime
from mgar_v3.repair.sandbox_executor import TestReceipt as SandboxTestReceipt
from mgar_v3.repair.worktree import PatchLimits, PatchResult
from mgar_v3.repository_analysis.workspace import RepositorySnapshot
from mgar_v3.workbench.store import WorkbenchStore

CheckpointPhase = Literal[
    "authorized",
    "baseline",
    "proposing",
    "patching",
    "public_tests",
    "verification",
    "waiting_approval",
    "failed",
    "cancelled",
    "base_advanced",
]
RepairOutcomeStatus = Literal[
    "waiting_approval",
    "baseline_not_reproduced",
    "waiting_for_dependencies",
    "sandbox_error",
    "exhausted",
    "budget_exhausted",
    "deadline_exceeded",
    "provider_error",
    "cancelled",
    "base_advanced",
]
ReceiptStage = Literal["baseline", "public_tests", "verification"]
_TERMINAL = frozenset(
    {"waiting_approval", "failed", "cancelled", "base_advanced"}
)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _canonical(value: Any) -> bytes:
    if isinstance(value, Model):
        value = value.model_dump(mode="json")
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _tree_hash(files: Mapping[str, str]) -> str:
    """Content tree hash used by the Workbench fix and Review Bundle records.

    The Git tree id is only 40 hex characters; the Workbench contract stores a
    SHA-256 over the per-file hashes instead, so both have to be recorded.
    """

    hashes = {
        path: hashlib.sha256(content.encode()).hexdigest()
        for path, content in sorted(files.items())
    }
    return digest(hashes)


class RepairCancelToken:
    """A cooperative cancellation signal checked at every node boundary."""

    def __init__(self) -> None:
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def is_set(self) -> bool:
        return self._cancelled


class RepairCancelled(Exception):
    """A cancellation observed at a node boundary, with a possible late receipt."""

    def __init__(
        self,
        stage: str,
        attempt: int,
        receipt: SandboxTestReceipt | None = None,
    ) -> None:
        super().__init__(f"repair cancelled at {stage}")
        self.stage = stage
        self.attempt = attempt
        self.receipt = receipt


class _LedgerBudgetExhausted(Exception):
    pass


class PlannedAttempt(Model):
    attempt: int
    summary: str
    artifact_hash: str


class RejectionRecord(Model):
    attempt: int
    stage: str
    code: str
    message: str
    retryable: bool


class PatchRecord(Model):
    attempt: int
    patch_id: str
    tree_before: str
    tree_after: str
    artifact_hash: str


class ReceiptRecord(Model):
    stage: ReceiptStage
    attempt: int
    receipt_id: str
    status: str
    passed: bool
    log_sha256: str
    artifact_hash: str
    receipt: dict[str, Any]


class AttemptRecord(Model):
    attempt: int
    accepted: bool
    patch_id: str | None
    public_receipt_id: str | None
    verification_receipt_id: str | None
    feedback_stage: str | None
    feedback_code: str | None
    feedback_message: str | None
    artifact_hash: str


class RepairCheckpoint(Model):
    """The durable state of one authorized repair, appended to the ledger."""

    schema_version: Literal["v35-repair-checkpoint-v1"] = "v35-repair-checkpoint-v1"
    seq: int = 0
    task_id: UUID
    finding_id: str
    handoff_id: str
    repository: str
    base_commit: str
    snapshot_hash: str
    phase: CheckpointPhase = "authorized"
    node: str = "start"
    attempt: int = 0
    test_runs: int = 0
    budget: dict[str, Any]
    started_at: str
    updated_at: str
    baseline: dict[str, Any] | None = None
    plans: list[PlannedAttempt] = Field(default_factory=list)
    rejections: list[RejectionRecord] = Field(default_factory=list)
    patches: list[PatchRecord] = Field(default_factory=list)
    receipts: list[ReceiptRecord] = Field(default_factory=list)
    attempts: list[AttemptRecord] = Field(default_factory=list)
    artifacts: list[str] = Field(default_factory=list)
    review_bundle: dict[str, Any] | None = None
    bundle_artifact: str | None = None
    outcome: dict[str, Any] | None = None
    reason: str = ""


class RepairOutcome(Model):
    status: RepairOutcomeStatus
    reason: str
    task_id: UUID
    finding_id: str
    handoff_id: str
    repository: str
    base_commit: str
    snapshot_hash: str
    baseline: dict[str, Any] | None
    attempts: list[AttemptRecord]
    receipts: list[ReceiptRecord] = Field(default_factory=list)
    test_runs: int
    budget: dict[str, Any] = Field(default_factory=dict)
    failure_stage: str | None = None
    failure_code: str | None = None
    review_bundle: dict[str, Any] | None = None
    bundle_artifact: str | None = None
    proposal: dict[str, Any] | None = None
    diff: str | None = None
    artifacts: list[str] = Field(default_factory=list)
    discarded_receipts: list[dict[str, Any]] = Field(default_factory=list)
    checkpoint_seq: int = 0


class AuthorizedRepairJob(Model):
    """Everything an authorized repair may read; no hidden content enters prompts."""

    task_id: UUID
    finding_id: str
    finding_title: str
    finding_commit: str
    handoff: RepairHandoff
    snapshot: RepositorySnapshot
    verification_files: dict[str, str]
    verification_tests: list[str]
    budget: RepairBudget = Field(default_factory=RepairBudget)
    patch_limits: PatchLimits = Field(default_factory=PatchLimits)


class RepairLedger(Protocol):
    def load(self, task_id: UUID, finding_id: str) -> RepairCheckpoint | None: ...

    def save(self, checkpoint: RepairCheckpoint) -> None: ...

    def put_artifact(self, content: bytes) -> str: ...

    def read_artifact(self, content_hash: str) -> bytes: ...


class JsonRepairLedger:
    """Atomic file ledger used by local runs and crash-restart tests."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.artifacts = self.root / "artifacts"
        self.artifacts.mkdir(parents=True, exist_ok=True)

    def _path(self, task_id: UUID, finding_id: str) -> Path:
        return self.root / f"{task_id}-{finding_id[:24]}.json"

    def load(self, task_id: UUID, finding_id: str) -> RepairCheckpoint | None:
        path = self._path(task_id, finding_id)
        if not path.exists():
            return None
        return RepairCheckpoint.model_validate_json(path.read_text(encoding="utf-8"))

    def save(self, checkpoint: RepairCheckpoint) -> None:
        path = self._path(checkpoint.task_id, checkpoint.finding_id)
        temporary = path.with_suffix(".tmp")
        temporary.write_bytes(_canonical(checkpoint))
        os.replace(temporary, path)

    def put_artifact(self, content: bytes) -> str:
        content_hash = hashlib.sha256(content).hexdigest()
        target = self.artifacts / content_hash
        if not target.exists():
            target.write_bytes(content)
        return content_hash

    def read_artifact(self, content_hash: str) -> bytes:
        return (self.artifacts / content_hash).read_bytes()


class WorkbenchRepairLedger:
    """Task/Run ledger adapter: checkpoints and artifacts live in PostgreSQL."""

    def __init__(self, store: WorkbenchStore) -> None:
        self.store = store

    def load(self, task_id: UUID, finding_id: str) -> RepairCheckpoint | None:
        for event in reversed(self.store.events(task_id)):
            if event["kind"] != "repair.checkpoint":
                continue
            payload = event["payload"]
            if payload.get("finding_id") != finding_id:
                continue
            return RepairCheckpoint.model_validate(payload["checkpoint"])
        return None

    def save(self, checkpoint: RepairCheckpoint) -> None:
        content = _canonical(checkpoint)
        artifact_hash = self.store.put_artifact(content)
        self.store.append_events(
            checkpoint.task_id,
            [
                {
                    "stage": "repair",
                    "status": checkpoint.phase,
                    "summary": (
                        f"Repair checkpoint {checkpoint.seq} "
                        f"({checkpoint.phase}) for {checkpoint.finding_id[:12]}"
                    ),
                    "payload": {
                        "finding_id": checkpoint.finding_id,
                        "checkpoint": checkpoint.model_dump(mode="json"),
                        "artifact_hash": artifact_hash,
                    },
                    "kind": "repair.checkpoint",
                    "idempotency_key": (
                        f"repair-checkpoint:{checkpoint.finding_id}:{checkpoint.seq}"
                    ),
                }
            ],
        )

    def put_artifact(self, content: bytes) -> str:
        return self.store.put_artifact(content)

    def read_artifact(self, content_hash: str) -> bytes:
        return self.store.engine.artifacts.read(content_hash)


class _ReplayProvider:
    """Replays recorded plans/rejections; refuses an unknown provider outcome."""

    def __init__(
        self,
        checkpoint: RepairCheckpoint,
        ledger: RepairLedger,
        live: RepairProvider,
        *,
        allow_unknown_repeat: bool,
        interrupted_attempt: int | None,
    ) -> None:
        self.checkpoint = checkpoint
        self.ledger = ledger
        self.live = live
        self.allow_unknown_repeat = allow_unknown_repeat
        self.interrupted_attempt = interrupted_attempt

    def propose(self, prompt: Any) -> PatchPlan:
        attempt = int(prompt.attempt)
        rejection = next(
            (item for item in self.checkpoint.rejections if item.attempt == attempt),
            None,
        )
        if rejection is not None:
            raise PatchProposalRejected(
                rejection.code,
                rejection.message,
                retryable=rejection.retryable,
            )
        planned = next(
            (item for item in self.checkpoint.plans if item.attempt == attempt),
            None,
        )
        if planned is not None:
            payload = json.loads(self.ledger.read_artifact(planned.artifact_hash))
            return PatchPlan.model_validate(payload)
        if (
            self.interrupted_attempt == attempt
            and not self.allow_unknown_repeat
        ):
            raise PatchProposalRejected(
                "REPAIR_UNKNOWN_PROVIDER_OUTCOME",
                (
                    "The previous process stopped while the provider call was in "
                    "flight; repeating it could duplicate an unknown side effect."
                ),
                retryable=False,
            )
        return self.live.propose(prompt)


class _CheckpointObserver:
    """Persists one checkpoint at every node boundary the repair agent reports."""

    def __init__(
        self,
        *,
        checkpoint: RepairCheckpoint,
        ledger: RepairLedger,
        budget: RepairBudget,
        cancel: RepairCancelToken | None,
    ) -> None:
        self.checkpoint = checkpoint
        self.ledger = ledger
        self.budget = budget
        self.cancel = cancel
        self.artifacts: set[str] = set(checkpoint.artifacts)

    def _check_cancel(
        self,
        *,
        stage: str,
        attempt: int = 0,
        receipt: SandboxTestReceipt | None = None,
    ) -> None:
        if self.cancel is not None and self.cancel.is_set():
            raise RepairCancelled(stage, attempt, receipt)

    def _artifact(self, payload: Any) -> str:
        content_hash = self.ledger.put_artifact(_canonical(payload))
        self.artifacts.add(content_hash)
        return content_hash

    def _update(self, **fields: Any) -> None:
        self.checkpoint = self.checkpoint.model_copy(update=fields)

    def _save(self) -> None:
        self._update(
            seq=self.checkpoint.seq + 1,
            updated_at=_now(),
            artifacts=sorted(self.artifacts),
        )
        self.ledger.save(self.checkpoint)

    def _count_test(self) -> None:
        self._update(test_runs=self.checkpoint.test_runs + 1)
        if self.checkpoint.test_runs > self.budget.max_test_runs:
            raise _LedgerBudgetExhausted

    def baseline_started(self) -> None:
        self._check_cancel(stage="baseline")
        self._update(phase="baseline", node="baseline")
        self._save()

    def baseline(self, receipt: SandboxTestReceipt) -> None:
        self._check_cancel(stage="baseline", receipt=receipt)
        record = self._receipt_record("baseline", 0, receipt)
        self._update(
            baseline=record.model_dump(mode="json"),
            phase="proposing",
            node="baseline",
        )
        self._count_test()
        self._save()

    def provider_started(self, attempt: int) -> None:
        self._check_cancel(stage="provider", attempt=attempt)
        self._update(attempt=attempt, phase="proposing", node="provider")
        self._save()

    def planned(self, attempt: int, plan: PatchPlan) -> None:
        self._check_cancel(stage="patch", attempt=attempt)
        artifact_hash = self._artifact(plan)
        self._update(
            plans=[
                *(
                    item
                    for item in self.checkpoint.plans
                    if item.attempt != attempt
                ),
                PlannedAttempt(
                    attempt=attempt,
                    summary=plan.summary,
                    artifact_hash=artifact_hash,
                ),
            ],
            phase="patching",
            node="patch",
        )
        self._save()

    def patched(self, attempt: int, patch: PatchResult) -> None:
        self._check_cancel(stage="patch", attempt=attempt)
        recorded = next(
            (item for item in self.checkpoint.patches if item.attempt == attempt),
            None,
        )
        if recorded is not None and recorded.patch_id != patch.patch_id:
            raise DomainError(
                "REPAIR_PATCH_DRIFT",
                "The replayed patch no longer matches its recorded identity",
                409,
            )
        patches = self.checkpoint.patches
        if recorded is None:
            patches = [
                *patches,
                PatchRecord(
                    attempt=attempt,
                    patch_id=patch.patch_id,
                    tree_before=patch.tree_before,
                    tree_after=patch.tree_after,
                    artifact_hash=self._artifact(patch),
                ),
            ]
        self._update(patches=patches, phase="public_tests", node="patch")
        self._save()

    def receipt(self, stage: str, attempt: int, receipt: SandboxTestReceipt) -> None:
        self._check_cancel(stage=stage, attempt=attempt, receipt=receipt)
        self._receipt_record(stage, attempt, receipt)
        self._update(phase="verification", node=stage)
        self._count_test()
        self._save()

    def attempt(self, record: RepairAttempt) -> None:
        self._check_cancel(stage="attempt", attempt=record.attempt)
        feedback = record.feedback
        artifact_hash = self._artifact(record)
        rejections = self.checkpoint.rejections
        if feedback is not None:
            rejections = [
                *(
                    item
                    for item in rejections
                    if item.attempt != record.attempt
                ),
                RejectionRecord(
                    attempt=record.attempt,
                    stage=feedback.stage,
                    code=feedback.code,
                    message=feedback.message,
                    retryable=True,
                ),
            ]
        self._update(
            rejections=rejections,
            attempts=[
                *self.checkpoint.attempts,
                AttemptRecord(
                    attempt=record.attempt,
                    accepted=record.accepted,
                    patch_id=record.patch_id,
                    public_receipt_id=(
                        record.public_receipt.receipt_id
                        if record.public_receipt is not None
                        else None
                    ),
                    verification_receipt_id=(
                        record.verification_receipt.receipt_id
                        if record.verification_receipt is not None
                        else None
                    ),
                    feedback_stage=feedback.stage if feedback is not None else None,
                    feedback_code=feedback.code if feedback is not None else None,
                    feedback_message=feedback.message if feedback is not None else None,
                    artifact_hash=artifact_hash,
                ),
            ],
            phase="proposing",
            node="attempt",
        )
        self._save()

    def _receipt_record(
        self,
        stage: str,
        attempt: int,
        receipt: SandboxTestReceipt,
    ) -> ReceiptRecord:
        artifact_hash = self._artifact(receipt)
        record = ReceiptRecord(
            stage=cast(ReceiptStage, stage),
            attempt=attempt,
            receipt_id=receipt.receipt_id,
            status=receipt.status,
            passed=receipt.status == "passed",
            log_sha256=receipt.log_sha256,
            artifact_hash=artifact_hash,
            receipt=receipt.model_dump(mode="json"),
        )
        self._update(receipts=[*self.checkpoint.receipts, record])
        return record


class AuthorizedRepairOrchestrator:
    """Run one authorized repair from a handoff to a waiting-approval bundle."""

    def __init__(
        self,
        *,
        ledger: RepairLedger,
        allowed_actors: Sequence[str],
        runtime: SnapshotRepairRuntime | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.ledger = ledger
        self.allowed_actors = list(allowed_actors)
        self.runtime = runtime or SnapshotRepairRuntime()
        self.clock = clock

    def run(
        self,
        job: AuthorizedRepairJob,
        provider: RepairProvider,
        *,
        cancel: RepairCancelToken | None = None,
        current_commit: Callable[[], str] | None = None,
        allow_unknown_repeat: bool = False,
    ) -> RepairOutcome:
        self._authorize(job)
        checkpoint = self.ledger.load(job.task_id, job.finding_id)
        interrupted_attempt: int | None = None
        if checkpoint is not None and checkpoint.phase == "proposing":
            interrupted_attempt = checkpoint.attempt or None
        if checkpoint is None:
            checkpoint = RepairCheckpoint(
                task_id=job.task_id,
                finding_id=job.finding_id,
                handoff_id=job.handoff.handoff_id,
                repository=job.handoff.repository,
                base_commit=job.handoff.base_commit,
                snapshot_hash=job.snapshot.snapshot_hash,
                budget=job.budget.model_dump(mode="json"),
                started_at=_now(),
                updated_at=_now(),
            )
            self.ledger.save(checkpoint)
        else:
            self._bind(checkpoint, job)
            if checkpoint.phase in _TERMINAL and checkpoint.outcome is not None:
                return RepairOutcome.model_validate(checkpoint.outcome)
        if current_commit is not None:
            observed = current_commit()
            if observed != job.handoff.base_commit:
                return self._finish(
                    checkpoint,
                    "base_advanced",
                    (
                        "The repository moved to commit "
                        f"{observed[:12]} before the repair started"
                    ),
                )
        observer = _CheckpointObserver(
            checkpoint=checkpoint,
            ledger=self.ledger,
            budget=job.budget,
            cancel=cancel,
        )
        replay = _ReplayProvider(
            checkpoint,
            self.ledger,
            provider,
            allow_unknown_repeat=allow_unknown_repeat,
            interrupted_attempt=interrupted_attempt,
        )
        try:
            report = self.runtime.run(
                job.handoff,
                job.snapshot,
                replay,
                verification_files=job.verification_files,
                verification_tests=job.verification_tests,
                budget=job.budget,
                patch_limits=job.patch_limits,
                observer=observer,
            )
        except RepairCancelled as exc:
            return self._cancelled(observer.checkpoint, exc)
        except _LedgerBudgetExhausted:
            return self._finish(
                observer.checkpoint,
                "budget_exhausted",
                "The cumulative repair test-run budget is exhausted",
            )
        checkpoint = observer.checkpoint
        if cancel is not None and cancel.is_set():
            return self._cancelled(
                checkpoint,
                RepairCancelled("post_run", checkpoint.attempt),
            )
        if current_commit is not None:
            observed = current_commit()
            if observed != job.handoff.base_commit:
                return self._finish(
                    checkpoint,
                    "base_advanced",
                    (
                        "The repository moved to commit "
                        f"{observed[:12]} while the repair was running"
                    ),
                )
        if report.status != "succeeded":
            return self._report_failure(checkpoint, report)
        return self._succeed(checkpoint, job, report)

    # -- gates -----------------------------------------------------------
    def _authorize(self, job: AuthorizedRepairJob) -> None:
        handoff = job.handoff
        if handoff.computed_id() != handoff.handoff_id:
            raise DomainError(
                "REPAIR_HANDOFF_CORRUPTED",
                "Handoff id does not match its content",
                409,
            )
        if job.finding_id != handoff.finding_id:
            raise DomainError(
                "REPAIR_FINDING_MISMATCH",
                "The job finding does not match the handoff",
                409,
            )
        if job.finding_commit != handoff.base_commit:
            raise DomainError(
                "REPAIR_COMMIT_MISMATCH",
                "The finding was not confirmed at the handoff base commit",
                409,
            )
        if (
            job.snapshot.repository != handoff.repository
            or job.snapshot.commit != handoff.base_commit
            or job.snapshot.computed_hash() != job.snapshot.snapshot_hash
        ):
            raise DomainError(
                "REPAIR_SNAPSHOT_MISMATCH",
                "The snapshot is not the handed-off commit",
                409,
            )
        handoff.authorization.validate_scope(
            repository=handoff.repository,
            budget=handoff.execution_budget,
            allowed_actors=self.allowed_actors,
        )

    @staticmethod
    def _bind(checkpoint: RepairCheckpoint, job: AuthorizedRepairJob) -> None:
        binding = (
            checkpoint.handoff_id,
            checkpoint.repository,
            checkpoint.base_commit,
            checkpoint.snapshot_hash,
        )
        expected = (
            job.handoff.handoff_id,
            job.handoff.repository,
            job.handoff.base_commit,
            job.snapshot.snapshot_hash,
        )
        if binding != expected:
            raise DomainError(
                "REPAIR_CHECKPOINT_CONFLICT",
                "A checkpoint exists for a different handoff or snapshot",
                409,
            )

    # -- outcomes --------------------------------------------------------
    def _finish(
        self,
        checkpoint: RepairCheckpoint,
        status: RepairOutcomeStatus,
        reason: str,
        *,
        proposal: dict[str, Any] | None = None,
        diff: str | None = None,
        bundle: dict[str, Any] | None = None,
        bundle_artifact: str | None = None,
        discarded: list[dict[str, Any]] | None = None,
    ) -> RepairOutcome:
        phase: CheckpointPhase
        if status == "waiting_approval":
            phase = "waiting_approval"
        elif status == "cancelled":
            phase = "cancelled"
        elif status == "base_advanced":
            phase = "base_advanced"
        else:
            phase = "failed"
        checkpoint = checkpoint.model_copy(
            update={
                "reason": reason,
                "phase": phase,
                "review_bundle": bundle,
                "bundle_artifact": bundle_artifact,
                "seq": checkpoint.seq + 1,
                "updated_at": _now(),
                "artifacts": sorted(set(checkpoint.artifacts)),
            }
        )
        failure = checkpoint.attempts[-1] if checkpoint.attempts else None
        outcome = RepairOutcome(
            status=status,
            reason=reason,
            task_id=checkpoint.task_id,
            finding_id=checkpoint.finding_id,
            handoff_id=checkpoint.handoff_id,
            repository=checkpoint.repository,
            base_commit=checkpoint.base_commit,
            snapshot_hash=checkpoint.snapshot_hash,
            baseline=checkpoint.baseline,
            attempts=list(checkpoint.attempts),
            receipts=list(checkpoint.receipts),
            test_runs=checkpoint.test_runs,
            budget=checkpoint.budget,
            failure_stage=(failure.feedback_stage if failure is not None else None),
            failure_code=(failure.feedback_code if failure is not None else None),
            review_bundle=bundle,
            bundle_artifact=bundle_artifact,
            proposal=proposal,
            diff=diff,
            artifacts=list(checkpoint.artifacts),
            discarded_receipts=list(discarded or []),
            checkpoint_seq=checkpoint.seq,
        )
        checkpoint = checkpoint.model_copy(
            update={"outcome": outcome.model_dump(mode="json")}
        )
        self.ledger.save(checkpoint)
        return outcome

    def _cancelled(
        self,
        checkpoint: RepairCheckpoint,
        exc: RepairCancelled,
    ) -> RepairOutcome:
        discarded: list[dict[str, Any]] = []
        if exc.receipt is not None:
            record = {
                "stage": exc.stage,
                "attempt": exc.attempt,
                "receipt_id": exc.receipt.receipt_id,
                "status": exc.receipt.status,
                "log_sha256": exc.receipt.log_sha256,
            }
            discarded.append(record)
        reason = (
            "Repair cancelled; a receipt that arrived after cancellation was "
            "discarded and no approval bundle was produced"
        )
        return self._finish(
            checkpoint,
            "cancelled",
            reason,
            discarded=discarded,
        )

    def _report_failure(
        self,
        checkpoint: RepairCheckpoint,
        report: RepairReport,
    ) -> RepairOutcome:
        allowed = {
            "baseline_not_reproduced",
            "waiting_for_dependencies",
            "sandbox_error",
            "exhausted",
            "budget_exhausted",
            "deadline_exceeded",
            "provider_error",
        }
        status: RepairOutcomeStatus = (
            cast(RepairOutcomeStatus, report.status)
            if report.status in allowed
            else "provider_error"
        )
        return self._finish(checkpoint, status, report.reason)

    def _succeed(
        self,
        checkpoint: RepairCheckpoint,
        job: AuthorizedRepairJob,
        report: RepairReport,
    ) -> RepairOutcome:
        proposal = report.proposal
        if proposal is None:
            return self._finish(
                checkpoint,
                "provider_error",
                "The runtime reported success without a proposal",
            )
        plan = self._accepted_plan(checkpoint, proposal.attempts_used)
        final_files = dict(job.snapshot.files)
        for edit in plan.edits:
            if edit.operation == "delete":
                final_files.pop(edit.path, None)
            else:
                final_files[edit.path] = edit.content or ""
        changed = sorted({item.path for item in proposal.changed_files})
        publication = [
            PublicationFile(path=path, content=final_files.get(path, ""))
            for path in changed
        ]
        body = (
            f"Verified repair proposal for finding {checkpoint.finding_id}.\n\n"
            f"Baseline receipt: {report.baseline.receipt_id if report.baseline else 'n/a'} "
            "(public tests failed before the patch)\n"
            f"Public receipt: {proposal.public_receipt.receipt_id} (passed)\n"
            f"Independent verification receipt: {proposal.verification_receipt.receipt_id} "
            "(passed; hidden tests never entered the provider prompt)\n\n"
            "This is a waiting-approval Review Bundle. No Draft PR was created and "
            "the workbench never merges."
        )
        bundle = seal_bundle(
            repository=job.handoff.repository,
            base_branch="main",
            base_sha=job.handoff.base_commit,
            tree_hash=_tree_hash(final_files),
            proposal_id=proposal.proposal_id,
            branch=f"mgar/v35-{checkpoint.finding_id[:12]}",
            title=f"Verified repair: {job.finding_title}"[:200],
            body=body,
            diff=proposal.diff,
            files=publication,
        )
        content = _canonical(bundle)
        artifact_hash = self.ledger.put_artifact(content)
        checkpoint = checkpoint.model_copy(
            update={"artifacts": sorted({*checkpoint.artifacts, artifact_hash})}
        )
        proposal_view = {
            "proposal_id": proposal.proposal_id,
            "plan_summary": proposal.plan_summary,
            "base_tree": proposal.base_tree,
            "tested_tree": proposal.tested_tree,
            "base_tree_sha256": _tree_hash(job.snapshot.files),
            "tested_tree_sha256": _tree_hash(final_files),
            "diff_sha256": hashlib.sha256(proposal.diff.encode()).hexdigest(),
            "public_receipt_id": proposal.public_receipt.receipt_id,
            "verification_receipt_id": proposal.verification_receipt.receipt_id,
            "attempts_used": proposal.attempts_used,
            "changed_files": [item.path for item in proposal.changed_files],
        }
        return self._finish(
            checkpoint,
            "waiting_approval",
            (
                f"Repair verified after {proposal.attempts_used} attempt(s); "
                "human approval is required before publication"
            ),
            proposal=proposal_view,
            diff=proposal.diff,
            bundle=bundle.model_dump(mode="json"),
            bundle_artifact=artifact_hash,
        )

    def _accepted_plan(
        self,
        checkpoint: RepairCheckpoint,
        attempt: int,
    ) -> PatchPlan:
        planned = next(
            (item for item in checkpoint.plans if item.attempt == attempt),
            None,
        )
        if planned is None:
            raise DomainError(
                "REPAIR_PLAN_MISSING",
                "The accepted attempt has no recorded patch plan",
                409,
            )
        payload = json.loads(self.ledger.read_artifact(planned.artifact_hash))
        return PatchPlan.model_validate(payload)
