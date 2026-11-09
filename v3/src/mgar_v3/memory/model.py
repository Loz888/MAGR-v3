"""V3.3 M01 memory contracts.

Four memory kinds share one identity: project / repository / commit scope,
actor, kind, scope and a stable subject key.  Kind-specific payloads stay in
narrow detail models.  Content is append-only; supersede, revoke and stale are
explicit lifecycle transitions rather than in-place rewrites.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from mgar_v3.domain import Model

MEMORY_SCHEMA = "v33-memory-v1"

MemoryKind = Literal["working", "episodic", "semantic", "procedural"]
MemoryScope = Literal["project", "repository", "commit"]
MemoryLifecycle = Literal["active", "superseded", "revoked", "stale"]
MemoryVerification = Literal["unverified", "supported", "verified", "disputed"]

MEMORY_KINDS: tuple[MemoryKind, ...] = ("working", "episodic", "semantic", "procedural")
MEMORY_SCOPES: tuple[MemoryScope, ...] = ("project", "repository", "commit")
MEMORY_LIFECYCLES: tuple[MemoryLifecycle, ...] = ("active", "superseded", "revoked", "stale")
MEMORY_VERIFICATIONS: tuple[MemoryVerification, ...] = (
    "unverified",
    "supported",
    "verified",
    "disputed",
)

_IDENTITY = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
_REPOSITORY = r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$"
_COMMIT = r"^[a-f0-9]{40}$"
_HASH = r"^[a-f0-9]{64}$"
_SUBJECT = r"^[A-Za-z0-9][A-Za-z0-9._:/#-]{0,199}$"
_SCOPE_KEY = r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$"
_REASON = re.compile(r"^\S(?:.*\S)?$", re.DOTALL)


class WorkingDetail(Model):
    kind: Literal["working"] = "working"
    run_id: UUID
    expires_at: datetime


class EpisodicDetail(Model):
    kind: Literal["episodic"] = "episodic"
    ticket_id: UUID
    run_id: UUID
    event_kind: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,99}$")]
    occurred_at: datetime


class SemanticDetail(Model):
    kind: Literal["semantic"] = "semantic"
    subject: Annotated[str, Field(min_length=1, max_length=200)]
    predicate: Annotated[str, Field(min_length=1, max_length=200)]
    object_value: Annotated[str, Field(min_length=1, max_length=2000)]


class ProceduralDetail(Model):
    kind: Literal["procedural"] = "procedural"
    task_key: Annotated[str, Field(min_length=1, max_length=200)]
    recipe_hash: Annotated[str, Field(pattern=_HASH)]
    success_count: Annotated[int, Field(ge=0)] = 0
    failure_count: Annotated[int, Field(ge=0)] = 0


MemoryDetail = Annotated[
    WorkingDetail | EpisodicDetail | SemanticDetail | ProceduralDetail,
    Field(discriminator="kind"),
]


class MemoryDraft(Model):
    project_id: Annotated[str, Field(pattern=_IDENTITY)]
    repository: Annotated[str, Field(pattern=_REPOSITORY)]
    commit: Annotated[str | None, Field(pattern=_COMMIT)] = None
    actor: Annotated[str, Field(pattern=_IDENTITY)]
    memory_kind: MemoryKind
    scope: MemoryScope
    subject_key: Annotated[str, Field(pattern=_SUBJECT)]
    summary_hash: Annotated[str, Field(pattern=_HASH)]
    evidence_ids: Annotated[list[UUID], Field(max_length=64)] = Field(default_factory=list)
    artifact_hashes: Annotated[list[str], Field(max_length=64)] = Field(default_factory=list)
    confidence: Annotated[float, Field(ge=0.0, le=1.0)] = 0.5
    verification_status: MemoryVerification = "unverified"
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
    def consistent_identity(self) -> MemoryDraft:
        if self.detail.kind != self.memory_kind:
            raise ValueError("detail kind must match memory_kind")
        if self.scope == "commit" and self.commit is None:
            raise ValueError("commit-scoped memory requires a commit")
        if self.scope != "commit" and self.commit is not None:
            raise ValueError("project/repository-scoped memory must not pin a commit")
        return self

    @property
    def scope_key(self) -> str:
        if self.scope == "commit":
            assert self.commit is not None
            return self.commit
        if self.scope == "repository":
            return self.repository
        return self.project_id


class MemoryRecord(Model):
    memory_id: UUID
    memory_kind: MemoryKind
    project_id: str
    repository: str
    commit: str | None
    actor: str
    scope: MemoryScope
    scope_key: str
    subject_key: str
    summary_hash: str
    evidence_ids: list[UUID]
    artifact_hashes: list[str]
    confidence: float
    verification_status: MemoryVerification
    detail: MemoryDetail
    status: MemoryLifecycle = "active"
    superseded_by: UUID | None = None
    revoked_at: datetime | None = None
    revoked_reason: str | None = None
    stale_at: datetime | None = None
    stale_reason: str | None = None
    legacy: bool = False
    created_at: datetime
    updated_at: datetime

    @property
    def identity(self) -> tuple[str, str, str, str, str, str, str]:
        return (
            self.project_id,
            self.repository,
            self.actor,
            self.memory_kind,
            self.scope,
            self.scope_key,
            self.subject_key,
        )

    @property
    def current(self) -> bool:
        return self.status in {"active", "stale"}


def require_reason(reason: str) -> str:
    if not _REASON.fullmatch(reason.strip()):
        raise ValueError("lifecycle reason must be non-empty")
    return reason.strip()
