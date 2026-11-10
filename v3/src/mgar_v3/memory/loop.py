"""V3.5 M01: persist repair and publication experience as memory.

The V3.4 repair loop and Draft PR delivery only wrote in-process episode logs.
This module adapts those episode contracts to the existing V3.3 memory writer:

* one episodic record per attempt/outcome, scoped to project and repository;
* provenance is the real evidence + artifact chain, never an inferred summary;
* a failed write is audited in ``knowledge.memory_write_attempts`` and can be
  retried without touching the already recorded business state;
* no fifth memory store is introduced.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import Field

from mgar_v3.domain import DomainError, Model
from mgar_v3.memory.model import MemoryRecord
from mgar_v3.memory.store import MemoryStore
from mgar_v3.memory.write import (
    CandidateTrigger,
    MemoryCandidate,
    MemoryOutcome,
    MemoryWriter,
    execution_candidate,
)
from mgar_v3.settings import Settings

if TYPE_CHECKING:
    from mgar_v3.repair.pr_delivery import PublicationEpisode
    from mgar_v3.repair.repair_agent import RepairEpisode
    from mgar_v3.workbench.orchestrator import RepairOutcome

TRACE_SCHEMA = "v35-memory-trace-v1"
_TOKEN = re.compile(r"[^A-Za-z0-9]+")

WriteStatus = Literal["written", "duplicate", "rejected", "requires_confirmation", "failed"]
AuditStatus = Literal["written", "duplicate", "rejected", "failed"]


class MemoryContext(Model):
    """The traceable identity shared by repair and publication episodes."""

    project_id: str
    repository: str
    commit: str
    actor: str
    ticket_id: UUID
    run_id: UUID
    task_id: UUID
    finding_id: str
    evidence_ids: list[UUID] = Field(default_factory=list)
    artifact_hashes: list[str] = Field(default_factory=list)


class MemoryWriteOutcome(Model):
    status: WriteStatus
    candidate_id: str
    memory_id: str | None = None
    issues: list[str] = Field(default_factory=list)


class MemoryWriteAttempt(Model):
    candidate_id: str
    project_id: str
    repository: str
    actor: str
    subject_key: str
    status: AuditStatus
    error_code: str
    error_message: str
    attempts: int
    memory_id: UUID | None = None
    updated_at: datetime


class MemoryWriteAudit:
    """Durable audit/retry ledger for memory writes that leave the store."""

    def __init__(self, settings: Settings):
        self.settings = settings

    def record(
        self,
        candidate: MemoryCandidate,
        *,
        status: AuditStatus,
        memory_id: UUID | None = None,
        code: str = "",
        message: str = "",
    ) -> None:
        with psycopg.connect(self.settings.database_url.get_secret_value()) as conn:
            conn.execute(
                "INSERT INTO knowledge.memory_write_attempts("
                "candidate_id,project_id,repository,actor,subject_key,status,"
                "error_code,error_message,candidate,memory_id) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (candidate_id) DO UPDATE SET "
                "status=EXCLUDED.status, error_code=EXCLUDED.error_code, "
                "error_message=EXCLUDED.error_message, attempts="
                "knowledge.memory_write_attempts.attempts+1, "
                "memory_id=COALESCE(EXCLUDED.memory_id, "
                "knowledge.memory_write_attempts.memory_id), updated_at=now()",
                (
                    candidate.candidate_id,
                    candidate.project_id,
                    candidate.repository,
                    candidate.actor,
                    candidate.resolved_subject,
                    status,
                    code[:120],
                    message[:1000],
                    Jsonb(candidate.model_dump(mode="json")),
                    memory_id,
                ),
            )

    def get(self, candidate_id: str) -> MemoryWriteAttempt:
        with psycopg.connect(
            self.settings.database_url.get_secret_value(), row_factory=dict_row
        ) as conn:
            row = conn.execute(
                "SELECT candidate_id,project_id,repository,actor,subject_key,status,"
                "error_code,error_message,attempts,memory_id,updated_at "
                "FROM knowledge.memory_write_attempts WHERE candidate_id=%s",
                (candidate_id,),
            ).fetchone()
        if row is None:
            raise DomainError(
                "MEMORY_WRITE_NOT_FOUND", "Memory write attempt was not found", 404
            )
        return MemoryWriteAttempt.model_validate(row)

    def pending(self, *, limit: int = 20) -> list[MemoryWriteAttempt]:
        with psycopg.connect(
            self.settings.database_url.get_secret_value(), row_factory=dict_row
        ) as conn:
            rows = conn.execute(
                "SELECT candidate_id,project_id,repository,actor,subject_key,status,"
                "error_code,error_message,attempts,memory_id,updated_at "
                "FROM knowledge.memory_write_attempts "
                "WHERE status='failed' ORDER BY updated_at, candidate_id LIMIT %s",
                (max(1, limit),),
            ).fetchall()
        return [MemoryWriteAttempt.model_validate(row) for row in rows]

    def candidate(self, candidate_id: str) -> MemoryCandidate:
        with psycopg.connect(
            self.settings.database_url.get_secret_value(), row_factory=dict_row
        ) as conn:
            row = conn.execute(
                "SELECT candidate,status FROM knowledge.memory_write_attempts "
                "WHERE candidate_id=%s",
                (candidate_id,),
            ).fetchone()
        if row is None:
            raise DomainError(
                "MEMORY_WRITE_NOT_FOUND", "Memory write attempt was not found", 404
            )
        if row["status"] != "failed":
            raise DomainError(
                "MEMORY_WRITE_NOT_RETRYABLE",
                f"Memory write attempt is {row['status']}, not failed",
                409,
            )
        return MemoryCandidate.model_validate(row["candidate"])


def _token(value: str) -> str:
    token = _TOKEN.sub("-", value).strip("-")[:32]
    return token or "finding"


def _repair_trigger(status: str) -> tuple[CandidateTrigger, MemoryOutcome] | None:
    if status == "succeeded":
        return "patch_tests_passed", "success"
    if status in {"exhausted", "patch_rejected", "public_tests_failed", "verification_failed"}:
        return "patch_verification_failed", "failure"
    if status in {"cancelled"}:
        return None
    return "investigation_completed", "failure"


def _outcome_trigger(status: str) -> tuple[CandidateTrigger, MemoryOutcome] | None:
    if status == "waiting_approval":
        return "patch_tests_passed", "success"
    if status == "exhausted":
        return "patch_verification_failed", "failure"
    if status in {"cancelled", "base_advanced"}:
        return None
    return "investigation_completed", "failure"


def _publication_trigger(status: str) -> tuple[CandidateTrigger, MemoryOutcome] | None:
    if status == "published":
        return "release_decided", "success"
    if status in {"write_permission_required", "base_changed", "failed"}:
        return "release_decided", "failure"
    return None


class PersistentMemoryLoop:
    """Write one execution episode through the V3.3 memory write gate."""

    def __init__(
        self,
        settings: Settings,
        *,
        engine: Any,
        writer: MemoryWriter | None = None,
        audit: MemoryWriteAudit | None = None,
    ):
        self.settings = settings
        self.engine = engine
        self._writer = writer
        self.audit = audit or MemoryWriteAudit(settings)

    # -- scope -----------------------------------------------------------
    def _view(self, repository: str) -> tuple[Settings, MemoryWriter]:
        repository = repository.lower()
        scoped = self.settings
        if self.settings.repository.lower() != repository:
            scoped = self.settings.model_copy(update={"repository": repository})
        if self._writer is not None:
            return scoped, self._writer
        return scoped, MemoryWriter(scoped, MemoryStore(scoped))

    def _artifact(self, payload: Any) -> str:
        data = (
            payload
            if isinstance(payload, bytes)
            else json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode()
        )
        content_hash = self.engine.artifacts.put(data)
        with self.engine.transaction() as conn:
            conn.execute(
                "INSERT INTO app.artifacts(content_hash,byte_count) VALUES (%s,%s) "
                "ON CONFLICT DO NOTHING",
                (content_hash, len(data)),
            )
        return str(content_hash)

    # -- write path ------------------------------------------------------
    def persist(self, candidate: MemoryCandidate) -> MemoryWriteOutcome:
        scoped, writer = self._view(candidate.repository)
        try:
            validation = writer.validate(candidate, actor=scoped.local_actor)
        except DomainError as exc:
            self._audit(candidate, status="failed", code=exc.code, message=str(exc))
            return MemoryWriteOutcome(
                status="failed", candidate_id=candidate.candidate_id, issues=[exc.code]
            )
        if not validation.accepted:
            issues = sorted({issue.code for issue in validation.issues})
            self._audit(
                candidate,
                status="rejected",
                code=",".join(issues),
                message="; ".join(issue.message for issue in validation.issues)[:1000],
            )
            return MemoryWriteOutcome(
                status="rejected",
                candidate_id=validation.candidate_id,
                issues=issues,
            )
        if validation.requires_confirmation:
            self._audit(
                candidate,
                status="rejected",
                code="MEMORY_CONFIRMATION_REQUIRED",
                message="candidate needs explicit human confirmation",
            )
            return MemoryWriteOutcome(
                status="requires_confirmation",
                candidate_id=validation.candidate_id,
                issues=["MEMORY_CONFIRMATION_REQUIRED"],
            )
        try:
            record = writer.write(candidate, actor=scoped.local_actor)
        except DomainError as exc:
            current = self._current(candidate)
            if (
                exc.code == "MEMORY_CONFLICT"
                and current is not None
                and current.summary_hash == candidate.summary_hash
            ):
                self._audit(
                    candidate,
                    status="duplicate",
                    memory_id=current.memory_id,
                    code=exc.code,
                    message="the same episode is already active",
                )
                return MemoryWriteOutcome(
                    status="duplicate",
                    candidate_id=candidate.candidate_id,
                    memory_id=str(current.memory_id),
                )
            self._audit(candidate, status="failed", code=exc.code, message=str(exc))
            return MemoryWriteOutcome(
                status="failed", candidate_id=candidate.candidate_id, issues=[exc.code]
            )
        self._audit(candidate, status="written", memory_id=record.memory_id)
        return MemoryWriteOutcome(
            status="written",
            candidate_id=candidate.candidate_id,
            memory_id=str(record.memory_id),
        )

    def retry(self, candidate_id: str) -> MemoryWriteOutcome:
        """Retry one audited failure from its durable candidate payload."""

        return self.persist(self.audit.candidate(candidate_id))

    def _current(self, candidate: MemoryCandidate) -> MemoryRecord | None:
        scoped, _ = self._view(candidate.repository)
        return MemoryStore(scoped).current(
            memory_kind=candidate.memory_kind,
            scope=candidate.scope,
            subject_key=candidate.resolved_subject,
            commit=candidate.commit,
        )

    def _audit(
        self,
        candidate: MemoryCandidate,
        *,
        status: AuditStatus,
        memory_id: UUID | None = None,
        code: str = "",
        message: str = "",
    ) -> None:
        try:
            self.audit.record(
                candidate,
                status=status,
                memory_id=memory_id,
                code=code,
                message=message,
            )
        except Exception:
            # The memory write must never take the verified business state down.
            # A database outage leaves only the local log entry, which still
            # avoids silently pretending the experience was persisted.
            logging.getLogger(__name__).exception("memory write audit failed")

    # -- repair ----------------------------------------------------------
    def record_repair(
        self, context: MemoryContext, episode: RepairEpisode
    ) -> MemoryWriteOutcome:
        try:
            return self._record_repair(context, episode)
        except Exception as exc:
            return MemoryWriteOutcome(
                status="failed",
                candidate_id="",
                issues=[type(exc).__name__],
            )

    def _record_repair(
        self, context: MemoryContext, episode: RepairEpisode
    ) -> MemoryWriteOutcome:
        mapped = _repair_trigger(episode.status)
        if mapped is None:
            return MemoryWriteOutcome(
                status="rejected", candidate_id="", issues=["NOT_REUSABLE"]
            )
        trigger, outcome = mapped
        trace = {
            "schema_version": TRACE_SCHEMA,
            "kind": "repair",
            "task_id": str(context.task_id),
            "run_id": str(context.run_id),
            "ticket_id": str(context.ticket_id),
            "finding_id": context.finding_id,
            "repository": context.repository,
            "commit": context.commit,
            "evidence_ids": [str(item) for item in context.evidence_ids],
            "episode": episode.model_dump(mode="json"),
        }
        trace_hash = self._artifact(trace)
        failed = ",".join(episode.failed_tests[:8]) or "none"
        summary = (
            f"repair status={episode.status} attempt={episode.attempt} "
            f"tree={episode.tree[:12]} reason={episode.reason[:400]} "
            f"failed_tests={failed} objective={episode.objective[:160]} "
            f"task={context.task_id} run={context.run_id} finding={context.finding_id} "
            f"evidence={','.join(str(item) for item in context.evidence_ids[:4]) or 'none'} "
            f"trace={trace_hash}"
        )[:2000]
        candidate = execution_candidate(
            trigger=trigger,
            outcome=outcome,
            project_id=context.project_id,
            repository=context.repository,
            actor=context.actor,
            subject_key=(
                f"repair:{_token(context.finding_id)}:{episode.attempt}:{episode.status}"
            ),
            summary=summary,
            summary_hash=self._artifact(summary),
            ticket_id=context.ticket_id,
            run_id=context.run_id,
            occurred_at=datetime.now(UTC),
            evidence_ids=context.evidence_ids,
            artifact_hashes=(
                [trace_hash, *context.artifact_hashes]
                if context.evidence_ids
                else list(context.artifact_hashes)
            ),
            confidence=(
                0.9
                if outcome == "success" and context.evidence_ids
                else 0.7
                if context.evidence_ids
                else 0.4
            ),
            verification_status="verified" if context.evidence_ids else "supported",
            provenance="mechanical" if context.evidence_ids else "agent_reported",
            commit=context.commit,
            scope="commit",
        )
        return self.persist(candidate)

    def record_repair_outcome(
        self, context: MemoryContext, outcome: RepairOutcome
    ) -> MemoryWriteOutcome:
        try:
            return self._record_repair_outcome(context, outcome)
        except Exception as exc:
            return MemoryWriteOutcome(
                status="failed", candidate_id="", issues=[type(exc).__name__]
            )

    def _record_repair_outcome(
        self, context: MemoryContext, outcome: RepairOutcome
    ) -> MemoryWriteOutcome:
        mapped = _outcome_trigger(outcome.status)
        if mapped is None:
            return MemoryWriteOutcome(
                status="rejected", candidate_id="", issues=["NOT_REUSABLE"]
            )
        trigger, result = mapped
        receipts = [
            {
                "stage": item.stage,
                "attempt": item.attempt,
                "receipt_id": item.receipt_id,
                "status": item.status,
                "passed": item.passed,
                "artifact_hash": item.artifact_hash,
                "log_sha256": item.log_sha256,
            }
            for item in outcome.receipts
        ]
        attempts = [
            {
                "attempt": item.attempt,
                "accepted": item.accepted,
                "feedback_stage": item.feedback_stage,
                "feedback_code": item.feedback_code,
                "artifact_hash": item.artifact_hash,
            }
            for item in outcome.attempts
        ]
        trace = {
            "schema_version": TRACE_SCHEMA,
            "kind": "repair_outcome",
            "task_id": str(context.task_id),
            "run_id": str(context.run_id),
            "ticket_id": str(context.ticket_id),
            "finding_id": context.finding_id,
            "repository": context.repository,
            "commit": context.commit,
            "handoff_id": outcome.handoff_id,
            "snapshot_hash": outcome.snapshot_hash,
            "status": outcome.status,
            "reason": outcome.reason,
            "failure_stage": outcome.failure_stage,
            "failure_code": outcome.failure_code,
            "proposal": outcome.proposal,
            "bundle_artifact": outcome.bundle_artifact,
            "attempts": attempts,
            "test_receipts": receipts,
            "evidence_ids": [str(item) for item in context.evidence_ids],
        }
        trace_hash = self._artifact(trace)
        receipt_hashes = [
            str(item["artifact_hash"]) for item in receipts if item["artifact_hash"]
        ]
        attempt_hashes = [
            str(item["artifact_hash"]) for item in attempts if item["artifact_hash"]
        ]
        summary = (
            f"repair-outcome status={outcome.status} reason={outcome.reason[:400]} "
            f"base={outcome.base_commit} snapshot={outcome.snapshot_hash[:12]} "
            f"test_runs={outcome.test_runs} "
            f"failure={outcome.failure_stage or 'none'}/{outcome.failure_code or 'none'} "
            f"attempts={len(attempts)} receipts={len(receipts)} "
            f"task={context.task_id} run={context.run_id} finding={context.finding_id} "
            f"evidence={','.join(str(item) for item in context.evidence_ids[:4]) or 'none'} "
            f"trace={trace_hash}"
        )[:2000]
        candidate = execution_candidate(
            trigger=trigger,
            outcome=result,
            project_id=context.project_id,
            repository=context.repository,
            actor=context.actor,
            subject_key=f"repair-outcome:{_token(context.finding_id)}:{outcome.status}",
            summary=summary,
            summary_hash=self._artifact(summary),
            ticket_id=context.ticket_id,
            run_id=context.run_id,
            occurred_at=datetime.now(UTC),
            evidence_ids=context.evidence_ids,
            artifact_hashes=list(
                dict.fromkeys(
                    [
                        trace_hash,
                        *context.artifact_hashes,
                        *attempt_hashes,
                        *receipt_hashes,
                        *outcome.artifacts,
                        *([outcome.bundle_artifact] if outcome.bundle_artifact else []),
                    ]
                )
            ),
            confidence=0.9 if result == "success" and context.evidence_ids else 0.7,
            verification_status="verified" if context.evidence_ids else "supported",
            provenance="mechanical",
            commit=context.commit,
            scope="commit",
        )
        return self.persist(candidate)

    # -- publication -----------------------------------------------------
    def record_publication(
        self, context: MemoryContext, episode: PublicationEpisode
    ) -> MemoryWriteOutcome:
        try:
            return self._record_publication(context, episode)
        except Exception as exc:
            return MemoryWriteOutcome(
                status="failed", candidate_id="", issues=[type(exc).__name__]
            )

    def _record_publication(
        self, context: MemoryContext, episode: PublicationEpisode
    ) -> MemoryWriteOutcome:
        mapped = _publication_trigger(episode.status)
        if mapped is None:
            return MemoryWriteOutcome(
                status="rejected", candidate_id="", issues=["NOT_TERMINAL"]
            )
        trigger, outcome = mapped
        trace = {
            "schema_version": TRACE_SCHEMA,
            "kind": "publication",
            "task_id": str(context.task_id),
            "run_id": str(context.run_id),
            "ticket_id": str(context.ticket_id),
            "finding_id": context.finding_id,
            "repository": context.repository,
            "commit": context.commit,
            "episode": episode.model_dump(mode="json"),
        }
        trace_hash = self._artifact(trace)
        summary = (
            f"publication status={episode.status} proposal={episode.proposal_id} "
            f"branch={episode.branch} base={episode.base_sha} tree={episode.tree_hash[:12]} "
            f"payload={episode.payload_hash[:12]} pull_request={episode.pull_request or 'none'} "
            f"draft={episode.draft} reason={episode.reason[:400]} "
            f"task={context.task_id} run={context.run_id} finding={context.finding_id} "
            f"evidence={','.join(str(item) for item in context.evidence_ids[:4]) or 'none'} "
            f"trace={trace_hash}"
        )[:2000]
        candidate = execution_candidate(
            trigger=trigger,
            outcome=outcome,
            project_id=context.project_id,
            repository=context.repository,
            actor=context.actor,
            subject_key=(
                f"publication:{_token(context.finding_id)}:"
                f"{episode.status}:{episode.payload_hash[:12]}"
            ),
            summary=summary,
            summary_hash=self._artifact(summary),
            ticket_id=context.ticket_id,
            run_id=context.run_id,
            occurred_at=datetime.now(UTC),
            evidence_ids=context.evidence_ids,
            artifact_hashes=(
                [trace_hash, *context.artifact_hashes]
                if context.evidence_ids
                else list(context.artifact_hashes)
            ),
            confidence=0.9 if outcome == "success" and context.evidence_ids else 0.6,
            verification_status="verified" if context.evidence_ids else "supported",
            provenance="mechanical",
            commit=context.commit,
            scope="commit",
        )
        return self.persist(candidate)


class PersistentRepairMemory:
    """``EpisodeSink`` adapter used by ``SnapshotRepairRuntime``."""

    def __init__(self, loop: PersistentMemoryLoop, context: MemoryContext):
        self.loop = loop
        self.context = context

    def record(self, episode: RepairEpisode) -> None:
        self.loop.record_repair(self.context, episode)


class PersistentPublicationMemory:
    """``DeliveryMemorySink`` adapter used by Draft PR delivery."""

    def __init__(self, loop: PersistentMemoryLoop, context: MemoryContext):
        self.loop = loop
        self.context = context

    def record(self, episode: PublicationEpisode) -> None:
        self.loop.record_publication(self.context, episode)


__all__ = [
    "MemoryContext",
    "MemoryWriteAttempt",
    "MemoryWriteAudit",
    "MemoryWriteOutcome",
    "PersistentMemoryLoop",
    "PersistentPublicationMemory",
    "PersistentRepairMemory",
    "TRACE_SCHEMA",
]
