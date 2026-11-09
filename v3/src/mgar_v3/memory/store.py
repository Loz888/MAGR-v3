"""PostgreSQL store for the V3.3 M01 memory contract.

New records live in ``knowledge.memory_records`` plus one kind-specific detail
table.  The legacy ``knowledge.memories`` table is never rewritten: legacy
rows are readable through this store and can be imported as a linked record.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from mgar_v3.domain import DomainError
from mgar_v3.memory.model import (
    EpisodicDetail,
    MemoryDetail,
    MemoryDraft,
    MemoryKind,
    MemoryLifecycle,
    MemoryRecord,
    MemoryScope,
    MemoryVerification,
    ProceduralDetail,
    SemanticDetail,
    WorkingDetail,
    require_reason,
)
from mgar_v3.settings import Settings

Row = dict[str, Any]
Connection = psycopg.Connection[Row]
_TERMINAL = {"superseded", "revoked"}


def _uuid_list(value: Any) -> list[UUID]:
    return [UUID(str(item)) for item in cast(list[Any], value)]


def _hash_list(value: Any) -> list[str]:
    return [str(item) for item in cast(list[Any], value)]


class MemoryStore:
    """Scoped, append-only memory access with an explicit lifecycle."""

    def __init__(self, settings: Settings):
        self.settings = settings

    def create(self, draft: MemoryDraft, *, actor: str | None = None) -> MemoryRecord:
        actor = self._actor(actor)
        self._authorize_draft(draft, actor)
        memory_id = uuid4()
        with self._connection() as conn:
            self._check_references(conn, draft)
            try:
                self._insert(conn, memory_id, draft)
            except psycopg.errors.UniqueViolation as exc:
                raise DomainError(
                    "MEMORY_CONFLICT",
                    "an active memory already exists for this identity; supersede it",
                    409,
                ) from exc
            return self._record(conn, self._fetch(conn, memory_id))

    def get(
        self, memory_id: UUID, *, actor: str | None = None, include_inactive: bool = False
    ) -> MemoryRecord:
        actor = self._actor(actor)
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM knowledge.memory_records WHERE "
                "(memory_id=%s OR legacy_memory_id=%s) AND project_id=%s AND "
                "repository=%s AND actor=%s ORDER BY (memory_id=%s) DESC LIMIT 1",
                (
                    memory_id,
                    memory_id,
                    self.settings.project_id,
                    self._repository,
                    actor,
                    memory_id,
                ),
            ).fetchone()
            if row is not None:
                record = self._record(conn, row)
                if not include_inactive and record.status in _TERMINAL:
                    raise _not_found()
                return record
            legacy = conn.execute(
                "SELECT * FROM knowledge.memories WHERE memory_id=%s AND project_id=%s "
                "AND repository=%s AND actor=%s AND revoked_at IS NULL",
                (memory_id, self.settings.project_id, self._repository, actor),
            ).fetchone()
            if legacy is None:
                raise _not_found()
            return _legacy_record(legacy)

    def current(
        self,
        *,
        memory_kind: MemoryKind,
        scope: MemoryScope,
        subject_key: str,
        commit: str | None = None,
        actor: str | None = None,
    ) -> MemoryRecord | None:
        actor = self._actor(actor)
        scope_key = self._scope_key(scope, commit)
        with self._connection() as conn:
            row = conn.execute(
                "SELECT m.* FROM knowledge.memory_records m LEFT JOIN "
                "knowledge.memory_working w ON w.memory_id=m.memory_id WHERE "
                "m.project_id=%s AND m.repository=%s AND m.actor=%s AND "
                "m.memory_kind=%s AND m.scope_kind=%s AND m.scope_key=%s AND "
                "m.subject_key=%s AND m.status IN ('active','stale') AND "
                "(m.memory_kind <> 'working' OR w.expires_at > now()) "
                "ORDER BY m.created_at DESC, m.memory_id DESC LIMIT 1",
                (
                    self.settings.project_id,
                    self._repository,
                    actor,
                    memory_kind,
                    scope,
                    scope_key,
                    subject_key,
                ),
            ).fetchone()
            return None if row is None else self._record(conn, row)

    def history(
        self,
        *,
        memory_kind: MemoryKind,
        scope: MemoryScope,
        subject_key: str,
        commit: str | None = None,
        actor: str | None = None,
    ) -> list[MemoryRecord]:
        actor = self._actor(actor)
        scope_key = self._scope_key(scope, commit)
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM knowledge.memory_records WHERE project_id=%s AND "
                "repository=%s AND actor=%s AND memory_kind=%s AND scope_kind=%s AND "
                "scope_key=%s AND subject_key=%s ORDER BY created_at, memory_id",
                (
                    self.settings.project_id,
                    self._repository,
                    actor,
                    memory_kind,
                    scope,
                    scope_key,
                    subject_key,
                ),
            ).fetchall()
            return [self._record(conn, row) for row in rows]

    def recall(
        self,
        *,
        actor: str | None = None,
        memory_kinds: Sequence[MemoryKind] | None = None,
        scope: MemoryScope | None = None,
        commit: str | None = None,
        limit: int = 32,
        include_stale: bool = False,
    ) -> list[MemoryRecord]:
        """Permission-scoped records; stale entries are opt-in for retrieval."""

        actor = self._actor(actor)
        if not 1 <= limit <= 64:
            raise DomainError("MEMORY_BOUND_INVALID", "recall limit is out of range", 400)
        clauses = [
            "m.project_id=%s",
            "m.repository=%s",
            "m.actor=%s",
            "m.status IN ('active','stale')" if include_stale else "m.status='active'",
            "(m.memory_kind <> 'working' OR w.expires_at > now())",
        ]
        params: list[Any] = [self.settings.project_id, self._repository, actor]
        if memory_kinds:
            clauses.append("m.memory_kind = ANY(%s)")
            params.append(list(memory_kinds))
        if scope is not None:
            clauses.append("m.scope_kind=%s")
            params.append(scope)
            clauses.append("m.scope_key=%s")
            params.append(self._scope_key(scope, commit))
        elif commit is not None:
            clauses.append("m.scope_kind='commit' AND m.scope_key=%s")
            params.append(commit)
        params.append(limit)
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT m.* FROM knowledge.memory_records m LEFT JOIN "
                "knowledge.memory_working w ON w.memory_id=m.memory_id WHERE "
                + " AND ".join(clauses)
                + " ORDER BY m.created_at DESC, m.memory_id DESC LIMIT %s",
                params,
            ).fetchall()
            return [self._record(conn, row) for row in rows]

    def legacy(self, *, actor: str | None = None, limit: int = 32) -> list[MemoryRecord]:
        """Read old confirmed memories without changing their storage."""

        actor = self._actor(actor)
        if not 1 <= limit <= 64:
            raise DomainError("MEMORY_BOUND_INVALID", "legacy limit is out of range", 400)
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM knowledge.memories WHERE project_id=%s AND repository=%s "
                "AND actor=%s AND revoked_at IS NULL ORDER BY confirmed_at DESC LIMIT %s",
                (self.settings.project_id, self._repository, actor, limit),
            ).fetchall()
            return [_legacy_record(row) for row in rows]

    def supersede(
        self, memory_id: UUID, draft: MemoryDraft, *, actor: str | None = None
    ) -> MemoryRecord:
        actor = self._actor(actor)
        self._authorize_draft(draft, actor)
        new_id = uuid4()
        with self._connection() as conn:
            old_row = self._fetch(conn, memory_id)
            old = self._record(conn, old_row)
            self._authorize_record(old, actor)
            if old.status not in {"active", "stale"}:
                raise _transition_invalid(f"{old.status} memory cannot be superseded")
            if old.identity != _draft_identity(draft):
                raise DomainError(
                    "MEMORY_IDENTITY_MISMATCH",
                    "a superseding memory must keep the same identity",
                    409,
                )
            self._check_references(conn, draft)
            updated = conn.execute(
                "UPDATE knowledge.memory_records SET status='superseded', superseded_by=%s "
                "WHERE memory_id=%s AND status IN ('active','stale') RETURNING memory_id",
                (new_id, memory_id),
            ).fetchone()
            if updated is None:
                raise _transition_invalid("memory changed before supersede committed")
            self._insert(conn, new_id, draft)
            return self._record(conn, self._fetch(conn, new_id))

    def revoke(self, memory_id: UUID, reason: str, *, actor: str | None = None) -> MemoryRecord:
        actor = self._actor(actor)
        reason = self._reason(reason)
        with self._connection() as conn:
            record = self._record(conn, self._fetch(conn, memory_id))
            self._authorize_record(record, actor)
            if record.status in _TERMINAL:
                raise _transition_invalid(f"{record.status} memory cannot be revoked")
            updated = conn.execute(
                "UPDATE knowledge.memory_records SET status='revoked', revoked_at=now(), "
                "revoked_reason=%s WHERE memory_id=%s AND status IN ('active','stale') "
                "RETURNING memory_id",
                (reason, memory_id),
            ).fetchone()
            if updated is None:
                raise _transition_invalid("memory changed before revoke committed")
            return self._record(conn, self._fetch(conn, memory_id))

    def mark_stale(
        self, memory_id: UUID, reason: str, *, actor: str | None = None
    ) -> MemoryRecord:
        actor = self._actor(actor)
        reason = self._reason(reason)
        with self._connection() as conn:
            record = self._record(conn, self._fetch(conn, memory_id))
            self._authorize_record(record, actor)
            if record.status == "stale":
                return record
            if record.status in _TERMINAL:
                raise _transition_invalid(f"{record.status} memory cannot become stale")
            updated = conn.execute(
                "UPDATE knowledge.memory_records SET status='stale', stale_at=now(), "
                "stale_reason=%s WHERE memory_id=%s AND status='active' "
                "RETURNING memory_id",
                (reason, memory_id),
            ).fetchone()
            if updated is None:
                raise _transition_invalid("memory changed before stale marker committed")
            return self._record(conn, self._fetch(conn, memory_id))

    def import_legacy(self, memory_id: UUID, *, actor: str | None = None) -> MemoryRecord:
        """Create a linked new-contract record for one confirmed legacy memory."""

        actor = self._actor(actor)
        with self._connection() as conn:
            existing = conn.execute(
                "SELECT * FROM knowledge.memory_records WHERE legacy_memory_id=%s AND "
                "project_id=%s AND repository=%s AND actor=%s",
                (memory_id, self.settings.project_id, self._repository, actor),
            ).fetchone()
            if existing is not None:
                return self._record(conn, existing)
            legacy = conn.execute(
                "SELECT * FROM knowledge.memories WHERE memory_id=%s AND project_id=%s "
                "AND repository=%s AND actor=%s AND revoked_at IS NULL",
                (memory_id, self.settings.project_id, self._repository, actor),
            ).fetchone()
            if legacy is None:
                raise DomainError("MEMORY_LEGACY_NOT_FOUND", "Legacy memory was not found", 404)
            evidence_ids = _uuid_list(legacy["evidence_ids"])
            draft = MemoryDraft(
                project_id=str(legacy["project_id"]),
                repository=str(legacy["repository"]),
                actor=str(legacy["actor"]),
                memory_kind="episodic",
                scope="repository",
                subject_key=f"legacy:{memory_id}",
                summary_hash=str(legacy["summary_hash"]),
                evidence_ids=evidence_ids,
                artifact_hashes=[str(legacy["report_hash"])],
                confidence=1.0,
                verification_status="verified" if evidence_ids else "supported",
                detail=EpisodicDetail(
                    ticket_id=UUID(str(legacy["ticket_id"])),
                    run_id=UUID(str(legacy["run_id"])),
                    event_kind="legacy_confirmation",
                    occurred_at=legacy["confirmed_at"],
                ),
            )
            self._check_references(conn, draft)
            new_id = uuid4()
            self._insert(conn, new_id, draft, legacy_memory_id=memory_id)
            return self._record(conn, self._fetch(conn, new_id))

    @property
    def _repository(self) -> str:
        return self.settings.repository.lower()

    def _connection(self) -> Connection:
        return psycopg.connect(
            self.settings.database_url.get_secret_value(), row_factory=dict_row
        )

    def _actor(self, actor: str | None) -> str:
        value = self.settings.local_actor if actor is None else actor
        if value not in self.settings.allowed_actors:
            raise DomainError(
                "MEMORY_ACTOR_DENIED", "Actor is not allowed to access memory", 403
            )
        return value

    def _authorize_draft(self, draft: MemoryDraft, actor: str) -> None:
        if draft.actor != actor:
            raise DomainError("MEMORY_ACTOR_DENIED", "Memory actor does not match", 403)
        if draft.project_id != self.settings.project_id:
            raise DomainError("MEMORY_SCOPE_DENIED", "Memory project is outside this scope", 403)
        if draft.repository != self._repository:
            raise DomainError(
                "MEMORY_SCOPE_DENIED", "Memory repository is outside this scope", 403
            )

    def _authorize_record(self, record: MemoryRecord, actor: str) -> None:
        if (
            record.project_id != self.settings.project_id
            or record.repository != self._repository
            or record.actor != actor
        ):
            raise _not_found()

    def _scope_key(self, scope: MemoryScope, commit: str | None) -> str:
        if scope == "commit":
            if commit is None:
                raise DomainError("MEMORY_SCOPE_INVALID", "commit scope requires a commit", 400)
            return commit
        if scope == "repository":
            return self._repository
        return self.settings.project_id

    def _reason(self, reason: str) -> str:
        try:
            return require_reason(reason)
        except ValueError as exc:
            raise DomainError(
                "MEMORY_REASON_REQUIRED", "A lifecycle reason is required", 400
            ) from exc

    def _fetch(self, conn: Connection, memory_id: UUID) -> Row:
        row = conn.execute(
            "SELECT * FROM knowledge.memory_records WHERE memory_id=%s", (memory_id,)
        ).fetchone()
        if row is None:
            raise _not_found()
        return row

    def _check_references(self, conn: Connection, draft: MemoryDraft) -> None:
        self._require_artifact(conn, draft.summary_hash)
        for content_hash in draft.artifact_hashes:
            self._require_artifact(conn, content_hash)
        for evidence_id in draft.evidence_ids:
            self._require_evidence(conn, evidence_id, draft)
        detail = draft.detail
        if draft.verification_status == "verified" and not draft.evidence_ids:
            raise DomainError(
                "MEMORY_EVIDENCE_REQUIRED",
                "verified memory requires at least one evidence reference",
                409,
            )
        if isinstance(detail, WorkingDetail):
            self._require_run(conn, detail.run_id, draft)
        elif isinstance(detail, EpisodicDetail):
            self._require_ticket(conn, detail.ticket_id, draft)
            self._require_run(conn, detail.run_id, draft)
            run = conn.execute(
                "SELECT ticket_id FROM app.runs WHERE run_id=%s", (detail.run_id,)
            ).fetchone()
            if run is None or UUID(str(run["ticket_id"])) != detail.ticket_id:
                raise DomainError(
                    "MEMORY_REFERENCE_INVALID",
                    "episodic run does not belong to the referenced ticket",
                    409,
                )
        elif isinstance(detail, ProceduralDetail):
            self._require_artifact(conn, detail.recipe_hash)

    def _require_artifact(self, conn: Connection, content_hash: str) -> None:
        row = conn.execute(
            "SELECT 1 FROM app.artifacts WHERE content_hash=%s", (content_hash,)
        ).fetchone()
        if row is None:
            raise DomainError(
                "MEMORY_REFERENCE_INVALID",
                f"artifact {content_hash} does not exist",
                409,
            )

    def _require_evidence(
        self, conn: Connection, evidence_id: UUID, draft: MemoryDraft
    ) -> None:
        row = conn.execute(
            "SELECT e.project_id,th.project_id AS thread_project,th.repository "
            "FROM app.evidence e JOIN app.runs r USING(run_id) "
            "JOIN app.tickets t USING(ticket_id) JOIN app.threads th USING(thread_id) "
            "WHERE e.evidence_id=%s",
            (evidence_id,),
        ).fetchone()
        if row is None:
            raise DomainError(
                "MEMORY_REFERENCE_INVALID", f"evidence {evidence_id} does not exist", 409
            )
        if (
            row["project_id"] != draft.project_id
            or row["thread_project"] != draft.project_id
            or row["repository"] != draft.repository
        ):
            raise DomainError(
                "MEMORY_SCOPE_DENIED", "evidence belongs to another scope", 403
            )

    def _require_run(self, conn: Connection, run_id: UUID, draft: MemoryDraft) -> None:
        row = conn.execute(
            "SELECT th.project_id,th.repository FROM app.runs r "
            "JOIN app.tickets t USING(ticket_id) JOIN app.threads th USING(thread_id) "
            "WHERE r.run_id=%s",
            (run_id,),
        ).fetchone()
        if row is None:
            raise DomainError("MEMORY_REFERENCE_INVALID", f"run {run_id} does not exist", 409)
        self._check_scope_row(row, draft, "run")

    def _require_ticket(self, conn: Connection, ticket_id: UUID, draft: MemoryDraft) -> None:
        row = conn.execute(
            "SELECT th.project_id,th.repository FROM app.tickets t "
            "JOIN app.threads th USING(thread_id) WHERE t.ticket_id=%s",
            (ticket_id,),
        ).fetchone()
        if row is None:
            raise DomainError(
                "MEMORY_REFERENCE_INVALID", f"ticket {ticket_id} does not exist", 409
            )
        self._check_scope_row(row, draft, "ticket")

    def _check_scope_row(self, row: Row, draft: MemoryDraft, kind: str) -> None:
        if row["project_id"] != draft.project_id or row["repository"] != draft.repository:
            raise DomainError(
                "MEMORY_SCOPE_DENIED", f"{kind} belongs to another scope", 403
            )

    def _insert(
        self,
        conn: Connection,
        memory_id: UUID,
        draft: MemoryDraft,
        *,
        legacy_memory_id: UUID | None = None,
    ) -> None:
        conn.execute(
            "INSERT INTO knowledge.memory_records(memory_id,memory_kind,project_id,"
            "repository,commit_sha,actor,scope_kind,scope_key,subject_key,summary_hash,"
            "evidence_ids,artifact_hashes,confidence,verification_status,legacy_memory_id) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                memory_id,
                draft.memory_kind,
                draft.project_id,
                draft.repository,
                draft.commit,
                draft.actor,
                draft.scope,
                draft.scope_key,
                draft.subject_key,
                draft.summary_hash,
                Jsonb([str(item) for item in draft.evidence_ids]),
                Jsonb(list(draft.artifact_hashes)),
                draft.confidence,
                draft.verification_status,
                legacy_memory_id,
            ),
        )
        detail = draft.detail
        if isinstance(detail, WorkingDetail):
            conn.execute(
                "INSERT INTO knowledge.memory_working(memory_id,run_id,expires_at) "
                "VALUES (%s,%s,%s)",
                (memory_id, detail.run_id, detail.expires_at),
            )
        elif isinstance(detail, EpisodicDetail):
            conn.execute(
                "INSERT INTO knowledge.memory_episodic(memory_id,ticket_id,run_id,"
                "event_kind,occurred_at) VALUES (%s,%s,%s,%s,%s)",
                (
                    memory_id,
                    detail.ticket_id,
                    detail.run_id,
                    detail.event_kind,
                    detail.occurred_at,
                ),
            )
        elif isinstance(detail, SemanticDetail):
            conn.execute(
                "INSERT INTO knowledge.memory_semantic(memory_id,subject,predicate,"
                "object_value) VALUES (%s,%s,%s,%s)",
                (memory_id, detail.subject, detail.predicate, detail.object_value),
            )
        else:
            conn.execute(
                "INSERT INTO knowledge.memory_procedural(memory_id,task_key,recipe_hash,"
                "success_count,failure_count) VALUES (%s,%s,%s,%s,%s)",
                (
                    memory_id,
                    detail.task_key,
                    detail.recipe_hash,
                    detail.success_count,
                    detail.failure_count,
                ),
            )

    def _record(self, conn: Connection, row: Row) -> MemoryRecord:
        memory_id = UUID(str(row["memory_id"]))
        kind = cast(MemoryKind, str(row["memory_kind"]))
        return MemoryRecord(
            memory_id=memory_id,
            memory_kind=kind,
            project_id=str(row["project_id"]),
            repository=str(row["repository"]),
            commit=None if row["commit_sha"] is None else str(row["commit_sha"]).strip(),
            actor=str(row["actor"]),
            scope=cast(MemoryScope, str(row["scope_kind"])),
            scope_key=str(row["scope_key"]),
            subject_key=str(row["subject_key"]),
            summary_hash=str(row["summary_hash"]),
            evidence_ids=_uuid_list(row["evidence_ids"]),
            artifact_hashes=_hash_list(row["artifact_hashes"]),
            confidence=float(row["confidence"]),
            verification_status=cast(MemoryVerification, str(row["verification_status"])),
            detail=self._detail(conn, memory_id, kind),
            status=cast(MemoryLifecycle, str(row["status"])),
            superseded_by=(
                None if row["superseded_by"] is None else UUID(str(row["superseded_by"]))
            ),
            revoked_at=row["revoked_at"],
            revoked_reason=row["revoked_reason"],
            stale_at=row["stale_at"],
            stale_reason=row["stale_reason"],
            legacy=row["legacy_memory_id"] is not None,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _detail(self, conn: Connection, memory_id: UUID, kind: MemoryKind) -> MemoryDetail:
        if kind == "working":
            row = conn.execute(
                "SELECT run_id,expires_at FROM knowledge.memory_working WHERE memory_id=%s",
                (memory_id,),
            ).fetchone()
            if row is None:
                raise _corrupt(memory_id)
            return WorkingDetail(
                run_id=UUID(str(row["run_id"])), expires_at=row["expires_at"]
            )
        if kind == "episodic":
            row = conn.execute(
                "SELECT ticket_id,run_id,event_kind,occurred_at FROM "
                "knowledge.memory_episodic WHERE memory_id=%s",
                (memory_id,),
            ).fetchone()
            if row is None:
                raise _corrupt(memory_id)
            return EpisodicDetail(
                ticket_id=UUID(str(row["ticket_id"])),
                run_id=UUID(str(row["run_id"])),
                event_kind=str(row["event_kind"]),
                occurred_at=row["occurred_at"],
            )
        if kind == "semantic":
            row = conn.execute(
                "SELECT subject,predicate,object_value FROM knowledge.memory_semantic "
                "WHERE memory_id=%s",
                (memory_id,),
            ).fetchone()
            if row is None:
                raise _corrupt(memory_id)
            return SemanticDetail(
                subject=str(row["subject"]),
                predicate=str(row["predicate"]),
                object_value=str(row["object_value"]),
            )
        row = conn.execute(
            "SELECT task_key,recipe_hash,success_count,failure_count FROM "
            "knowledge.memory_procedural WHERE memory_id=%s",
            (memory_id,),
        ).fetchone()
        if row is None:
            raise _corrupt(memory_id)
        return ProceduralDetail(
            task_key=str(row["task_key"]),
            recipe_hash=str(row["recipe_hash"]),
            success_count=int(row["success_count"]),
            failure_count=int(row["failure_count"]),
        )


def _legacy_record(row: Row) -> MemoryRecord:
    memory_id = UUID(str(row["memory_id"]))
    evidence_ids = _uuid_list(row["evidence_ids"])
    confirmed_at = row["confirmed_at"]
    return MemoryRecord(
        memory_id=memory_id,
        memory_kind="episodic",
        project_id=str(row["project_id"]),
        repository=str(row["repository"]),
        commit=None,
        actor=str(row["actor"]),
        scope="repository",
        scope_key=str(row["repository"]),
        subject_key=f"legacy:{memory_id}",
        summary_hash=str(row["summary_hash"]),
        evidence_ids=evidence_ids,
        artifact_hashes=[str(row["report_hash"])],
        confidence=1.0,
        verification_status="verified" if evidence_ids else "supported",
        detail=EpisodicDetail(
            ticket_id=UUID(str(row["ticket_id"])),
            run_id=UUID(str(row["run_id"])),
            event_kind="legacy_confirmation",
            occurred_at=confirmed_at,
        ),
        status="active",
        legacy=True,
        created_at=confirmed_at,
        updated_at=confirmed_at,
    )


def _draft_identity(draft: MemoryDraft) -> tuple[str, str, str, str, str, str, str]:
    return (
        draft.project_id,
        draft.repository,
        draft.actor,
        draft.memory_kind,
        draft.scope,
        draft.scope_key,
        draft.subject_key,
    )


def _not_found() -> DomainError:
    return DomainError("MEMORY_NOT_FOUND", "Memory was not found for this actor and scope", 404)


def _transition_invalid(message: str) -> DomainError:
    return DomainError("MEMORY_TRANSITION_INVALID", message, 409)


def _corrupt(memory_id: UUID) -> DomainError:
    return DomainError(
        "MEMORY_CORRUPT", f"memory {memory_id} has no kind-specific detail row", 500
    )
