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
    TaskStatus,
    WorkbenchFinding,
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
                "status,stage) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,"
                "'queued','suspected') RETURNING *",
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

    def append_events(
        self, task_id: UUID, entries: Sequence[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        if not entries:
            return []
        written: list[dict[str, Any]] = []
        with self.engine.transaction() as conn:
            locked = conn.execute(
                "SELECT task_id FROM app.workbench_tasks WHERE task_id=%s FOR UPDATE", (task_id,)
            ).fetchone()
            if locked is None:
                raise DomainError("NOT_FOUND", "Workbench task was not found", 404)
            current = conn.execute(
                "SELECT COALESCE(MAX(seq),0) AS seq FROM app.workbench_events WHERE task_id=%s",
                (task_id,),
            ).fetchone()
            seq = int(current["seq"]) if current is not None else 0
            for entry in entries:
                seq += 1
                row = conn.execute(
                    "INSERT INTO app.workbench_events(task_id,seq,stage,status,summary,payload) "
                    "VALUES (%s,%s,%s,%s,%s,%s) RETURNING seq,stage,status,summary,payload,"
                    "created_at",
                    (
                        task_id,
                        seq,
                        str(entry["stage"]),
                        str(entry["status"]),
                        str(entry["summary"]),
                        Jsonb(entry.get("payload") or {}),
                    ),
                ).fetchone()
                assert row is not None
                written.append(self._event(row))
        return written

    def events(self, task_id: UUID) -> list[dict[str, Any]]:
        with self.engine.transaction() as conn:
            rows = conn.execute(
                "SELECT seq,stage,status,summary,payload,created_at FROM app.workbench_events "
                "WHERE task_id=%s ORDER BY seq",
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
            "audit_run_id": row["audit_run_id"],
            "repair_run_id": row["repair_run_id"],
            "revision": row["revision"],
            "created_at": row["created_at"].isoformat(),
            "updated_at": row["updated_at"].isoformat(),
        }

    @staticmethod
    def _event(row: Any) -> dict[str, Any]:
        return {
            "seq": int(row["seq"]),
            "stage": row["stage"],
            "status": row["status"],
            "summary": row["summary"],
            "payload": dict(row["payload"]),
            "created_at": row["created_at"].isoformat(),
        }
