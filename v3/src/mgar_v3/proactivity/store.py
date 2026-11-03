from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from mgar_v3.domain import DomainError, TriggerInput, digest
from mgar_v3.proactivity.domain import Activation, Policy, ProactiveEvent

if TYPE_CHECKING:
    from mgar_v3.coordination.engine import Connection, Engine, Row
    from mgar_v3.live import LivePermit


class Proactivity:
    def __init__(self, engine: "Engine"):
        self.engine = engine

    def _policy(self, definition: Policy, permit: "LivePermit|None" = None) -> None:
        if definition.repository.lower() != self.engine.settings.repository.lower() or (
            definition.expires_at <= datetime.now(UTC)
        ):
            raise DomainError("POLICY_SCOPE_DENIED", "Policy repository or expiry is invalid", 403)
        if definition.execution_mode == "live":
            if not permit:
                raise DomainError("LIVE_PROFILE_REQUIRED", "Separate paid permit required", 403)
            permit.validate()
            p = permit.profile
            if (
                p.profile_hash != definition.model_profile_hash
                or p.issue_number != definition.issue_number
                or p.source_ref != definition.source_ref
                or p.question != definition.question
                or p.repository.lower() != definition.repository.lower()
                or p.investigation_version == "r5-repair-v1"
                or definition.max_cost_micros < definition.max_model_calls * p.reservation_micros
            ):
                raise DomainError(
                    "POLICY_SCOPE_DENIED", "Policy differs from its exact paid permit", 403
                )

    def create(self, definition: Policy, actor: str, permit: "LivePermit|None" = None) -> "Row":
        self._actor(actor)
        self._policy(definition, permit)
        with self.engine.transaction() as conn:
            count = conn.execute(
                "SELECT count(*) AS n FROM app.proactive_policies "
                "WHERE project_id=%s AND repository=%s AND mode!='revoked'",
                (self.engine.settings.project_id, self.engine.settings.repository.lower()),
            ).fetchone()
            if (
                count
                and count["n"] >= 32
                and not conn.execute(
                    "SELECT 1 FROM app.proactive_policies WHERE policy_hash=%s",
                    (definition.policy_hash,),
                ).fetchone()
            ):
                raise DomainError("POLICY_LIMIT", "At most 32 policies per project", 429)
            conn.execute(
                "INSERT INTO app.proactive_policies(policy_hash,project_id,repository,definition) "
                "VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (
                    definition.policy_hash,
                    self.engine.settings.project_id,
                    definition.repository.lower(),
                    Jsonb(definition.model_dump(mode="json")),
                ),
            )
            conn.execute(
                "INSERT INTO app.speculation_budgets(policy_hash,run_limit,read_limit,"
                "call_limit,cost_limit_micros) VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (
                    definition.policy_hash,
                    definition.max_runs,
                    definition.max_reads,
                    definition.max_model_calls,
                    definition.max_cost_micros,
                ),
            )
            return self._get(conn, definition.policy_hash)

    def _actor(self, actor: str) -> None:
        if actor.lower() not in {a.lower() for a in self.engine.settings.allowed_actors}:
            raise DomainError("ACTOR_DENIED", "Actor cannot change proactive policy", 403)

    def _get(self, conn: "Connection", key: str) -> "Row":
        row = conn.execute(
            "SELECT * FROM app.proactive_policies WHERE policy_hash=%s "
            "AND project_id=%s AND repository=%s FOR UPDATE",
            (key, self.engine.settings.project_id, self.engine.settings.repository.lower()),
        ).fetchone()
        if not row:
            raise DomainError("NOT_FOUND", "Proactive policy not found", 404)
        return row

    def activate(self, data: Activation, actor: str, permit: "LivePermit|None" = None) -> "Row":
        self._actor(actor)
        with self.engine.transaction() as conn:
            row = self._get(conn, data.policy_hash)
            if row["mode"] == "revoked":
                raise DomainError("POLICY_REVOKED", "Revoked policies cannot reactivate")
            if data.mode == "auto":
                self._policy(Policy.model_validate(row["definition"]), permit)
            changed = conn.execute(
                "UPDATE app.proactive_policies SET mode=%s,activated_by=%s,"
                "activated_at=now() WHERE policy_hash=%s RETURNING *",
                (data.mode, actor, data.policy_hash),
            ).fetchone()
            assert changed is not None
            if data.mode == "revoked":
                for r in conn.execute(
                    "SELECT r.run_id FROM app.speculative_runs s JOIN app.runs r "
                    "USING(run_id) WHERE s.policy_hash=%s AND r.status NOT IN "
                    "('cancelled','failed','succeeded')",
                    (data.policy_hash,),
                ).fetchall():
                    self.engine.cancel(r["run_id"])
            return changed

    def policies(self, *, active_only: bool = False) -> list["Row"]:
        with self.engine.transaction() as conn:
            return conn.execute(
                "SELECT p.*,b.used_runs,b.used_reads,b.used_calls,"
                "b.reserved_micros,b.settled_micros FROM app.proactive_policies p "
                "JOIN app.speculation_budgets b USING(policy_hash) "
                "WHERE project_id=%s AND repository=%s AND (NOT %s OR mode!='revoked') "
                "ORDER BY created_at DESC LIMIT 200",
                (
                    self.engine.settings.project_id,
                    self.engine.settings.repository.lower(),
                    active_only,
                ),
            ).fetchall()

    def observe(self, event: ProactiveEvent, permit: "LivePermit|None" = None) -> list["Row"]:
        if event.repository.lower() != self.engine.settings.repository.lower():
            raise DomainError("PROJECT_DENIED", "Proactive event repository denied", 403)
        payload = event.model_dump(mode="json")
        hashed = digest(payload)
        results = []
        with self.engine.transaction() as conn:
            for row in self.policies(active_only=True):
                policy = Policy.model_validate(row["definition"])
                if (
                    row["mode"] == "auto"
                    and policy.execution_mode == "live"
                    and (permit is None or permit.profile.profile_hash != policy.model_profile_hash)
                ):
                    continue
                if (
                    row["mode"] == "revoked"
                    or policy.issue_number != event.issue_number
                    or policy.expires_at <= datetime.now(UTC)
                    or event.kind not in policy.kinds
                    or event.actor.lower() not in {a.lower() for a in policy.allowed_actors}
                    or not set(policy.required_labels) <= set(event.labels)
                    or event.source_ref != policy.source_ref
                ):
                    continue
                previous = conn.execute(
                    "SELECT * FROM app.proactive_observations "
                    "WHERE policy_hash=%s AND external_event_id=%s",
                    (policy.policy_hash, event.external_event_id),
                ).fetchone()
                if previous:
                    if previous["payload_hash"] != hashed:
                        raise DomainError("EVENT_ID_REUSED", "Proactive event content changed")
                    results.append(previous)
                    continue
                run_id = None
                decision = "observed"
                if row["mode"] == "auto":
                    self._policy(policy, permit)
                    claimed = conn.execute(
                        "SELECT * FROM app.proactive_event_claims WHERE project_id=%s "
                        "AND repository=%s AND external_event_id=%s",
                        (
                            self.engine.settings.project_id,
                            event.repository.lower(),
                            event.external_event_id,
                        ),
                    ).fetchone()
                    if claimed:
                        if claimed["payload_hash"] != hashed:
                            raise DomainError("EVENT_ID_REUSED", "Claimed event content changed")
                        decision = "already_started"
                    elif self._charge(conn, policy.policy_hash, "used_runs", "run_limit"):
                        run_id = self._start(conn, event, policy, permit)
                        decision = "started"
                        conn.execute(
                            "INSERT INTO app.proactive_event_claims "
                            "(project_id,repository,external_event_id,payload_hash,run_id) "
                            "VALUES (%s,%s,%s,%s,%s)",
                            (
                                self.engine.settings.project_id,
                                event.repository.lower(),
                                event.external_event_id,
                                hashed,
                                run_id,
                            ),
                        )
                    else:
                        decision = "budget_exhausted"
                stored = conn.execute(
                    "INSERT INTO app.proactive_observations "
                    "(observation_id,policy_hash,external_event_id,payload_hash,"
                    "payload,decision,run_id) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING *",
                    (
                        uuid4(),
                        policy.policy_hash,
                        event.external_event_id,
                        hashed,
                        Jsonb(payload),
                        decision,
                        run_id,
                    ),
                ).fetchone()
                assert stored is not None
                results.append(stored)
        return results

    @staticmethod
    def _charge(conn: "Connection", key: str, used: str, limit: str) -> bool:
        assert (used, limit) in {("used_runs", "run_limit"), ("used_reads", "read_limit")}
        return (
            conn.execute(
                f"UPDATE app.speculation_budgets SET {used}={used}+1 "
                f"WHERE policy_hash=%s AND {used}<{limit} RETURNING policy_hash",
                (key,),
            ).fetchone()
            is not None
        )

    def _start(
        self, conn: "Connection", event: ProactiveEvent, policy: Policy, permit: "LivePermit|None"
    ) -> UUID:
        thread = conn.execute(
            "INSERT INTO app.threads(thread_id,project_id,repository,external_key,"
            "revision,latest_seq) VALUES (%s,%s,%s,%s,1,1) RETURNING *",
            (
                uuid4(),
                self.engine.settings.project_id,
                event.repository.lower(),
                f"speculation-{policy.policy_hash}-{digest(event.external_event_id)}#issue:{event.issue_number}",
            ),
        ).fetchone()
        assert thread is not None
        initial = TriggerInput(
            source="github",
            external_event_id="speculation:" + str(uuid4()),
            project_id=self.engine.settings.project_id,
            repository=event.repository,
            issue_number=event.issue_number,
            actor=self.engine.settings.local_actor,
            body="/mgar investigate " + policy.question,
            execution_mode=policy.execution_mode,
        )
        ticket, run = self.engine._create_ticket(
            conn,
            initial,
            thread,
            permit,
            question=policy.question,
            basis="proactive_policy:" + policy.policy_hash,
        )
        conn.execute(
            "UPDATE app.runs SET graph_version=%s WHERE run_id=%s", (policy.graph_version, run)
        )
        conn.execute(
            "UPDATE app.mandates SET scope=scope||%s WHERE mandate_id="
            "(SELECT mandate_id FROM app.tickets WHERE ticket_id=%s)",
            (
                Jsonb({"read_only": True, "actions": [], "speculation_policy": policy.policy_hash}),
                ticket,
            ),
        )
        identifier = uuid4()
        conn.execute(
            "INSERT INTO app.trigger_events(event_id,source,external_event_id,thread_id,"
            "thread_seq,payload_hash,payload,ticket_id,run_id,route,route_snapshot) "
            "VALUES (%s,'github',%s,%s,1,%s,%s,%s,%s,'created',%s)",
            (
                identifier,
                initial.external_event_id,
                thread["thread_id"],
                digest(initial.model_dump(mode="json")),
                Jsonb(initial.model_dump(mode="json")),
                ticket,
                run,
                Jsonb([{"run_id": str(run), "role": "primary"}]),
            ),
        )
        conn.execute(
            "INSERT INTO app.mailbox(run_id,event_id,seq) VALUES (%s,%s,1)", (run, identifier)
        )
        conn.execute(
            "INSERT INTO app.speculative_runs(run_id,policy_hash) VALUES (%s,%s)",
            (run, policy.policy_hash),
        )
        self.engine._event(
            conn, run, "speculative_readonly_started", policy_hash=policy.policy_hash
        )
        return run

    def charge_read(self, conn: "Connection", run_id: UUID) -> None:
        row = conn.execute(
            "SELECT policy_hash FROM app.speculative_runs WHERE run_id=%s", (run_id,)
        ).fetchone()
        if row and not self._charge(conn, row["policy_hash"], "used_reads", "read_limit"):
            raise DomainError("SPECULATION_BUDGET_EXHAUSTED", "Separate read budget exhausted")

    def reserve(self, conn: "Connection", run_id: UUID, amount: int) -> None:
        row = conn.execute(
            "SELECT s.policy_hash FROM app.speculative_runs s WHERE run_id=%s", (run_id,)
        ).fetchone()
        if not row:
            return
        if conn.execute(
            "SELECT 1 FROM app.usage_calls c JOIN app.speculative_runs s USING(run_id) "
            "WHERE s.policy_hash=%s AND c.status='unknown'",
            (row["policy_hash"],),
        ).fetchone():
            raise DomainError("USAGE_UNKNOWN", "Speculation usage must reconcile before more calls")
        changed = conn.execute(
            "UPDATE app.speculation_budgets SET used_calls=used_calls+1,"
            "reserved_micros=reserved_micros+%s WHERE policy_hash=%s AND used_calls<call_limit "
            "AND reserved_micros+settled_micros+%s<=cost_limit_micros RETURNING policy_hash",
            (amount, row["policy_hash"], amount),
        ).fetchone()
        if not changed:
            raise DomainError("SPECULATION_BUDGET_EXHAUSTED", "Separate model budget exhausted")

    def settle(self, conn: "Connection", run_id: UUID, reserved: int, cost: int) -> None:
        conn.execute(
            "UPDATE app.speculation_budgets b SET reserved_micros=reserved_micros-%s,"
            "settled_micros=settled_micros+%s FROM app.speculative_runs s "
            "WHERE s.run_id=%s AND b.policy_hash=s.policy_hash",
            (reserved, cost, run_id),
        )
