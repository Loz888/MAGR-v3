"""V3.3 M02 memory write gate: candidate -> validation -> confirmed memory.

Candidates are produced from explicit execution moments (investigation, finding
evidence, patch verification, PR approval, release).  Only execution experience
with real provenance may be auto-confirmed; cross-task knowledge, executable
recipes and high-risk experience need an explicit human confirmation bound to
the candidate digest.  Secrets, prompt injection and unverified inference never
reach ``knowledge.memory_records``.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from pydantic import Field, field_validator, model_validator

from mgar_v3.domain import DomainError, Model, digest
from mgar_v3.findings.model import Finding, FindingLifecycle
from mgar_v3.memory.model import (
    EpisodicDetail,
    MemoryDetail,
    MemoryDraft,
    MemoryKind,
    MemoryRecord,
    MemoryScope,
    MemoryVerification,
    ProceduralDetail,
    SemanticDetail,
    WorkingDetail,
)
from mgar_v3.memory.store import MemoryStore
from mgar_v3.settings import Settings

CANDIDATE_SCHEMA = "v33-memory-candidate-v1"

CandidateTrigger = Literal[
    "investigation_completed",
    "finding_evidence_added",
    "patch_verification_failed",
    "patch_tests_passed",
    "pr_approval_decided",
    "release_decided",
]
MemoryOutcome = Literal["success", "failure", "neutral"]
CandidateProvenance = Literal["mechanical", "agent_reported", "human", "inferred"]

TRIGGER_OUTCOMES: dict[CandidateTrigger, set[MemoryOutcome]] = {
    "investigation_completed": {"success", "failure"},
    "finding_evidence_added": {"success", "failure", "neutral"},
    "patch_verification_failed": {"failure"},
    "patch_tests_passed": {"success"},
    "pr_approval_decided": {"success", "failure"},
    "release_decided": {"success", "failure"},
}

_IDENTITY = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
_REPOSITORY = r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$"
_COMMIT = r"^[a-f0-9]{40}$"
_HASH = r"^[a-f0-9]{64}$"
_SUBJECT = r"^[A-Za-z0-9][A-Za-z0-9._:/#-]{0,119}$"

Row = dict[str, Any]
Connection = psycopg.Connection[Row]

_SECRET_PATTERNS = re.compile(
    r"(?:"
    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|"
    r"\bAKIA[0-9A-Z]{16}\b|"
    r"\bgh[pousr]_[A-Za-z0-9]{20,}\b|"
    r"\bsk-[A-Za-z0-9]{16,}\b|"
    r"\b(?:api[_-]?key|secret|token|password|passwd|private[_-]?key)\s*[:=]\s*"
    r"['\"]?[A-Za-z0-9_\-./]{12,}|"
    r"\bBearer\s+[A-Za-z0-9._\-]{20,}|"
    r"\b(?:postgres(?:ql)?|mysql|redis)://[^:\s]+:[^@\s]+@"
    r")",
    re.IGNORECASE,
)

_INJECTION_PATTERNS = re.compile(
    r"(?:"
    r"ignore\s+(?:all\s+|the\s+|your\s+)?(?:previous|prior|above)\s+"
    r"(?:instructions?|prompts?)|"
    r"disregard\s+(?:all\s+|the\s+|your\s+)?(?:previous|prior|above)|"
    r"forget\s+(?:all\s+|the\s+|your\s+)?(?:previous|prior|above)|"
    r"system\s+prompt|"
    r"you\s+are\s+now\b|"
    r"reveal\s+(?:the\s+)?(?:system|hidden)\s+prompt|"
    r"jailbreak|developer\s+mode|"
    r"do\s+not\s+(?:tell|inform)\s+the\s+user"
    r")",
    re.IGNORECASE,
)


class CandidateIssue(Model):
    code: str
    message: str


class CandidateValidation(Model):
    candidate_id: str
    accepted: bool
    requires_confirmation: bool
    auto_confirmable: bool
    issues: list[CandidateIssue] = Field(default_factory=list)


class MemoryConfirmation(Model):
    """A human confirmation bound to one exact candidate digest."""

    confirmed_by: Annotated[str, Field(pattern=_IDENTITY)]
    reason: Annotated[str, Field(min_length=1, max_length=500)]
    candidate_digest: Annotated[str, Field(pattern=_HASH)]


class MemoryCandidate(Model):
    schema_version: str = CANDIDATE_SCHEMA
    trigger: CandidateTrigger
    outcome: MemoryOutcome
    project_id: Annotated[str, Field(pattern=_IDENTITY)]
    repository: Annotated[str, Field(pattern=_REPOSITORY)]
    commit: Annotated[str | None, Field(pattern=_COMMIT)] = None
    actor: Annotated[str, Field(pattern=_IDENTITY)]
    memory_kind: MemoryKind
    scope: MemoryScope
    subject_key: Annotated[str, Field(pattern=_SUBJECT)]
    summary: Annotated[str, Field(min_length=1, max_length=2_000)]
    summary_hash: Annotated[str, Field(pattern=_HASH)]
    evidence_ids: Annotated[list[UUID], Field(max_length=64)] = Field(default_factory=list)
    artifact_hashes: Annotated[list[str], Field(max_length=64)] = Field(default_factory=list)
    confidence: Annotated[float, Field(ge=0.0, le=1.0)] = 0.5
    verification_status: MemoryVerification = "supported"
    provenance: CandidateProvenance = "agent_reported"
    high_risk: bool = False
    detail: MemoryDetail

    @field_validator("repository")
    @classmethod
    def normalize_repository(cls, value: str) -> str:
        return value.lower()

    @field_validator("evidence_ids")
    @classmethod
    def unique_evidence(cls, value: list[UUID]) -> list[UUID]:
        return list(dict.fromkeys(value))

    @field_validator("artifact_hashes")
    @classmethod
    def unique_artifacts(cls, value: list[str]) -> list[str]:
        if any(re.fullmatch(_HASH, item) is None for item in value):
            raise ValueError("artifact hashes must be lowercase SHA-256 values")
        return list(dict.fromkeys(value))

    @model_validator(mode="after")
    def consistent_candidate(self) -> MemoryCandidate:
        if self.detail.kind != self.memory_kind:
            raise ValueError("detail kind must match memory_kind")
        if self.outcome not in TRIGGER_OUTCOMES[self.trigger]:
            raise ValueError(f"outcome {self.outcome} is not valid for {self.trigger}")
        if self.scope == "commit" and self.commit is None:
            raise ValueError("commit-scoped candidate requires a commit")
        if self.scope != "commit" and self.commit is not None:
            raise ValueError("project/repository-scoped candidate must not pin a commit")
        return self

    @property
    def candidate_id(self) -> str:
        return digest(self.model_dump(mode="json"))

    @property
    def resolved_subject(self) -> str:
        return f"{self.trigger}:{self.subject_key}:{self.outcome}"

    def to_draft(self) -> MemoryDraft:
        detail = self.detail
        if isinstance(detail, EpisodicDetail):
            detail = detail.model_copy(
                update={"event_kind": f"{self.trigger}:{self.outcome}"[:100]}
            )
        return MemoryDraft(
            project_id=self.project_id,
            repository=self.repository,
            commit=self.commit,
            actor=self.actor,
            memory_kind=self.memory_kind,
            scope=self.scope,
            subject_key=self.resolved_subject,
            summary_hash=self.summary_hash,
            evidence_ids=list(self.evidence_ids),
            artifact_hashes=list(self.artifact_hashes),
            confidence=self.confidence,
            verification_status=self.verification_status,
            detail=detail,
        )


def execution_candidate(
    *,
    trigger: CandidateTrigger,
    outcome: MemoryOutcome,
    project_id: str,
    repository: str,
    actor: str,
    subject_key: str,
    summary: str,
    summary_hash: str,
    ticket_id: UUID,
    run_id: UUID,
    occurred_at: datetime,
    evidence_ids: Sequence[UUID] = (),
    artifact_hashes: Sequence[str] = (),
    confidence: float = 0.7,
    verification_status: MemoryVerification = "supported",
    provenance: CandidateProvenance = "agent_reported",
    high_risk: bool = False,
    commit: str | None = None,
    scope: MemoryScope | None = None,
) -> MemoryCandidate:
    """Build one episodic experience candidate from an execution moment."""

    return MemoryCandidate(
        trigger=trigger,
        outcome=outcome,
        project_id=project_id,
        repository=repository,
        commit=commit,
        actor=actor,
        memory_kind="episodic",
        scope=scope or ("commit" if commit is not None else "repository"),
        subject_key=subject_key,
        summary=summary,
        summary_hash=summary_hash,
        evidence_ids=list(evidence_ids),
        artifact_hashes=list(artifact_hashes),
        confidence=confidence,
        verification_status=verification_status,
        provenance=provenance,
        high_risk=high_risk,
        detail=EpisodicDetail(
            ticket_id=ticket_id,
            run_id=run_id,
            event_kind=trigger,
            occurred_at=occurred_at,
        ),
    )


def candidate_from_finding(
    finding: Finding,
    *,
    actor: str,
    project_id: str,
    ticket_id: UUID,
    run_id: UUID,
    summary_hash: str,
    occurred_at: datetime,
    evidence_ids: Sequence[UUID] = (),
    artifact_hashes: Sequence[str] = (),
    trigger: CandidateTrigger = "finding_evidence_added",
) -> MemoryCandidate:
    """Convert an A03 Finding into an execution-experience candidate."""

    suffix = f" | status={finding.lifecycle_status} | risk={finding.fix_risk}"
    return execution_candidate(
        trigger=trigger,
        outcome=_finding_outcome(finding.lifecycle_status),
        project_id=project_id,
        repository=finding.repository,
        actor=actor,
        subject_key=f"finding:{finding.finding_id[:24]}",
        summary=(finding.summary + suffix)[:2_000],
        summary_hash=summary_hash,
        ticket_id=ticket_id,
        run_id=run_id,
        occurred_at=occurred_at,
        evidence_ids=evidence_ids,
        artifact_hashes=artifact_hashes,
        confidence=finding.confidence,
        verification_status="verified" if evidence_ids else "supported",
        provenance="mechanical" if evidence_ids else "agent_reported",
        high_risk=finding.fix_risk == "high",
        commit=finding.commit,
        scope="commit",
    )


def _finding_outcome(status: FindingLifecycle) -> MemoryOutcome:
    if status == "not_reproduced":
        return "failure"
    if status in {"reproduced", "confirmed_static", "patch_verified"}:
        return "success"
    return "neutral"


class MemoryWriter:
    """Validation and confirmation gate in front of ``MemoryStore``."""

    def __init__(self, settings: Settings, store: MemoryStore | None = None):
        self.settings = settings
        self.store = store or MemoryStore(settings)

    def validate(
        self, candidate: MemoryCandidate, *, actor: str | None = None
    ) -> CandidateValidation:
        actor = self._actor(actor)
        self._authorize(candidate, actor)
        issues: list[CandidateIssue] = []
        if candidate.memory_kind == "working":
            issues.append(
                CandidateIssue(
                    code="KIND_NOT_WRITABLE",
                    message="working memory is ephemeral and cannot be confirmed",
                )
            )
        issues.extend(self._content_issues(candidate))
        if candidate.provenance == "inferred":
            issues.append(
                CandidateIssue(
                    code="UNVERIFIED_INFERENCE",
                    message="inferred content cannot be written as confirmed memory",
                )
            )
        if candidate.verification_status == "disputed":
            issues.append(
                CandidateIssue(
                    code="DISPUTED_EVIDENCE",
                    message="disputed content cannot be confirmed",
                )
            )
        if candidate.verification_status == "verified" and not candidate.evidence_ids:
            issues.append(
                CandidateIssue(
                    code="EVIDENCE_REQUIRED",
                    message="verified candidate requires at least one evidence reference",
                )
            )
        if not candidate.evidence_ids and not candidate.artifact_hashes:
            issues.append(
                CandidateIssue(
                    code="UNVERIFIED_INFERENCE",
                    message="candidate has no evidence or artifact provenance",
                )
            )
        issues.extend(self._reference_issues(candidate))
        requires_confirmation = self._requires_confirmation(candidate)
        accepted = not issues
        return CandidateValidation(
            candidate_id=candidate.candidate_id,
            accepted=accepted,
            requires_confirmation=requires_confirmation,
            auto_confirmable=accepted and not requires_confirmation,
            issues=issues,
        )

    def write(
        self,
        candidate: MemoryCandidate,
        *,
        actor: str | None = None,
        confirmation: MemoryConfirmation | None = None,
    ) -> MemoryRecord:
        validation = self.validate(candidate, actor=actor)
        if not validation.accepted:
            codes = ",".join(sorted({issue.code for issue in validation.issues}))
            raise DomainError(
                "MEMORY_CANDIDATE_REJECTED", f"candidate rejected: {codes}", 422
            )
        if validation.requires_confirmation and confirmation is None:
            raise DomainError(
                "MEMORY_CONFIRMATION_REQUIRED",
                "candidate requires explicit human confirmation",
                409,
            )
        if confirmation is not None:
            self._check_confirmation(candidate, confirmation)
        return self.store.create(candidate.to_draft(), actor=actor)

    def _requires_confirmation(self, candidate: MemoryCandidate) -> bool:
        return candidate.memory_kind in {"semantic", "procedural"} or candidate.high_risk

    def _check_confirmation(
        self, candidate: MemoryCandidate, confirmation: MemoryConfirmation
    ) -> None:
        if confirmation.candidate_digest != candidate.candidate_id:
            raise DomainError(
                "MEMORY_CONFIRMATION_INVALID",
                "confirmation is not bound to this candidate",
                403,
            )
        if confirmation.confirmed_by not in self.settings.allowed_actors:
            raise DomainError(
                "MEMORY_CONFIRMATION_INVALID",
                "confirmer is not allowed to confirm memory",
                403,
            )
        if confirmation.confirmed_by == candidate.actor:
            raise DomainError(
                "MEMORY_CONFIRMATION_INVALID",
                "a candidate cannot confirm itself",
                403,
            )

    def _content_issues(self, candidate: MemoryCandidate) -> list[CandidateIssue]:
        issues: list[CandidateIssue] = []
        for field, value in _text_fields(candidate).items():
            if _SECRET_PATTERNS.search(value):
                issues.append(
                    CandidateIssue(
                        code="SENSITIVE_CONTENT",
                        message=f"{field} contains credential-like content",
                    )
                )
            if _INJECTION_PATTERNS.search(value):
                issues.append(
                    CandidateIssue(
                        code="PROMPT_INJECTION",
                        message=f"{field} contains instruction-like content",
                    )
                )
        return issues

    def _reference_issues(self, candidate: MemoryCandidate) -> list[CandidateIssue]:
        issues: list[CandidateIssue] = []
        with self._connection() as conn:
            if not _artifact_exists(conn, candidate.summary_hash):
                issues.append(
                    CandidateIssue(
                        code="ARTIFACT_NOT_FOUND",
                        message="summary artifact does not exist",
                    )
                )
            for content_hash in candidate.artifact_hashes:
                if not _artifact_exists(conn, content_hash):
                    issues.append(
                        CandidateIssue(
                            code="ARTIFACT_NOT_FOUND",
                            message=f"artifact {content_hash} does not exist",
                        )
                    )
            for evidence_id in candidate.evidence_ids:
                issues.extend(_evidence_issues(conn, evidence_id, candidate))
            detail = candidate.detail
            if isinstance(detail, EpisodicDetail):
                issues.extend(_run_issues(conn, detail.run_id, candidate))
                issues.extend(_ticket_issues(conn, detail.ticket_id, candidate))
                run = conn.execute(
                    "SELECT ticket_id FROM app.runs WHERE run_id=%s", (detail.run_id,)
                ).fetchone()
                if run is not None and UUID(str(run["ticket_id"])) != detail.ticket_id:
                    issues.append(
                        CandidateIssue(
                            code="REFERENCE_NOT_FOUND",
                            message="episodic run does not belong to the referenced ticket",
                        )
                    )
            elif isinstance(detail, ProceduralDetail) and not _artifact_exists(
                conn, detail.recipe_hash
            ):
                issues.append(
                    CandidateIssue(
                        code="ARTIFACT_NOT_FOUND",
                        message="procedural recipe artifact does not exist",
                    )
                )
        return issues

    def _connection(self) -> Connection:
        return psycopg.connect(
            self.settings.database_url.get_secret_value(), row_factory=dict_row
        )

    def _actor(self, actor: str | None) -> str:
        value = self.settings.local_actor if actor is None else actor
        if value not in self.settings.allowed_actors:
            raise DomainError(
                "MEMORY_ACTOR_DENIED", "Actor is not allowed to write memory", 403
            )
        return value

    def _authorize(self, candidate: MemoryCandidate, actor: str) -> None:
        if candidate.actor != actor:
            raise DomainError("MEMORY_ACTOR_DENIED", "Candidate actor does not match", 403)
        if candidate.project_id != self.settings.project_id:
            raise DomainError("MEMORY_SCOPE_DENIED", "Candidate project is outside this scope", 403)
        if candidate.repository != self.settings.repository.lower():
            raise DomainError(
                "MEMORY_SCOPE_DENIED", "Candidate repository is outside this scope", 403
            )


def _text_fields(candidate: MemoryCandidate) -> dict[str, str]:
    fields = {"summary": candidate.summary, "subject_key": candidate.subject_key}
    detail = candidate.detail
    if isinstance(detail, EpisodicDetail):
        fields["event_kind"] = detail.event_kind
    elif isinstance(detail, SemanticDetail):
        fields["semantic_subject"] = detail.subject
        fields["semantic_predicate"] = detail.predicate
        fields["semantic_object"] = detail.object_value
    elif isinstance(detail, ProceduralDetail):
        fields["task_key"] = detail.task_key
    elif isinstance(detail, WorkingDetail):
        fields["run_id"] = str(detail.run_id)
    return fields


def _artifact_exists(conn: Connection, content_hash: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM app.artifacts WHERE content_hash=%s", (content_hash,)
        ).fetchone()
        is not None
    )


def _evidence_issues(
    conn: Connection, evidence_id: UUID, candidate: MemoryCandidate
) -> list[CandidateIssue]:
    row = conn.execute(
        "SELECT e.project_id,th.project_id AS thread_project,th.repository "
        "FROM app.evidence e JOIN app.runs r USING(run_id) "
        "JOIN app.tickets t USING(ticket_id) JOIN app.threads th USING(thread_id) "
        "WHERE e.evidence_id=%s",
        (evidence_id,),
    ).fetchone()
    if row is None:
        return [
            CandidateIssue(
                code="EVIDENCE_NOT_FOUND", message=f"evidence {evidence_id} does not exist"
            )
        ]
    if (
        row["project_id"] != candidate.project_id
        or row["thread_project"] != candidate.project_id
        or row["repository"] != candidate.repository
    ):
        return [
            CandidateIssue(
                code="EVIDENCE_SCOPE_DENIED",
                message="evidence belongs to another project or repository",
            )
        ]
    return []


def _run_issues(
    conn: Connection, run_id: UUID, candidate: MemoryCandidate
) -> list[CandidateIssue]:
    row = conn.execute(
        "SELECT th.project_id,th.repository FROM app.runs r "
        "JOIN app.tickets t USING(ticket_id) JOIN app.threads th USING(thread_id) "
        "WHERE r.run_id=%s",
        (run_id,),
    ).fetchone()
    if row is None:
        return [
            CandidateIssue(code="REFERENCE_NOT_FOUND", message=f"run {run_id} does not exist")
        ]
    if row["project_id"] != candidate.project_id or row["repository"] != candidate.repository:
        return [
            CandidateIssue(
                code="REFERENCE_SCOPE_DENIED", message="run belongs to another scope"
            )
        ]
    return []


def _ticket_issues(
    conn: Connection, ticket_id: UUID, candidate: MemoryCandidate
) -> list[CandidateIssue]:
    row = conn.execute(
        "SELECT th.project_id,th.repository FROM app.tickets t "
        "JOIN app.threads th USING(thread_id) WHERE t.ticket_id=%s",
        (ticket_id,),
    ).fetchone()
    if row is None:
        return [
            CandidateIssue(
                code="REFERENCE_NOT_FOUND", message=f"ticket {ticket_id} does not exist"
            )
        ]
    if row["project_id"] != candidate.project_id or row["repository"] != candidate.repository:
        return [
            CandidateIssue(
                code="REFERENCE_SCOPE_DENIED", message="ticket belongs to another scope"
            )
        ]
    return []
