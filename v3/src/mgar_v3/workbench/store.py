"""Durable ledger for the unified repository workbench (V3.4 U01)."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from mgar_v3.coordination.engine import Engine
from mgar_v3.domain import DomainError
from mgar_v3.workbench.domain import (
    CoverageRequest,
    CreateWorkbenchTask,
    TaskPhase,
    TaskStatus,
    WorkbenchFinding,
    validate_task_phase,
    validate_task_stage,
)


class WorkbenchStore:
    def __init__(self, engine: Engine):
        self.engine = engine

    def create_task(
        self,
        request: CreateWorkbenchTask,
        *,
        repository: str,
        commit: str,
        request_key: str,
        request_digest: str,
        task_text: str,
        limits: dict[str, Any],
        parent_task_id: UUID | None = None,
        thread_id: UUID | None = None,
    ) -> dict[str, Any]:
        task_id = uuid4()
        actual_thread = thread_id or uuid4()
        with self.engine.transaction() as conn:
            row = conn.execute(
                "INSERT INTO app.workbench_tasks("
                "task_id,thread_id,parent_task_id,request_key,request_digest,mode,repository_url,"
                "repository,resolved_commit,task,issue_number,execution_mode,coverage,limits,"
                "status,stage,phase,execution_epoch,resume_phase) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,"
                "'queued','suspected','queued',1,'queued') RETURNING *",
                (
                    task_id,
                    actual_thread,
                    parent_task_id,
                    request_key,
                    request_digest,
                    request.mode,
                    request.repository_url,
                    repository,
                    commit,
                    task_text,
                    request.issue_number,
                    request.execution_mode,
                    Jsonb(request.coverage.model_dump(mode="json")),
                    Jsonb(limits),
                ),
            ).fetchone()
            assert row is not None
            conn.execute(
                "INSERT INTO app.workbench_events(task_id,seq,stage,status,summary,payload,"
                "kind,idempotency_key,execution_epoch) "
                "VALUES (%s,1,'task','created','Task accepted','{}','task.created',%s,1) "
                "ON CONFLICT DO NOTHING",
                (task_id, f"task-created:{task_id}"),
            )
        return self._task(row)

    def task_by_request_key(self, request_key: str) -> dict[str, Any] | None:
        with self.engine.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM app.workbench_tasks WHERE request_key=%s", (request_key,)
            ).fetchone()
        return self._task(row) if row is not None else None

    def task(self, task_id: UUID) -> dict[str, Any]:
        with self.engine.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM app.workbench_tasks WHERE task_id=%s", (task_id,)
            ).fetchone()
        if row is None:
            raise DomainError("NOT_FOUND", "Workbench task was not found", 404)
        return self._task(row)

    def tasks(self, limit: int = 30) -> list[dict[str, Any]]:
        with self.engine.transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM app.workbench_tasks ORDER BY created_at DESC LIMIT %s", (limit,)
            ).fetchall()
        return [self._task(row) for row in rows]

    def update(
        self,
        task_id: UUID,
        *,
        status: TaskStatus | None = None,
        stage: str | None = None,
        audit_run_id: UUID | None = None,
        repair_run_id: UUID | None = None,
        observed_coverage: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        assignments = ["revision=revision+1", "updated_at=now()"]
        values: list[Any] = []
        if status is not None:
            assignments.append("status=%s")
            values.append(status)
        if stage is not None:
            assignments.append("stage=%s")
            values.append(stage)
        if audit_run_id is not None:
            assignments.append("audit_run_id=%s")
            values.append(audit_run_id)
        if repair_run_id is not None:
            assignments.append("repair_run_id=%s")
            values.append(repair_run_id)
        if observed_coverage is not None:
            assignments.append("observed_coverage=%s")
            values.append(Jsonb(observed_coverage))
        values.append(task_id)
        with self.engine.transaction() as conn:
            row = conn.execute(
                f"UPDATE app.workbench_tasks SET {', '.join(assignments)} "
                "WHERE task_id=%s RETURNING *",
                tuple(values),
            ).fetchone()
        if row is None:
            raise DomainError("NOT_FOUND", "Workbench task was not found", 404)
        return self._task(row)

    def transition(
        self,
        task_id: UUID,
        *,
        to_phase: TaskPhase,
        to_status: TaskStatus | None = None,
        expected_revision: int | None = None,
        expected_epoch: int | None = None,
        actor: str = "workbench",
        reason: str = "",
        stage: str | None = None,
        bump_epoch: bool = False,
        event: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """CAS- and epoch-fenced phase transition, validated server-side.

        A stale revision or execution epoch is rejected before any write.  The
        database trigger independently enforces the same phase graph and the
        monotonic revision/epoch rules, so an ad-hoc SQL update cannot skip a
        phase either.
        """
        with self.engine.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM app.workbench_tasks WHERE task_id=%s FOR UPDATE", (task_id,)
            ).fetchone()
            if row is None:
                raise DomainError("NOT_FOUND", "Workbench task was not found", 404)
            current = self._task(row)
            if expected_revision is not None and expected_revision != current["revision"]:
                raise DomainError(
                    "TASK_REVISION_CONFLICT",
                    f"Task revision is {current['revision']}, expected {expected_revision}",
                    409,
                )
            if expected_epoch is not None and expected_epoch != current["execution_epoch"]:
                raise DomainError(
                    "FENCED",
                    f"Task epoch is {current['execution_epoch']}, expected {expected_epoch}",
                    409,
                )
            validate_task_phase(current["phase"], to_phase)
            target_status: TaskStatus = to_status or current["status"]
            validate_task_stage(current["status"], target_status)
            resume_phase = current["resume_phase"]
            if to_phase == "paused":
                resume_phase = current["phase"]
            elif current["phase"] == "paused":
                resume_phase = "queued"
            assignments = [
                "phase=%s",
                "status=%s",
                "resume_phase=%s",
                "revision=revision+1",
                "updated_at=now()",
            ]
            values: list[Any] = [to_phase, target_status, resume_phase]
            if stage is not None:
                assignments.append("stage=%s")
                values.append(stage)
            if bump_epoch:
                assignments.append("execution_epoch=execution_epoch+1")
            values.append(task_id)
            updated = conn.execute(
                f"UPDATE app.workbench_tasks SET {', '.join(assignments)} "
                "WHERE task_id=%s RETURNING *",
                tuple(values),
            ).fetchone()
            assert updated is not None
            updated_task = self._task(updated)
            event_row = None
            if event is not None:
                event_row = self._insert_event(
                    conn,
                    task_id,
                    event,
                    idempotency_key=idempotency_key,
                    epoch=updated_task["execution_epoch"],
                )
            conn.execute(
                "INSERT INTO app.workbench_transitions(task_id,revision,from_phase,to_phase,"
                "from_status,to_status,execution_epoch,actor,reason,event_seq) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    task_id,
                    updated_task["revision"],
                    current["phase"],
                    to_phase,
                    current["status"],
                    target_status,
                    updated_task["execution_epoch"],
                    actor,
                    reason,
                    event_row["seq"] if event_row is not None else None,
                ),
            )
        return updated_task

    def bump_epoch(self, task_id: UUID, *, expected_epoch: int | None = None) -> dict[str, Any]:
        """Fence every writer holding an older execution epoch."""
        with self.engine.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM app.workbench_tasks WHERE task_id=%s FOR UPDATE", (task_id,)
            ).fetchone()
            if row is None:
                raise DomainError("NOT_FOUND", "Workbench task was not found", 404)
            current = self._task(row)
            if expected_epoch is not None and expected_epoch != current["execution_epoch"]:
                raise DomainError(
                    "FENCED",
                    f"Task epoch is {current['execution_epoch']}, expected {expected_epoch}",
                    409,
                )
            updated = conn.execute(
                "UPDATE app.workbench_tasks SET execution_epoch=execution_epoch+1,"
                "revision=revision+1,updated_at=now() WHERE task_id=%s RETURNING *",
                (task_id,),
            ).fetchone()
            assert updated is not None
            return self._task(updated)

    def link_run(self, task_id: UUID, kind: str, run_id: UUID) -> None:
        if kind not in {"audit", "repair", "verifier"}:
            raise DomainError("RUN_KIND_INVALID", f"Unknown run kind: {kind}", 400)
        columns = {
            "audit": "audit_run_id",
            "repair": "repair_run_id",
            "verifier": "verifier_run_id",
        }
        column = columns[kind]
        with self.engine.transaction() as conn:
            row = conn.execute(
                f"SELECT {column} AS linked FROM app.workbench_tasks WHERE task_id=%s FOR UPDATE",
                (task_id,),
            ).fetchone()
            if row is None:
                raise DomainError("NOT_FOUND", "Workbench task was not found", 404)
            conn.execute(
                "INSERT INTO app.workbench_run_links(task_id,run_kind,run_id,ordinal) "
                "SELECT %s,%s,%s,COALESCE(MAX(ordinal),0)+1 FROM app.workbench_run_links "
                "WHERE task_id=%s AND run_kind=%s ON CONFLICT DO NOTHING",
                (task_id, kind, run_id, task_id, kind),
            )
            if row["linked"] != run_id:
                conn.execute(
                    f"UPDATE app.workbench_tasks SET {column}=%s,revision=revision+1,"
                    "updated_at=now() WHERE task_id=%s",
                    (run_id, task_id),
                )

    def run_links(self, task_id: UUID) -> list[dict[str, Any]]:
        with self.engine.transaction() as conn:
            rows = conn.execute(
                "SELECT run_kind,run_id,ordinal FROM app.workbench_run_links "
                "WHERE task_id=%s ORDER BY run_kind,ordinal",
                (task_id,),
            ).fetchall()
        return [
            {"kind": row["run_kind"], "run_id": row["run_id"], "ordinal": int(row["ordinal"])}
            for row in rows
        ]

    def transitions(self, task_id: UUID) -> list[dict[str, Any]]:
        with self.engine.transaction() as conn:
            rows = conn.execute(
                "SELECT revision,from_phase,to_phase,from_status,to_status,execution_epoch,"
                "actor,reason,event_seq,created_at FROM app.workbench_transitions "
                "WHERE task_id=%s ORDER BY revision",
                (task_id,),
            ).fetchall()
        return [
            {
                "revision": int(row["revision"]),
                "from_phase": row["from_phase"],
                "to_phase": row["to_phase"],
                "from_status": row["from_status"],
                "to_status": row["to_status"],
                "execution_epoch": int(row["execution_epoch"]),
                "actor": row["actor"],
                "reason": row["reason"],
                "event_seq": row["event_seq"],
                "created_at": row["created_at"].isoformat(),
            }
            for row in rows
        ]

    def append_events(
        self, task_id: UUID, entries: Sequence[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        if not entries:
            return []
        written: list[dict[str, Any]] = []
        with self.engine.transaction() as conn:
            locked = conn.execute(
                "SELECT revision,execution_epoch FROM app.workbench_tasks "
                "WHERE task_id=%s FOR UPDATE",
                (task_id,),
            ).fetchone()
            if locked is None:
                raise DomainError("NOT_FOUND", "Workbench task was not found", 404)
            epoch = int(locked["execution_epoch"])
            for entry in entries:
                row = self._insert_event(
                    conn,
                    task_id,
                    entry,
                    idempotency_key=entry.get("idempotency_key"),
                    epoch=epoch,
                )
                if row is not None:
                    written.append(row)
        return written

    def events(self, task_id: UUID) -> list[dict[str, Any]]:
        with self.engine.transaction() as conn:
            rows = conn.execute(
                "SELECT seq,stage,status,summary,payload,kind,idempotency_key,execution_epoch,"
                "created_at FROM app.workbench_events WHERE task_id=%s ORDER BY seq",
                (task_id,),
            ).fetchall()
        return [self._event(row) for row in rows]

    def upsert_findings(self, task_id: UUID, findings: Iterable[WorkbenchFinding]) -> None:
        with self.engine.transaction() as conn:
            for finding in findings:
                conn.execute(
                    "INSERT INTO app.workbench_findings(task_id,finding_id,stage,risk_score,"
                    "payload) VALUES (%s,%s,%s,%s,%s) ON CONFLICT (task_id,finding_id) "
                    "DO UPDATE SET stage=EXCLUDED.stage,risk_score=EXCLUDED.risk_score,"
                    "payload=EXCLUDED.payload,updated_at=now()",
                    (
                        task_id,
                        finding.finding_id,
                        finding.stage,
                        finding.risk_score,
                        Jsonb(finding.model_dump(mode="json")),
                    ),
                )

    def findings(self, task_id: UUID) -> list[WorkbenchFinding]:
        with self.engine.transaction() as conn:
            rows = conn.execute(
                "SELECT payload FROM app.workbench_findings WHERE task_id=%s "
                "ORDER BY risk_score DESC,finding_id",
                (task_id,),
            ).fetchall()
        return [WorkbenchFinding.model_validate(row["payload"]) for row in rows]

    def finding(self, task_id: UUID, finding_id: str) -> WorkbenchFinding:
        with self.engine.transaction() as conn:
            row = conn.execute(
                "SELECT payload FROM app.workbench_findings WHERE task_id=%s AND finding_id=%s",
                (task_id, finding_id),
            ).fetchone()
        if row is None:
            raise DomainError("NOT_FOUND", "Finding was not found", 404)
        return WorkbenchFinding.model_validate(row["payload"])

    def put_artifact(self, content: bytes) -> str:
        """Persist bytes by hash and mirror the row the ledger points at."""

        content_hash = self.engine.artifacts.put(content)
        with self.engine.transaction() as conn:
            conn.execute(
                "INSERT INTO app.artifacts(content_hash,byte_count) VALUES (%s,%s) "
                "ON CONFLICT DO NOTHING",
                (content_hash, len(content)),
            )
        return content_hash

    def save_handoff(
        self,
        task_id: UUID,
        finding_id: str,
        *,
        handoff_id: str,
        content_hash: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        if handoff_id != content_hash:
            raise DomainError(
                "HANDOFF_ARTIFACT_MISMATCH",
                "Handoff identity is not the artifact content hash",
                409,
            )
        with self.engine.transaction() as conn:
            conn.execute(
                "INSERT INTO app.workbench_handoffs(task_id,finding_id,handoff_id,"
                "content_hash,payload) VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (task_id, finding_id, handoff_id, content_hash, Jsonb(payload)),
            )
            row = conn.execute(
                "SELECT task_id,finding_id,handoff_id,content_hash,payload,created_at "
                "FROM app.workbench_handoffs WHERE task_id=%s AND finding_id=%s",
                (task_id, finding_id),
            ).fetchone()
        if row is None:
            raise DomainError("NOT_FOUND", "Handoff was not found", 404)
        if row["handoff_id"] != handoff_id or row["content_hash"] != content_hash:
            raise DomainError(
                "HANDOFF_CONFLICT",
                "This finding is already bound to a different handoff",
                409,
            )
        return self._handoff(row)

    def handoff(self, task_id: UUID, finding_id: str) -> dict[str, Any] | None:
        with self.engine.transaction() as conn:
            row = conn.execute(
                "SELECT task_id,finding_id,handoff_id,content_hash,payload,created_at "
                "FROM app.workbench_handoffs WHERE task_id=%s AND finding_id=%s",
                (task_id, finding_id),
            ).fetchone()
        return self._handoff(row) if row is not None else None

    def handoffs(self, task_id: UUID) -> list[dict[str, Any]]:
        with self.engine.transaction() as conn:
            rows = conn.execute(
                "SELECT task_id,finding_id,handoff_id,content_hash,payload,created_at "
                "FROM app.workbench_handoffs WHERE task_id=%s ORDER BY created_at,finding_id",
                (task_id,),
            ).fetchall()
        return [self._handoff(row) for row in rows]

    def set_memory(self, task_id: UUID, payload: dict[str, Any]) -> None:
        with self.engine.transaction() as conn:
            conn.execute(
                "INSERT INTO app.workbench_memory(task_id,payload) VALUES (%s,%s) "
                "ON CONFLICT (task_id) DO UPDATE SET payload=EXCLUDED.payload,updated_at=now()",
                (task_id, Jsonb(payload)),
            )

    def memory(self, task_id: UUID) -> dict[str, Any]:
        with self.engine.transaction() as conn:
            row = conn.execute(
                "SELECT payload FROM app.workbench_memory WHERE task_id=%s", (task_id,)
            ).fetchone()
        return dict(row["payload"]) if row is not None else {}

    def coverage_request(self, task: dict[str, Any]) -> CoverageRequest:
        return CoverageRequest.model_validate(task["coverage"])

    @staticmethod
    def _insert_event(
        conn: Any,
        task_id: UUID,
        entry: dict[str, Any],
        *,
        idempotency_key: str | None,
        epoch: int,
    ) -> dict[str, Any] | None:
        current = conn.execute(
            "SELECT COALESCE(MAX(seq),0) AS seq FROM app.workbench_events WHERE task_id=%s",
            (task_id,),
        ).fetchone()
        seq = (int(current["seq"]) if current is not None else 0) + 1
        row = conn.execute(
            "INSERT INTO app.workbench_events(task_id,seq,stage,status,summary,payload,kind,"
            "idempotency_key,execution_epoch) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT DO NOTHING RETURNING seq,stage,status,summary,payload,kind,"
            "idempotency_key,execution_epoch,created_at",
            (
                task_id,
                seq,
                str(entry["stage"]),
                str(entry["status"]),
                str(entry["summary"]),
                Jsonb(entry.get("payload") or {}),
                str(entry.get("kind") or "task.state"),
                idempotency_key,
                epoch,
            ),
        ).fetchone()
        return WorkbenchStore._event(row) if row is not None else None

    @staticmethod
    def _task(row: Any) -> dict[str, Any]:
        return {
            "task_id": row["task_id"],
            "thread_id": row["thread_id"],
            "parent_task_id": row["parent_task_id"],
            "request_digest": row["request_digest"],
            "mode": row["mode"],
            "repository_url": row["repository_url"],
            "repository": row["repository"],
            "commit": row["resolved_commit"],
            "task": row["task"],
            "issue_number": row["issue_number"],
            "execution_mode": row["execution_mode"],
            "coverage": row["coverage"],
            "observed_coverage": row["observed_coverage"],
            "limits": row["limits"],
            "status": row["status"],
            "stage": row["stage"],
            "phase": row["phase"],
            "resume_phase": row["resume_phase"],
            "execution_epoch": int(row["execution_epoch"]),
            "audit_run_id": row["audit_run_id"],
            "repair_run_id": row["repair_run_id"],
            "verifier_run_id": row["verifier_run_id"],
            "revision": row["revision"],
            "created_at": row["created_at"].isoformat(),
            "updated_at": row["updated_at"].isoformat(),
        }

    @staticmethod
    def _handoff(row: Any) -> dict[str, Any]:
        return {
            "task_id": row["task_id"],
            "finding_id": row["finding_id"],
            "handoff_id": row["handoff_id"],
            "content_hash": row["content_hash"],
            "payload": dict(row["payload"]),
            "created_at": row["created_at"].isoformat(),
        }

    @staticmethod
    def _event(row: Any) -> dict[str, Any]:
        return {
            "seq": int(row["seq"]),
            "stage": row["stage"],
            "status": row["status"],
            "summary": row["summary"],
            "payload": dict(row["payload"]),
            "kind": row["kind"],
            "idempotency_key": row["idempotency_key"],
            "execution_epoch": int(row["execution_epoch"]),
            "created_at": row["created_at"].isoformat(),
        }
