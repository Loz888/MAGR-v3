import json
from typing import Any, cast
from uuid import UUID

from psycopg.types.json import Jsonb

from mgar_v3.coordination.engine import COMMAND, Engine, Row
from mgar_v3.coordination_eval.budget import CoordinationBudget
from mgar_v3.coordination_eval.domain import Event, Route
from mgar_v3.domain import DomainError, TriggerInput, digest
from mgar_v3.live import LivePermit
from mgar_v3.settings import ROOT

DATA = ROOT / "eval/coordination-v1"


class Journal:
    """Every experiment mutation uses the original Engine's connection and lock."""

    def __init__(self, engine: Engine, trial: dict[str, Any], manifest_hash: str):
        self.engine, self.trial = engine, trial
        self.id = str(trial["id"])
        ddl = (DATA / "schema.sql").read_text()
        with engine.transaction() as conn:
            conn.execute(ddl)
            conn.execute(
                "INSERT INTO coordination_eval.trials(trial_id,manifest_hash,arm) "
                "VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                (self.id, manifest_hash, trial["arm"]),
            )
            row = conn.execute(
                "SELECT * FROM coordination_eval.trials WHERE trial_id=%s", (self.id,)
            ).fetchone()
            if not row or row["manifest_hash"] != manifest_hash or row["arm"] != trial["arm"]:
                raise DomainError("COORD_TRIAL_CHANGED", "Trial identity cannot be replaced")
        engine.benchmarks = CoordinationBudget(engine)

    def audit(self, kind: str, **details: Any) -> None:
        with self.engine.transaction() as conn:
            conn.execute(
                "INSERT INTO coordination_eval.audit(trial_id,kind,details) VALUES (%s,%s,%s)",
                (self.id, kind, Jsonb(details)),
            )

    def row(self, key: str) -> Row | None:
        with self.engine.transaction() as conn:
            return conn.execute(
                "SELECT * FROM coordination_eval.events WHERE trial_id=%s AND event_key=%s",
                (self.id, key),
            ).fetchone()

    def accept(self, event: Event, position: int) -> tuple[Row, bool]:
        # Phase is delivery scheduling, not part of the immutable logical event payload.
        payload = event.model_dump(exclude={"phase"}, mode="json")
        with self.engine.transaction() as conn:
            prior_delivery = conn.execute(
                "SELECT 1 FROM coordination_eval.audit WHERE trial_id=%s "
                "AND kind='physical_delivery' AND details->>'position'=%s",
                (self.id, str(position)),
            ).fetchone()
            old = self.row(event.key)
            if old and old["payload_hash"] != digest(payload):
                raise DomainError("EVENT_ID_REUSED", "Experiment event payload changed")
            duplicate = bool(old and not prior_delivery)
            if not prior_delivery:
                self.audit("physical_delivery", position=position, key=event.key)
                if duplicate:
                    self.audit("duplicate", key=event.key)
            conn.execute(
                "INSERT INTO coordination_eval.events(trial_id,event_key,payload,payload_hash) "
                "VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (self.id, event.key, Jsonb(payload), digest(payload)),
            )
            row = self.row(event.key)
            assert row is not None
            return row, duplicate

    def trigger(self, event: Event) -> TriggerInput:
        s = self.engine.settings
        return TriggerInput(
            source=event.source,
            external_event_id=f"coord:{self.id}:{event.key}",
            project_id=s.project_id,
            repository=s.repository,
            issue_number=int(self.trial["issue_base"]) + event.issue,
            actor=s.local_actor if event.actor == "owner" else "coord-outsider",
            body=event.body,
            execution_mode="live",
        )

    def deny_actor(self, event: Event) -> None:
        try:
            self.engine.ingest(self.trigger(event))
        except DomainError as exc:
            if exc.code != "ACTOR_DENIED":
                raise
            self.finish(event.key, "denied", reason=exc.code)
        else:
            raise AssertionError("Unauthorized event was accepted by the real Engine")

    def targets(self) -> list[Row]:
        with self.engine.transaction() as conn:
            rows = conn.execute(
                "SELECT r.run_id,r.execution_epoch,r.status,h.revision,h.external_key,"
                "h.thread_id,t.ticket_id FROM app.subscriptions s JOIN app.runs r "
                "USING(run_id) JOIN app.tickets t USING(ticket_id) JOIN app.threads h "
                "ON h.thread_id=t.thread_id WHERE s.active AND s.role='primary' "
                "AND h.project_id=%s AND h.repository=%s AND t.status='open' "
                "ORDER BY h.external_key",
                (self.engine.settings.project_id, self.engine.settings.repository.lower()),
            ).fetchall()
        for row in rows:
            row["issue"] = int(row["external_key"].rsplit(":", 1)[-1]) - int(
                self.trial["issue_base"]
            )
        return rows

    def target(self, event: Event) -> Row | None:
        suffix = f"#issue:{int(self.trial['issue_base']) + event.issue}"
        return next((r for r in self.targets() if r["external_key"].endswith(suffix)), None)

    def finish(
        self,
        key: str,
        action: str,
        *,
        run_id: UUID | None = None,
        event_id: UUID | None = None,
        reason: str | None = None,
    ) -> None:
        with self.engine.transaction() as conn:
            conn.execute(
                "UPDATE coordination_eval.events SET action=%s,run_id=%s,event_id=%s,"
                "reason=%s,completed_at=clock_timestamp() WHERE trial_id=%s AND event_key=%s "
                "AND action IS NULL",
                (action, run_id, event_id, reason, self.id, key),
            )

    def readonly(self, run_id: UUID, permit: LivePermit, manifest_hash: str, case_id: str) -> None:
        permit.validate()
        with self.engine.transaction() as conn:
            run = self.engine._run(conn, run_id)
            changed = conn.execute(
                "UPDATE app.mandates SET scope=scope||%s WHERE mandate_id=%s "
                "AND scope->>'profile_hash'=%s RETURNING mandate_id",
                (Jsonb({"read_only": True}), run["mandate_id"], permit.profile.profile_hash),
            ).fetchone()
            if not changed:
                raise DomainError("COORD_SCOPE_DENIED", "Experiment mandate differs from profile")
            self.engine.benchmarks.bind(run_id, manifest_hash, case_id, self.trial["arm"])

    def apply(
        self,
        event: Event,
        route: Route,
        permit: LivePermit | None,
        manifest_hash: str,
        case_id: str | None,
        expected: dict[str, Any] | None = None,
    ) -> UUID | None:
        current = self.target(event)
        if event.actor != "owner" or route.event_key != event.key:
            raise DomainError("COORD_SCOPE_DENIED", "Route differs from its accepted event")
        if route.action == "deliver":
            if not current or current["run_id"] != route.target_run_id:
                raise DomainError("COORD_TARGET_DENIED", "Run is not this issue's primary")
            if expected and any(
                str(current[k]) != str(expected[k]) for k in ("run_id", "execution_epoch")
            ):
                raise DomainError("COORD_TARGET_STALE", "Primary epoch changed during routing")
        elif route.target_run_id is not None:
            raise DomainError("COORD_TARGET_DENIED", "Only delivery may target a Run")
        if route.action == "start":
            if current or not event.body.startswith(COMMAND) or not permit or not case_id:
                raise DomainError(
                    "COORD_INTENT_DENIED", "New work needs an explicit scoped request"
                )
        if route.action in {"ignore", "clarify"}:
            self.finish(event.key, route.action)
            return None
        assert permit is not None
        receipt = self.engine.ingest(self.trigger(event), permit)
        assert receipt.run_id is not None
        if route.action == "start":
            assert case_id is not None
            self.readonly(receipt.run_id, permit, manifest_hash, case_id)
        self.finish(event.key, route.action, run_id=receipt.run_id, event_id=receipt.event_id)
        return receipt.run_id

    def export(self) -> dict[str, Any]:
        with self.engine.transaction() as conn:
            data = {
                "trial": self.trial,
                "events": conn.execute(
                    "SELECT * FROM coordination_eval.events WHERE trial_id=%s "
                    "ORDER BY received_at,event_key",
                    (self.id,),
                ).fetchall(),
                "batches": conn.execute(
                    "SELECT * FROM coordination_eval.batches WHERE trial_id=%s ORDER BY ordinal",
                    (self.id,),
                ).fetchall(),
                "audit": conn.execute(
                    "SELECT * FROM coordination_eval.audit WHERE trial_id=%s ORDER BY id",
                    (self.id,),
                ).fetchall(),
                "runs": conn.execute(
                    "SELECT r.* FROM app.runs r JOIN app.tickets t USING(ticket_id) "
                    "JOIN app.threads h USING(thread_id) WHERE h.project_id=%s ORDER BY r.run_id",
                    (self.engine.settings.project_id,),
                ).fetchall(),
                "calls": conn.execute(
                    "SELECT c.* FROM app.usage_calls c JOIN app.runs r USING(run_id) "
                    "JOIN app.tickets t USING(ticket_id) JOIN app.threads h USING(thread_id) "
                    "WHERE h.project_id=%s ORDER BY c.call_id",
                    (self.engine.settings.project_id,),
                ).fetchall(),
                "mailbox": conn.execute(
                    "SELECT m.*,e.kind,e.external_event_id FROM app.mailbox m "
                    "JOIN app.trigger_events e USING(event_id) JOIN app.runs r ON "
                    "r.run_id=m.run_id JOIN app.tickets t ON t.ticket_id=r.ticket_id "
                    "JOIN app.threads h "
                    "ON h.thread_id=t.thread_id WHERE h.project_id=%s ORDER BY m.run_id,m.seq",
                    (self.engine.settings.project_id,),
                ).fetchall(),
                "run_events": conn.execute(
                    "SELECT e.* FROM app.run_events e JOIN app.runs r USING(run_id) "
                    "JOIN app.tickets t USING(ticket_id) JOIN app.threads h USING(thread_id) "
                    "WHERE h.project_id=%s ORDER BY e.event_id",
                    (self.engine.settings.project_id,),
                ).fetchall(),
                "evidence": conn.execute(
                    "SELECT e.* FROM app.evidence e JOIN app.runs r USING(run_id) "
                    "JOIN app.tickets t USING(ticket_id) JOIN app.threads h USING(thread_id) "
                    "WHERE h.project_id=%s",
                    (self.engine.settings.project_id,),
                ).fetchall(),
                "forbidden_effects": cast(
                    Row,
                    conn.execute(
                        "SELECT count(*) AS n FROM app.actions a JOIN app.runs r USING(run_id) "
                        "JOIN app.tickets t USING(ticket_id) JOIN app.threads h USING(thread_id) "
                        "WHERE h.project_id=%s",
                        (self.engine.settings.project_id,),
                    ).fetchone(),
                )["n"],
            }
        data["artifacts"] = {
            row["report_hash"]: self.engine.artifacts.read(row["report_hash"]).decode()
            for row in data["runs"]
            if row["report_hash"]
        }
        data["source_artifacts"] = {
            row["content_hash"]: self.engine.artifacts.read(row["content_hash"]).decode()
            for row in data["evidence"]
        }
        return cast(dict[str, Any], json.loads(json.dumps(data, default=str)))
