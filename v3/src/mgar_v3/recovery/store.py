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
                "coalesce(e.event_id,0) AS progress_cursor,u.action_id AS unknown_action_id,"
                "c.call_id AS unknown_call_id,"
                "b.expires_at AS heartbeat_expires_at,"
                "(SELECT count(*) FROM app.mailbox WHERE run_id=r.run_id "
                "AND consumed_at IS NULL) AS mailbox_pending "
                "FROM app.runs r JOIN app.tickets t USING(ticket_id) "
                "JOIN app.threads h USING(thread_id) LEFT JOIN LATERAL "
                "(SELECT event_id,created_at FROM app.run_events WHERE run_id=r.run_id "
                "ORDER BY event_id DESC LIMIT 1) e ON true LEFT JOIN LATERAL "
                "(SELECT action_id FROM app.actions WHERE run_id=r.run_id "
                "AND status='outcome_unknown' ORDER BY created_at,action_id LIMIT 1) u ON true "
                "LEFT JOIN app.worker_heartbeats b ON b.run_id=r.run_id "
                "AND b.execution_epoch=r.execution_epoch "
                "LEFT JOIN LATERAL (SELECT call_id FROM app.usage_calls WHERE run_id=r.run_id "
                "AND status='unknown' ORDER BY call_id LIMIT 1) c ON true "
                "WHERE h.project_id=%s AND h.repository=%s "
                "AND (%s::uuid IS NULL OR r.run_id=%s) "
                "AND (r.status NOT IN ('cancelled','failed','succeeded') "
                "OR u.action_id IS NOT NULL OR c.call_id IS NOT NULL) "
                "ORDER BY r.run_id LIMIT 257",
                (
                    self.engine.settings.project_id,
                    self.engine.settings.repository.lower(),
                    run_id,
                    run_id,
                ),
            ).fetchall()
            assert now is not None
            scope = (self.engine.settings.project_id, self.engine.settings.repository.lower())
            queues = conn.execute(
                "SELECT router.n AS router_backlog,router.cursor AS router_cursor,"
                "outgoing.n AS outbox_backlog,outgoing.cursor AS outbox_cursor FROM "
                "(SELECT count(*) AS n,coalesce(max(i.event_id::text),'') AS cursor "
                "FROM app.router_inbox i "
                "JOIN app.trigger_events e USING(event_id) JOIN app.threads h USING(thread_id) "
                "WHERE h.project_id=%s AND h.repository=%s) router CROSS JOIN "
                "(SELECT count(*) AS n,max(o.created_at) AS cursor "
                "FROM app.outbox o JOIN app.actions a USING(action_id) "
                "JOIN app.runs r USING(run_id) JOIN app.tickets t USING(ticket_id) "
                "JOIN app.threads h USING(thread_id) WHERE o.status IN ('ready','dispatching') "
                "AND h.project_id=%s AND h.repository=%s) outgoing",
                scope + scope,
            ).fetchone()
            health = conn.execute(
                "SELECT dependency,state,generation FROM app.dependency_health "
                "WHERE project_id=%s AND repository=%s AND checked_at>now()-interval '60 seconds'",
                scope,
            ).fetchall()
            router_usage = conn.execute(
                "SELECT count(*) AS n,coalesce(max(c.call_id::text),'') AS cursor "
                "FROM app.router_calls c JOIN app.router_batches b USING(batch_id) "
                "WHERE c.status='unknown' AND b.project_id=%s AND EXISTS "
                "(SELECT 1 FROM jsonb_array_elements(b.snapshot->'threads') item "
                "JOIN app.threads h ON h.thread_id=(item->>'thread_id')::uuid "
                "WHERE h.repository=%s)",
                scope,
            ).fetchone()
            system = {
                **({k: queues[k] for k in ("router_backlog", "outbox_backlog")} if queues else {}),
                **{"dependency:" + h["dependency"]: h["state"] for h in health},
                "router_usage_unknown": router_usage["n"] if router_usage else 0,
            }
            return Snapshot(
                sampled_at=now["sampled_at"],
                items=[Observation.model_validate(r) for r in rows[:256]],
                truncated=len(rows) > 256,
                system=system,
                system_cursors={
                    "router_usage_unknown": router_usage["cursor"] if router_usage else "",
                    **{"dependency:" + h["dependency"]: str(h["generation"]) for h in health},
                    **(
                        {
                            "router_backlog": str(queues["router_cursor"]),
                            "outbox_backlog": str(queues["outbox_cursor"]),
                        }
                        if queues
                        else {}
                    ),
                },
                project_id=scope[0],
                repository=scope[1],
            )

    def _scope(self, conn: Any, run_id: UUID) -> "Row":
        run = self.engine._run(conn, run_id)
        if run["repository"] != self.engine.settings.repository.lower():
            raise DomainError("PROJECT_DENIED", "Recovery is outside this repository", 403)
        return run

    def submit(self, data: RecoveryInput) -> "Row":
        with self.engine.transaction() as conn:
            run = self._scope(conn, data.run_id) if data.run_id else None
            existing = conn.execute(
                "SELECT * FROM app.recovery_requests WHERE observation_key=%s "
                "AND project_id=%s AND repository=%s",
                (
                    data.observation_key,
                    self.engine.settings.project_id,
                    self.engine.settings.repository.lower(),
                ),
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
            if (run and run["execution_epoch"] != data.execution_epoch) or data not in detect(
                snapshot
            ):
                raise DomainError("STALE_OBSERVATION", "Current facts do not support this request")
            row = conn.execute(
                "INSERT INTO app.recovery_requests(request_id,run_id,execution_epoch,kind,"
                "observation_key,evidence,project_id,repository) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
                (
                    uuid4(),
                    data.run_id,
                    data.execution_epoch,
                    data.kind,
                    data.observation_key,
                    Jsonb(snapshot.model_dump(mode="json")),
                    self.engine.settings.project_id,
                    self.engine.settings.repository.lower(),
                ),
            ).fetchone()
            assert row is not None
            return {**row, "created": True}

    def list(self) -> list["Row"]:
        with self.engine.transaction() as conn:
            return conn.execute(
                "SELECT q.* FROM app.recovery_requests q "
                "WHERE q.project_id=%s AND q.repository=%s ORDER BY q.created_at DESC LIMIT 256",
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
            if reference["run_id"]:
                self._scope(conn, reference["run_id"])
            row = conn.execute(
                "SELECT * FROM app.recovery_requests WHERE request_id=%s FOR UPDATE", (request_id,)
            ).fetchone()
            assert row is not None
            if row["project_id"] != self.engine.settings.project_id or (
                row["repository"] != self.engine.settings.repository.lower()
            ):
                raise DomainError("PROJECT_DENIED", "Recovery request is outside scope", 403)
            choice = decision.decision
            if row["status"] != "proposed":
                if row["decision"] == choice or row["status"] == "stale":
                    return row
                raise DomainError("ALREADY_DECIDED", "Recovery already has a different decision")
            if (row["kind"] == "outcome_unknown" or row["run_id"] is None) and choice not in {
                "wait",
                "escalate",
            }:
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

    def heartbeat(self, run_id: UUID, epoch: int) -> None:
        with self.engine.transaction() as conn:
            run = self.engine._run(conn, run_id, epoch)
            if run["status"] != "running":
                raise DomainError("FENCED", "Worker is no longer running")
            conn.execute(
                "INSERT INTO app.worker_heartbeats(run_id,execution_epoch) VALUES (%s,%s) "
                "ON CONFLICT(run_id,execution_epoch) DO UPDATE SET last_beat=now(),"
                "expires_at=now()+interval '30 seconds',sequence=app.worker_heartbeats.sequence+1",
                (run_id, epoch),
            )
