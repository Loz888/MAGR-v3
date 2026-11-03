from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from mgar_v3.domain import DomainError
from mgar_v3.recovery.domain import Decision, Observation, RecoveryInput, Snapshot, detect

if TYPE_CHECKING:
    from mgar_v3.coordination.engine import Engine, Row


class Recovery:
    def __init__(self, engine: "Engine"):
        self.engine = engine

    def snapshot(self, run_id: UUID | None = None) -> Snapshot:
        with self.engine.transaction() as conn:
            now = conn.execute("SELECT now() AS sampled_at").fetchone()
            rows = conn.execute(
                "SELECT r.run_id,r.execution_epoch,r.status,"
                "coalesce(e.created_at,r.created_at) AS progress_at,"
                "coalesce(e.event_id,0) AS progress_cursor,u.action_id AS unknown_action_id "
                "FROM app.runs r JOIN app.tickets t USING(ticket_id) "
                "JOIN app.threads h USING(thread_id) LEFT JOIN LATERAL "
                "(SELECT event_id,created_at FROM app.run_events WHERE run_id=r.run_id "
                "ORDER BY event_id DESC LIMIT 1) e ON true LEFT JOIN LATERAL "
                "(SELECT action_id FROM app.actions WHERE run_id=r.run_id "
                "AND status='outcome_unknown' ORDER BY created_at,action_id LIMIT 1) u ON true "
                "WHERE h.project_id=%s AND h.repository=%s "
                "AND (%s::uuid IS NULL OR r.run_id=%s) "
                "AND (r.status NOT IN ('cancelled','failed','succeeded') "
                "OR u.action_id IS NOT NULL) "
                "ORDER BY r.run_id LIMIT 257",
                (
                    self.engine.settings.project_id,
                    self.engine.settings.repository.lower(),
                    run_id,
                    run_id,
                ),
            ).fetchall()
            assert now is not None
            return Snapshot(
                sampled_at=now["sampled_at"],
                items=[Observation.model_validate(r) for r in rows[:256]],
                truncated=len(rows) > 256,
            )

    def _scope(self, conn: Any, run_id: UUID) -> "Row":
        run = self.engine._run(conn, run_id)
        if run["repository"] != self.engine.settings.repository.lower():
            raise DomainError("PROJECT_DENIED", "Recovery is outside this repository", 403)
        return run

    def submit(self, data: RecoveryInput) -> "Row":
        with self.engine.transaction() as conn:
            run = self._scope(conn, data.run_id)
            existing = conn.execute(
                "SELECT * FROM app.recovery_requests WHERE observation_key=%s",
                (data.observation_key,),
            ).fetchone()
            if existing:
                if (existing["run_id"], existing["execution_epoch"], existing["kind"]) != (
                    data.run_id,
                    data.execution_epoch,
                    data.kind,
                ):
                    raise DomainError("OBSERVATION_CHANGED", "Recovery key belongs to other facts")
                return {**existing, "created": False}
            snapshot = self.snapshot(data.run_id)
            if run["execution_epoch"] != data.execution_epoch or data not in detect(snapshot):
                raise DomainError("STALE_OBSERVATION", "Current facts do not support this request")
            row = conn.execute(
                "INSERT INTO app.recovery_requests(request_id,run_id,execution_epoch,kind,"
                "observation_key,evidence) VALUES (%s,%s,%s,%s,%s,%s) RETURNING *",
                (
                    uuid4(),
                    data.run_id,
                    data.execution_epoch,
                    data.kind,
                    data.observation_key,
                    Jsonb(snapshot.model_dump(mode="json")),
                ),
            ).fetchone()
            assert row is not None
            return {**row, "created": True}

    def list(self) -> list["Row"]:
        with self.engine.transaction() as conn:
            return conn.execute(
                "SELECT q.* FROM app.recovery_requests q JOIN app.runs r USING(run_id) "
                "JOIN app.tickets t USING(ticket_id) JOIN app.threads h USING(thread_id) "
                "WHERE h.project_id=%s AND h.repository=%s ORDER BY q.created_at DESC LIMIT 256",
                (self.engine.settings.project_id, self.engine.settings.repository.lower()),
            ).fetchall()

    def decide(self, request_id: UUID, decision: Decision, actor: str) -> "Row":
        if actor.lower() not in {a.lower() for a in self.engine.settings.allowed_actors}:
            raise DomainError("ACTOR_DENIED", "Actor cannot decide recovery", 403)
        with self.engine.transaction() as conn:
            reference = conn.execute(
                "SELECT run_id FROM app.recovery_requests WHERE request_id=%s", (request_id,)
            ).fetchone()
            if not reference:
                raise DomainError("NOT_FOUND", "Recovery request was not found", 404)
            self._scope(conn, reference["run_id"])
            row = conn.execute(
                "SELECT * FROM app.recovery_requests WHERE request_id=%s FOR UPDATE", (request_id,)
            ).fetchone()
            assert row is not None
            choice = decision.decision
            if row["status"] != "proposed":
                if row["decision"] == choice or row["status"] == "stale":
                    return row
                raise DomainError("ALREADY_DECIDED", "Recovery already has a different decision")
            if row["kind"] == "outcome_unknown" and choice not in {"wait", "escalate"}:
                raise DomainError(
                    "UNKNOWN_REQUIRES_HUMAN", "Unknown effects only permit wait/escalate"
                )
            current = detect(self.snapshot(row["run_id"]))
            if not any(s.observation_key == row["observation_key"] for s in current):
                status = "stale"
            else:
                if choice == "pause":
                    self.engine.pause(row["run_id"])
                elif choice == "cancel":
                    self.engine.cancel(row["run_id"])
                status = {"wait": "waiting", "escalate": "needs_human"}.get(choice, "resolved")
            changed = conn.execute(
                "UPDATE app.recovery_requests SET status=%s,decision=%s,decided_by=%s,"
                "decided_at=now() WHERE request_id=%s RETURNING *",
                (status, choice, actor, request_id),
            ).fetchone()
            assert changed is not None
            return changed
