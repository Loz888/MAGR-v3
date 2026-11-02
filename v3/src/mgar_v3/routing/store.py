"Engine-owned routing transactions. This service never opens a second connection."

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from mgar_v3.domain import DomainError, TriggerInput, Verification, digest
from mgar_v3.routing.domain import Proposals, RouterDecision

if TYPE_CHECKING:
    from mgar_v3.coordination.engine import Connection, Engine, Row
    from mgar_v3.live import LivePermit

TEMPLATES = {
    "clarification": "请补充要调查的问题、期望结果和可核对的来源，再继续本工单。",
    "queued": "已收到请求，正在等待处理。",
    "unavailable": "当前路由服务不可用，请稍后重试；已接收的输入仍保留。",
}
CONTROL = {"pause_ticket": "pause", "resume_ticket": "resume", "cancel_ticket": "cancel"}


def plain(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def addressed(body: str) -> bool:
    return body.startswith(("@mgar ", "/mgar investigate "))


def question_for(events: list[dict[str, Any]]) -> str:
    parts = []
    for event in events:
        body = event["payload"]["body"]
        parts.append(body.removeprefix("@mgar ").removeprefix("/mgar investigate ").strip())
    return "\n\n".join(parts)


class Routing:
    def __init__(self, engine: "Engine"):
        self.engine = engine

    def _audit(
        self,
        conn: "Connection",
        batch_id: UUID | None,
        kind: str,
        reason: str | None = None,
        **details: Any,
    ) -> None:
        conn.execute(
            "INSERT INTO app.router_audit(batch_id,kind,reason_code,details) VALUES (%s,%s,%s,%s)",
            (batch_id, kind, reason, Jsonb(plain(details))),
        )

    def _rows(
        self,
        conn: "Connection",
        actor: str,
        mode: str | None = None,
        ticket_id: UUID | None = None,
        limit: int = 20,
    ) -> list["Row"]:
        s = self.engine.settings
        return conn.execute(
            "SELECT t.ticket_id,t.thread_id,t.status AS ticket_status,t.outcome,t.created_at,"
            "m.mandate_id,m.actor,m.policy_version,h.repository,h.external_key,h.revision,"
            "r.run_id,r.status AS stage,r.execution_epoch,r.execution_mode,r.graph_version,"
            "left(r.question,256) AS question FROM app.tickets t JOIN app.mandates m "
            "USING(mandate_id) JOIN app.threads h USING(thread_id) JOIN LATERAL (SELECT * FROM "
            "app.runs x WHERE x.ticket_id=t.ticket_id ORDER BY attempt_no DESC LIMIT 1) r ON "
            "true WHERE h.project_id=%s AND h.repository=%s AND m.project_id=%s AND m.actor=%s "
            "AND m.scope->>'repository'=h.repository "
            "AND m.scope->>'issue_number'=split_part(h.external_key,':',2) "
            "AND m.policy_version=%s AND (%s::text IS NULL OR r.execution_mode=%s) AND "
            "(%s::uuid IS NULL OR t.ticket_id=%s) ORDER BY t.created_at DESC,t.ticket_id LIMIT "
            "%s",
            (
                s.project_id,
                s.repository.lower(),
                s.project_id,
                actor,
                s.policy_version,
                mode,
                mode,
                ticket_id,
                ticket_id,
                limit,
            ),
        ).fetchall()

    def owned_ticket(self, ticket_id: UUID, *, actor: str | None = None) -> "Row":
        actor = actor or self.engine.settings.local_actor.lower()
        if actor not in {a.lower() for a in self.engine.settings.allowed_actors}:
            raise DomainError("ACTOR_DENIED", "Actor is no longer authorized", 403)
        with self.engine.transaction() as conn:
            rows = self._rows(conn, actor, ticket_id=ticket_id, limit=1)
            if not rows:
                raise DomainError("NOT_FOUND", "Ticket was not found", 404)
            return rows[0]

    def tickets(self, limit: int = 50) -> list["Row"]:
        if not 1 <= limit <= 50:
            raise DomainError("INVALID_LIMIT", "Ticket limit must be between 1 and 50", 400)
        if self.engine.settings.local_actor.lower() not in {
            a.lower() for a in self.engine.settings.allowed_actors
        }:
            raise DomainError("ACTOR_DENIED", "Actor is no longer authorized", 403)
        with self.engine.transaction() as conn:
            return self._rows(conn, self.engine.settings.local_actor.lower(), limit=limit)

    def ticket(self, ticket_id: UUID) -> "Row":
        ticket = self.owned_ticket(ticket_id)
        with self.engine.transaction() as conn:
            links = conn.execute(
                "SELECT related_ticket_id FROM app.ticket_links WHERE ticket_id=%s ORDER BY "
                "related_ticket_id LIMIT 3",
                (ticket_id,),
            ).fetchall()
            related = []
            for link in links:
                rows = self._rows(
                    conn, ticket["actor"], ticket["execution_mode"], link["related_ticket_id"], 1
                )
                if rows:
                    related.append(rows[0])
            runs = conn.execute(
                "SELECT * FROM app.runs WHERE ticket_id=%s ORDER BY attempt_no DESC LIMIT 20",
                (ticket_id,),
            ).fetchall()
            channels = conn.execute(
                "SELECT DISTINCT e.source FROM app.trigger_events e LEFT JOIN "
                "app.router_resolutions x ON x.event_id=e.event_id WHERE (e.ticket_id=%s OR "
                "x.ticket_id=%s) AND e.kind='user'",
                (ticket_id, ticket_id),
            ).fetchall()
            return {
                **ticket,
                "related_tickets": related,
                "runs": runs,
                "channels": [r["source"] for r in channels],
            }

    def _recall(self, conn: "Connection", event: "Row") -> list["Row"]:
        payload = event["payload"]
        body = payload["body"]
        rows = self._rows(conn, payload["actor"], payload["execution_mode"])
        grams = {body[i : i + 2].lower() for i in range(len(body) - 1)}

        def score(row: "Row") -> int:
            question = row["question"].lower()
            return sum(g in question for g in grams) + (
                10000 if str(row["ticket_id"]) in body else 0
            )

        return sorted(rows, key=score, reverse=True)[:3]

    def prepare(self, mode: str = "fixture") -> "Row | None":
        s = self.engine.settings
        with self.engine.transaction() as conn:
            prior = conn.execute(
                "SELECT * FROM app.router_batches WHERE project_id=%s AND mode=%s AND status "
                "IN ('prepared','ready','model_running') AND (expires_at>now() OR "
                "status='model_running') ORDER BY created_at LIMIT 1",
                (s.project_id, mode),
            ).fetchone()
            if prior is not None:
                return None if prior["status"] == "model_running" else prior
            pending = conn.execute(
                "SELECT e.*,h.revision FROM app.router_inbox i JOIN app.trigger_events e "
                "USING(event_id) JOIN app.threads h USING(thread_id) WHERE h.project_id=%s AND "
                "h.repository=%s AND i.next_attempt_at<=now() AND NOT EXISTS (SELECT 1 FROM "
                "app.subscriptions u WHERE u.thread_id=e.thread_id AND u.active AND "
                "u.role='primary') ORDER BY e.received_at,e.thread_id,e.thread_seq LIMIT 128",
                (s.project_id, s.repository.lower()),
            ).fetchall()
            groups: dict[str, Any] = {}
            blocked: set[str] = set()
            failed_versions: set[tuple[str, int]] = set()
            if mode == "live":
                failed = conn.execute(
                    "SELECT b.snapshot,b.status,b.reason_code FROM app.router_batches b "
                    "WHERE b.project_id=%s AND b.status IN ('failed','rejected','usage_unknown') "
                    "AND EXISTS (SELECT 1 FROM app.router_calls c WHERE c.batch_id=b.batch_id)",
                    (s.project_id,),
                ).fetchall()
                for failed_batch in failed:
                    for g in failed_batch["snapshot"]["threads"]:
                        if failed_batch["status"] == "usage_unknown":
                            blocked.add(g["thread_id"])
                        elif failed_batch["reason_code"] not in {
                            "ROUTER_REVISION_STALE",
                            "ROUTER_EPOCH_STALE",
                            "ROUTER_SUBSCRIPTION_STALE",
                        }:
                            failed_versions.add((g["thread_id"], g["revision"]))
            count = 0
            for event in pending:
                payload = event["payload"]
                if payload["actor"] not in {a.lower() for a in s.allowed_actors}:
                    continue
                thread_id = str(event["thread_id"])
                if mode == "live" and (
                    payload["actor"] != s.local_actor.lower()
                    or thread_id in blocked
                    or (thread_id, event["revision"]) in failed_versions
                ):
                    continue
                group = groups.get(thread_id)
                if group is not None and (
                    len(group["events"]) >= 4
                    or group["events"][0]["payload"]["execution_mode"] != payload["execution_mode"]
                    or group["events"][0]["payload"]["actor"] != payload["actor"]
                ):
                    continue
                if group is None:
                    group = {
                        "thread_id": thread_id,
                        "revision": event["revision"],
                        "events": [],
                        "candidates": plain(self._recall(conn, event)),
                    }
                candidate = {k: plain(event[k]) for k in ["event_id", "thread_seq", "payload"]}
                proposal = plain(groups)
                proposal.setdefault(thread_id, {**group, "events": []})["events"].append(candidate)
                if (
                    len(json.dumps(proposal, ensure_ascii=False).encode())
                    > s.router_context_bytes - 4096
                ):
                    if not groups:
                        conn.execute(
                            "UPDATE app.router_inbox SET next_attempt_at=now()+interval '1 "
                            "hour',reason_code='ROUTER_CONTEXT_LIMIT' WHERE event_id=%s",
                            (event["event_id"],),
                        )
                    continue
                groups = proposal
                count += 1
                if count >= s.router_batch_max:
                    break
            if not groups:
                return None
            batch_id = uuid4()
            expires = datetime.now(UTC) + timedelta(seconds=s.router_decision_ttl_seconds)
            snapshot = {
                "batch_id": str(batch_id),
                "project_id": s.project_id,
                "policy_version": s.policy_version,
                "expires_at": expires.isoformat(),
                "threads": list(groups.values()),
            }
            batch = conn.execute(
                "INSERT INTO app.router_batches(batch_id,project_id,policy_version,"
                "context_hash,snapshot,mode,status,expires_at) VALUES (%s,%s,%s,%s,%s,%s,"
                "'prepared',%s) RETURNING *",
                (
                    batch_id,
                    s.project_id,
                    s.policy_version,
                    digest(snapshot),
                    Jsonb(snapshot),
                    mode,
                    expires,
                ),
            ).fetchone()
            assert batch is not None
            if mode == "live":
                business = {
                    k: v for k, v in snapshot.items() if k not in {"batch_id", "expires_at"}
                }
                cached = conn.execute(
                    "SELECT b.batch_id,c.result FROM app.router_batches b "
                    "JOIN app.router_calls c USING(batch_id) WHERE b.project_id=%s "
                    "AND b.status IN ('prepared','model_running','ready') AND c.status='known' "
                    "AND c.result IS NOT NULL AND (b.snapshot-'batch_id'-'expires_at')=%s "
                    "ORDER BY b.created_at DESC LIMIT 1",
                    (s.project_id, Jsonb(business)),
                ).fetchone()
                if cached:
                    self.save(
                        batch_id, self.wrap(batch, Proposals.model_validate(cached["result"]))
                    )
                    self._audit(
                        conn,
                        batch_id,
                        "known_result_rebased",
                        previous_batch_id=str(cached["batch_id"]),
                    )
                    batch = conn.execute(
                        "SELECT * FROM app.router_batches WHERE batch_id=%s", (batch_id,)
                    ).fetchone()
            return batch

    def wrap(self, batch: "Row", proposals: Proposals) -> list[RouterDecision]:
        groups = {t["thread_id"]: t for t in batch["snapshot"]["threads"]}
        if {str(p.thread_id) for p in proposals.proposals} != set(groups) or len(
            proposals.proposals
        ) != len(groups):
            raise DomainError("ROUTER_BATCH_INCOMPLETE", "One proposal per Thread is required")
        decisions = []
        for proposal in proposals.proposals:
            group = groups[str(proposal.thread_id)]
            target = next(
                (t for t in group["candidates"] if t["ticket_id"] == str(proposal.target_id)), None
            )
            decisions.append(
                RouterDecision(
                    **proposal.model_dump(),
                    decision_key=f"route:{batch['batch_id']}:{proposal.thread_id}",
                    input_event_ids=[e["event_id"] for e in group["events"]],
                    expected_thread_revision=group["revision"],
                    policy_version=batch["policy_version"],
                    context_snapshot_hash=batch["context_hash"],
                    expires_at=batch["expires_at"],
                    mandate_id=target["mandate_id"] if target else None,
                    expected_execution_epoch=target["execution_epoch"] if target else None,
                )
            )
        return decisions

    def save(self, batch_id: UUID, decisions: list[RouterDecision]) -> None:
        with self.engine.transaction() as conn:
            row = conn.execute(
                "UPDATE app.router_batches SET decisions=%s,status='ready' WHERE batch_id=%s "
                "AND project_id=%s AND status IN ('prepared','model_running') RETURNING "
                "batch_id",
                (
                    Jsonb([d.model_dump(mode="json") for d in decisions]),
                    batch_id,
                    self.engine.settings.project_id,
                ),
            ).fetchone()
            if row is None:
                raise DomainError("ROUTER_BATCH_STATE", "Batch cannot record proposals now")

    def _validate(
        self, conn: "Connection", batch: "Row", decisions: list[RouterDecision], internal: bool
    ) -> list[tuple[RouterDecision, dict[str, Any], "Row", list["Row"]]]:
        s = self.engine.settings
        if (
            batch["project_id"] != s.project_id
            or batch["policy_version"] != s.policy_version
            or batch["context_hash"] != digest(batch["snapshot"])
            or batch["expires_at"] <= datetime.now(UTC)
        ):
            raise DomainError("ROUTER_BATCH_STALE", "Router context expired or changed")
        groups = {g["thread_id"]: g for g in batch["snapshot"]["threads"]}
        if len(decisions) != len(groups) or {str(d.thread_id) for d in decisions} != set(groups):
            raise DomainError(
                "ROUTER_BATCH_INCOMPLETE", "Batch must resolve every selected Thread once"
            )
        validated = []
        for d in decisions:
            group = groups[str(d.thread_id)]
            if (
                d.decision_key != f"route:{batch['batch_id']}:{d.thread_id}"
                or d.context_snapshot_hash != batch["context_hash"]
                or d.policy_version != s.policy_version
                or d.expected_thread_revision != group["revision"]
                or d.expires_at.tzinfo is None
                or d.expires_at != batch["expires_at"]
                or len(d.input_event_ids) != len(set(d.input_event_ids))
                or list(map(str, d.input_event_ids)) != [e["event_id"] for e in group["events"]]
            ):
                raise DomainError(
                    "ROUTER_DECISION_INVALID", "Decision differs from its frozen context"
                )
            thread = conn.execute(
                "SELECT * FROM app.threads WHERE thread_id=%s FOR UPDATE", (d.thread_id,)
            ).fetchone()
            if (
                not thread
                or thread["revision"] != d.expected_thread_revision
                or thread["project_id"] != s.project_id
            ):
                raise DomainError(
                    "ROUTER_REVISION_STALE", "Thread changed while Router was deciding"
                )
            if conn.execute(
                "SELECT 1 FROM app.subscriptions WHERE thread_id=%s AND active AND role='primary'",
                (d.thread_id,),
            ).fetchone():
                raise DomainError("ROUTER_SUBSCRIPTION_STALE", "Thread already has a primary")
            events = conn.execute(
                "SELECT e.* FROM app.router_inbox i JOIN app.trigger_events e USING(event_id) "
                "WHERE e.thread_id=%s ORDER BY e.thread_seq LIMIT %s",
                (d.thread_id, len(d.input_event_ids)),
            ).fetchall()
            if [e["event_id"] for e in events] != d.input_event_ids:
                raise DomainError(
                    "ROUTER_EVENTS_STALE", "Events are resolved or no longer the ordered prefix"
                )
            if any(
                digest(event["payload"]) != event["payload_hash"]
                or plain(event["payload"]) != frozen["payload"]
                for event, frozen in zip(events, group["events"], strict=True)
            ):
                raise DomainError(
                    "ROUTER_EVENT_CHANGED", "Accepted event differs from the frozen input"
                )
            actor = events[0]["payload"]["actor"]
            mode = events[0]["payload"]["execution_mode"]
            if actor not in {a.lower() for a in s.allowed_actors} or any(
                e["payload"]["actor"] != actor or e["payload"]["execution_mode"] != mode
                for e in events
            ):
                raise DomainError(
                    "ROUTER_ACTOR_DENIED", "Batch cannot combine actors or execution modes"
                )
            candidates = {t["ticket_id"]: t for t in group["candidates"]}
            for ticket_id in [*d.related_ticket_ids, *([d.target_id] if d.target_id else [])]:
                expected = candidates.get(str(ticket_id))
                current = self._rows(conn, actor, mode, ticket_id, 1)
                if not expected or not current:
                    raise DomainError(
                        "ROUTER_TARGET_DENIED", "Target is outside the authorized context", 403
                    )
                if any(
                    str(current[0][k]) != str(expected[k])
                    for k in [
                        "mandate_id",
                        "run_id",
                        "execution_epoch",
                        "stage",
                        "policy_version",
                        "ticket_status",
                    ]
                ):
                    raise DomainError("ROUTER_EPOCH_STALE", "Target execution changed")
            if d.target_id:
                target = candidates[str(d.target_id)]
                if (
                    str(d.mandate_id) != target["mandate_id"]
                    or d.expected_execution_epoch != target["execution_epoch"]
                ):
                    raise DomainError("ROUTER_MANDATE_INVALID", "Target mandate or epoch differs")
            elif d.mandate_id is not None or d.expected_execution_epoch is not None:
                raise DomainError("ROUTER_MANDATE_INVALID", "Target authority cannot be invented")
            if d.action in {"create_ticket", "request_clarification", "reply_system"}:
                if d.target_id or not any(addressed(e["payload"]["body"]) for e in events):
                    raise DomainError(
                        "ROUTER_INTENT_REQUIRED",
                        "New work or a reply requires an explicit request",
                        403,
                    )
                if (
                    d.action == "create_ticket"
                    and not 1 <= len(question_for(group["events"])) <= 8000
                ):
                    raise DomainError(
                        "ROUTER_QUESTION_LIMIT", "Initial task exceeds its input bound"
                    )
                if d.action != "create_ticket" and d.related_ticket_ids:
                    raise DomainError(
                        "ROUTER_ACTION_INVALID", "Only new tasks can add context links"
                    )
            elif d.action in CONTROL:
                if not d.target_id or d.related_ticket_ids:
                    raise DomainError(
                        "ROUTER_ACTION_INVALID", "Control requires one explicit target"
                    )
                expected = f"/mgar {CONTROL[d.action]}-ticket {d.target_id}"
                if not all(e["payload"]["body"] == expected for e in events):
                    raise DomainError(
                        "ROUTER_CONTROL_DENIED", "Control proposal lacks an exact user command", 403
                    )
            elif d.action == "attach_observer":
                if not internal or not d.target_id or d.related_ticket_ids:
                    raise DomainError(
                        "OBSERVER_INTERNAL_ONLY", "Observer is an internal test capability", 403
                    )
            elif d.target_id or d.related_ticket_ids:
                raise DomainError("ROUTER_ACTION_INVALID", "Ignore cannot target another task")
            if (
                d.action not in {"request_clarification", "reply_system"}
                and d.message_code is not None
            ):
                raise DomainError("ROUTER_ACTION_INVALID", "Message is not allowed for this action")
            if (
                d.action in {"request_clarification", "reply_system"}
                and d.message_code not in TEMPLATES
            ):
                raise DomainError(
                    "ROUTER_TEMPLATE_REQUIRED", "A deterministic system template is required"
                )
            if d.action == "request_clarification" and d.message_code != "clarification":
                raise DomainError(
                    "ROUTER_TEMPLATE_REQUIRED", "Clarification requires its fixed template"
                )
            validated.append((d, group, thread, events))
        return validated

    def require_primary(self, run_id: UUID, thread_id: UUID) -> None:
        with self.engine.transaction() as conn:
            run = self.engine._run(conn, run_id)
            primary = conn.execute(
                "SELECT 1 FROM app.subscriptions WHERE thread_id=%s AND run_id=%s AND active "
                "AND role='primary'",
                (thread_id, run_id),
            ).fetchone()
            if run["thread_id"] != thread_id or not primary:
                raise DomainError(
                    "OBSERVER_WRITE_DENIED",
                    "Only the owning primary can request a target write",
                    403,
                )

    def _forward_tail(
        self,
        conn: "Connection",
        decision: RouterDecision,
        run_id: UUID,
        ticket_id: UUID,
        events: list["Row"],
    ) -> None:
        tail = conn.execute(
            "SELECT e.* FROM app.router_inbox i JOIN app.trigger_events e USING(event_id) "
            "WHERE e.thread_id=%s AND e.thread_seq>%s ORDER BY e.thread_seq",
            (decision.thread_id, events[-1]["thread_seq"]),
        ).fetchall()
        if not tail:
            return
        actor = events[0]["payload"]["actor"]
        mode = events[0]["payload"]["execution_mode"]
        size = sum(
            len(json.dumps(e["payload"], ensure_ascii=False).encode()) for e in [*events, *tail]
        )
        if len(events) + len(tail) > 64 or size > 65536:
            raise DomainError(
                "MAILBOX_FULL", "Accepted Thread input exceeds the primary mailbox bound", 429
            )
        if any(
            e["payload"]["actor"] != actor or e["payload"]["execution_mode"] != mode for e in tail
        ):
            raise DomainError(
                "ROUTER_ACTOR_DENIED", "Pending tail contains a different actor or mode"
            )
        for event in tail:
            conn.execute(
                "INSERT INTO app.mailbox(run_id,event_id,seq) VALUES (%s,%s,%s)",
                (run_id, event["event_id"], event["thread_seq"]),
            )
            conn.execute(
                "INSERT INTO app.router_resolutions(resolution_id,event_id,decision_key,action,"
                "ticket_id,run_id) VALUES (%s,%s,%s,'subscription_forward',%s,%s)",
                (uuid4(), event["event_id"], decision.decision_key, ticket_id, run_id),
            )
            conn.execute("DELETE FROM app.router_inbox WHERE event_id=%s", (event["event_id"],))
        if decision.action != "create_ticket":
            self.engine._invalidate_proposals(conn, run_id)
            conn.execute(
                "UPDATE app.runs SET graph_version='r2-mailbox-v1',status='queued',"
                "execution_epoch=execution_epoch+1,proposal_generation=proposal_generation+1,"
                "reason_code='CLARIFICATION_RECEIVED' WHERE run_id=%s",
                (run_id,),
            )
        self._audit(
            conn,
            None,
            "subscription_forwarded",
            decision_key=decision.decision_key,
            event_ids=[str(e["event_id"]) for e in tail],
        )

    def commit(
        self,
        batch_id: UUID,
        decisions: list[RouterDecision],
        *,
        internal: bool = False,
        permit: "LivePermit | None" = None,
    ) -> list[dict[str, Any]]:
        # Store fixed-template bytes before the atomic business transaction.
        stored = {
            d.message_code: self.engine.artifacts.put(TEMPLATES[d.message_code].encode())
            for d in decisions
            if d.action in {"request_clarification", "reply_system"} and d.message_code in TEMPLATES
        }
        error = None
        results: list[dict[str, Any]] = []
        with self.engine.transaction() as conn:
            batch = conn.execute(
                "SELECT * FROM app.router_batches WHERE batch_id=%s AND project_id=%s FOR UPDATE",
                (batch_id, self.engine.settings.project_id),
            ).fetchone()
            if batch is None:
                raise DomainError("NOT_FOUND", "Router batch was not found", 404)
            if batch["status"] == "applied":
                saved = conn.execute(
                    "SELECT * FROM app.router_decisions WHERE batch_id=%s ORDER BY decision_key",
                    (batch_id,),
                ).fetchall()
                actual = {d.decision_key: digest(d.model_dump(mode="json")) for d in decisions}
                if {d["decision_key"]: d["payload_hash"] for d in saved} != actual:
                    raise DomainError(
                        "ROUTER_DECISION_REUSED", "Applied batch has different decisions"
                    )
                return [row["result"] for row in saved]
            if batch["status"] not in {"prepared", "ready"}:
                raise DomainError("ROUTER_BATCH_STATE", "Batch is not available for commit")
            try:
                with conn.transaction():
                    validated = self._validate(conn, batch, decisions, internal)
                    for d, group, thread, events in validated:
                        event = TriggerInput.model_validate(events[0]["payload"])
                        ticket_id = run_id = None
                        action_id = None
                        if d.action in {"create_ticket", "request_clarification", "reply_system"}:
                            question = question_for(group["events"])
                            if event.execution_mode == "live":
                                if permit is None or len(events) != 1:
                                    raise DomainError(
                                        "LIVE_PROFILE_REQUIRED",
                                        "Router cannot authorize paid Workers",
                                        403,
                                    )
                                permit.validate_event(
                                    event.model_copy(
                                        update={"body": "/mgar investigate " + question}
                                    )
                                )
                            ticket_id, run_id = self.engine._create_ticket(
                                conn,
                                event,
                                thread,
                                permit,
                                question=question,
                                basis="explicit_router_request",
                            )
                            conn.execute(
                                "UPDATE app.runs SET initial_event_ids=%s WHERE run_id=%s",
                                (Jsonb([str(e["event_id"]) for e in events]), run_id),
                            )
                            for e in events:
                                conn.execute(
                                    "INSERT INTO app.mailbox(run_id,event_id,seq) VALUES (%s,"
                                    "%s,%s)",
                                    (run_id, e["event_id"], e["thread_seq"]),
                                )
                            for related in d.related_ticket_ids:
                                conn.execute(
                                    "INSERT INTO app.ticket_links(ticket_id,related_ticket_id,"
                                    "decision_key) VALUES (%s,%s,%s)",
                                    (ticket_id, related, d.decision_key),
                                )
                            if d.action != "create_ticket":
                                conn.execute(
                                    "UPDATE app.runs SET graph_version='system-reply-v1' WHERE "
                                    "run_id=%s",
                                    (run_id,),
                                )
                                started = self.engine.begin_run(run_id)
                                action_id = self.engine.commit_report(
                                    run_id,
                                    started["execution_epoch"],
                                    TEMPLATES[d.message_code or "clarification"].encode(),
                                    Verification(
                                        passed=True,
                                        checks=["fixed_system_template"],
                                        reasons=[],
                                        scope="deterministic_system_template",
                                    ),
                                    reply_kind="system",
                                    in_reply_to_event_id=events[0]["event_id"],
                                    stored_report_hash=stored[d.message_code or "clarification"],
                                )
                                conn.execute(
                                    "UPDATE app.mailbox SET delivered_at=coalesce(delivered_at,"
                                    "now()),read_at=coalesce(read_at,now()),"
                                    "consumed_at=coalesce(consumed_at,now()) WHERE run_id=%s",
                                    (run_id,),
                                )
                                conn.execute(
                                    "UPDATE app.runs SET mailbox_cursor=%s WHERE run_id=%s",
                                    (events[-1]["thread_seq"], run_id),
                                )
                            self.engine._event(
                                conn, run_id, "router_assigned", decision_key=d.decision_key
                            )
                        elif d.action in CONTROL:
                            assert d.target_id is not None
                            target = self.owned_ticket(d.target_id, actor=event.actor)
                            ticket_id, run_id = target["ticket_id"], target["run_id"]
                            getattr(self.engine, CONTROL[d.action])(run_id)
                        elif d.action == "attach_observer":
                            assert d.target_id is not None
                            target = self.owned_ticket(d.target_id, actor=event.actor)
                            ticket_id, run_id = target["ticket_id"], target["run_id"]
                            conn.execute(
                                "INSERT INTO app.subscriptions(thread_id,run_id,role) VALUES "
                                "(%s,%s,'observer') ON CONFLICT(thread_id,run_id) DO UPDATE "
                                "SET role='observer',active=true",
                                (d.thread_id, run_id),
                            )
                            for e in events:
                                conn.execute(
                                    "INSERT INTO app.observer_deliveries(run_id,event_id,"
                                    "source_thread_id,seq) VALUES (%s,%s,%s,%s)",
                                    (run_id, e["event_id"], d.thread_id, e["thread_seq"]),
                                )
                        result = plain(
                            {
                                "thread_id": d.thread_id,
                                "event_ids": d.input_event_ids,
                                "action": d.action,
                                "ticket_id": ticket_id,
                                "run_id": run_id,
                                "action_id": action_id,
                            }
                        )
                        conn.execute(
                            "INSERT INTO app.router_decisions(decision_key,batch_id,"
                            "payload_hash,payload,result) VALUES (%s,%s,%s,%s,%s)",
                            (
                                d.decision_key,
                                batch_id,
                                digest(d.model_dump(mode="json")),
                                Jsonb(d.model_dump(mode="json")),
                                Jsonb(result),
                            ),
                        )
                        for event_id in d.input_event_ids:
                            conn.execute(
                                "INSERT INTO app.router_resolutions(resolution_id,event_id,"
                                "decision_key,action,ticket_id,run_id) VALUES (%s,%s,%s,%s,%s,"
                                "%s)",
                                (uuid4(), event_id, d.decision_key, d.action, ticket_id, run_id),
                            )
                            conn.execute(
                                "DELETE FROM app.router_inbox WHERE event_id=%s", (event_id,)
                            )
                        if d.action in {"create_ticket", "request_clarification", "reply_system"}:
                            assert run_id is not None and ticket_id is not None
                            self._forward_tail(conn, d, run_id, ticket_id, events)
                        results.append(result)
                    conn.execute(
                        "UPDATE app.router_batches SET status='applied' WHERE batch_id=%s",
                        (batch_id,),
                    )
                    self._audit(conn, batch_id, "batch_applied", decisions=len(decisions))
            except DomainError as exc:
                error = exc
                results = []
                conn.execute(
                    "UPDATE app.router_batches SET status='rejected',reason_code=%s WHERE "
                    "batch_id=%s",
                    (exc.code, batch_id),
                )
                self._audit(conn, batch_id, "batch_rejected", exc.code)
                for group in batch["snapshot"]["threads"]:
                    for e in group["events"]:
                        conn.execute(
                            "UPDATE app.router_inbox SET next_attempt_at=now()+interval '2 "
                            "seconds',reason_code=%s WHERE event_id=%s",
                            (exc.code, e["event_id"]),
                        )
        if error is not None:
            raise error
        return results

    def failure(self, batch_id: UUID, reason: str) -> None:
        with self.engine.transaction() as conn:
            batch = conn.execute(
                "UPDATE app.router_batches SET status=CASE WHEN status='usage_unknown' THEN "
                "status ELSE 'failed' END,reason_code=%s WHERE batch_id=%s AND project_id=%s "
                "AND status NOT IN ('applied','rejected') RETURNING snapshot",
                (reason, batch_id, self.engine.settings.project_id),
            ).fetchone()
            if batch:
                self._audit(conn, batch_id, "router_failure", reason)
                for g in batch["snapshot"]["threads"]:
                    for e in g["events"]:
                        conn.execute(
                            "UPDATE app.router_inbox SET next_attempt_at=now()+interval '60 "
                            "seconds',reason_code=%s WHERE event_id=%s",
                            (reason, e["event_id"]),
                        )

    def metrics(self) -> dict[str, Any]:
        with self.engine.transaction() as conn:
            queued = conn.execute(
                "SELECT count(*) AS n FROM app.router_inbox i JOIN app.trigger_events e "
                "USING(event_id) JOIN app.threads h USING(thread_id) WHERE h.project_id=%s",
                (self.engine.settings.project_id,),
            ).fetchone()
            calls = conn.execute(
                "SELECT count(*) AS n FROM app.router_calls c JOIN app.router_batches b "
                "USING(batch_id) WHERE b.project_id=%s",
                (self.engine.settings.project_id,),
            ).fetchone()
            batches = conn.execute(
                "SELECT status,count(*) AS n FROM app.router_batches WHERE project_id=%s GROUP "
                "BY status",
                (self.engine.settings.project_id,),
            ).fetchall()
            return {
                "mode": self.engine.settings.router_mode,
                "pending_events": queued["n"] if queued else 0,
                "physical_router_calls": calls["n"] if calls else 0,
                "batches": {b["status"]: b["n"] for b in batches},
            }

    def correct(self, event_ids: list[UUID], revision: int, reason: str) -> UUID:
        with self.engine.transaction() as conn:
            rows = conn.execute(
                "SELECT e.*,x.decision_key,x.run_id AS resolved_run,x.action AS "
                "resolved_action FROM app.trigger_events e JOIN app.router_resolutions x "
                "USING(event_id) WHERE e.event_id=ANY(%s) AND x.superseded_at IS NULL ORDER BY "
                "e.thread_seq",
                (event_ids,),
            ).fetchall()
            if len(rows) != len(event_ids) or len({r["thread_id"] for r in rows}) != 1:
                raise DomainError("CORRECTION_INVALID", "Correction requires one resolved Thread")
            if any(
                r["resolved_action"]
                not in {
                    "ignore",
                    "create_ticket",
                    "request_clarification",
                    "reply_system",
                    "subscription_forward",
                }
                for r in rows
            ):
                raise DomainError(
                    "CORRECTION_ACTION_UNSUPPORTED",
                    "Control and observer decisions cannot be reopened",
                )
            thread = conn.execute(
                "SELECT * FROM app.threads WHERE thread_id=%s FOR UPDATE", (rows[0]["thread_id"],)
            ).fetchone()
            s = self.engine.settings
            if (
                thread is None
                or thread["project_id"] != s.project_id
                or thread["repository"] != s.repository.lower()
                or any(r["payload"]["actor"] != s.local_actor.lower() for r in rows)
            ):
                raise DomainError("NOT_FOUND", "Routing input was not found", 404)
            if thread["revision"] != revision:
                raise DomainError("ROUTER_REVISION_STALE", "Correction version changed")
            keys = {r["decision_key"] for r in rows}
            all_ids = conn.execute(
                "SELECT event_id FROM app.router_resolutions WHERE decision_key=ANY(%s) AND "
                "superseded_at IS NULL",
                (list(keys),),
            ).fetchall()
            if {r["event_id"] for r in all_ids} != set(event_ids):
                raise DomainError("CORRECTION_INCOMPLETE", "Correct the complete original decision")
            for run_id in {r["resolved_run"] for r in rows if r["resolved_run"]}:
                unsafe = conn.execute(
                    "SELECT 1 FROM app.evidence WHERE run_id=%s UNION ALL SELECT 1 FROM "
                    "app.usage_calls WHERE run_id=%s UNION ALL SELECT 1 FROM app.read_jobs "
                    "WHERE run_id=%s UNION ALL SELECT 1 FROM app.actions WHERE run_id=%s AND "
                    "status IN ('dispatching','outcome_unknown','succeeded')",
                    (run_id, run_id, run_id, run_id),
                ).fetchone()
                if unsafe:
                    raise DomainError(
                        "CORRECTION_EXECUTION_STARTED",
                        "Executed work cannot be silently reassigned",
                    )
                self.engine.cancel(run_id)
            conn.execute(
                "UPDATE app.threads SET revision=revision+1 WHERE thread_id=%s",
                (thread["thread_id"],),
            )
            correction_id = uuid4()
            for index, r in enumerate(rows):
                conn.execute(
                    "UPDATE app.router_resolutions SET superseded_at=now() WHERE event_id=%s "
                    "AND superseded_at IS NULL",
                    (r["event_id"],),
                )
                conn.execute(
                    "INSERT INTO app.router_inbox(event_id) VALUES (%s) ON CONFLICT DO NOTHING",
                    (r["event_id"],),
                )
                conn.execute(
                    "INSERT INTO app.routing_corrections(correction_id,event_id,"
                    "previous_decision_key,actor,reason,thread_revision) VALUES (%s,%s,%s,%s,"
                    "%s,%s)",
                    (
                        correction_id if index == 0 else uuid4(),
                        r["event_id"],
                        r["decision_key"],
                        s.local_actor.lower(),
                        reason,
                        revision + 1,
                    ),
                )
            self._audit(
                conn,
                None,
                "routing_corrected",
                actor=s.local_actor,
                correction_id=correction_id,
                event_ids=event_ids,
                reason=reason,
            )
            return correction_id

    def recover(self) -> None:
        with self.engine.transaction() as conn:
            conn.execute("UPDATE app.router_calls SET status='unknown' WHERE status='reserved'")
            conn.execute(
                "UPDATE app.router_batches b SET status='usage_unknown',"
                "reason_code='USAGE_UNKNOWN' WHERE EXISTS (SELECT 1 FROM app.router_calls c "
                "WHERE c.batch_id=b.batch_id AND c.status='unknown')"
            )
            conn.execute(
                "UPDATE app.router_batches SET status='prepared' WHERE status='model_running' "
                "AND NOT EXISTS (SELECT 1 FROM app.router_calls c WHERE "
                "c.batch_id=router_batches.batch_id AND c.status='unknown')"
            )
