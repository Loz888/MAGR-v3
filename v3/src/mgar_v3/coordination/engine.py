import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from typing import Any, TypeVar
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from mgar_v3.agents.tools import ToolAuthority
from mgar_v3.context.store import ContextStore
from mgar_v3.domain import (
    DomainError,
    EventReceipt,
    Evidence,
    Investigation,
    Model,
    TriggerInput,
    Verification,
    digest,
)
from mgar_v3.evaluation.store import Trials
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.knowledge.store import Knowledge
from mgar_v3.live import LivePermit
from mgar_v3.repair.store import Repairs
from mgar_v3.routing.store import Routing
from mgar_v3.settings import Settings

TModel = TypeVar("TModel", bound=Model)
Row = dict[str, Any]
Connection = psycopg.Connection[Row]
ENGINE_LOCK = 31010002
COMMAND = "/mgar investigate "
TERMINAL = {"succeeded", "failed", "cancelled"}
DUPLEX_VERSIONS = {"r2-mailbox-v1", "r4-experts-v1", "r5-repair-v1"}


class Engine:
    """One PG session owns the coordination lock and all global business transactions."""

    def __init__(self, settings: Settings, artifacts: ArtifactStore):
        self.settings = settings
        self.artifacts = artifacts
        self._mutex = threading.RLock()
        self.connection: Connection = psycopg.connect(
            settings.database_url.get_secret_value(), autocommit=True, row_factory=dict_row
        )
        result = self.connection.execute(
            "SELECT pg_try_advisory_lock(%s) AS acquired", (ENGINE_LOCK,)
        )
        row = result.fetchone()
        if not row or not row["acquired"]:
            self.connection.close()
            raise DomainError(
                "ENGINE_ALREADY_RUNNING", "Another control instance owns this database"
            )
        self.routing = Routing(self)
        self.knowledge = Knowledge(self)
        self.context = ContextStore(self)
        self.tools = ToolAuthority(self)
        self.trials = Trials(self)
        self.repairs = Repairs(self)

    def close(self) -> None:
        with self._mutex:
            self.connection.close()

    @contextmanager
    def transaction(self) -> Iterator[Connection]:
        with self._mutex, self.connection.transaction():
            yield self.connection

    def _event(self, conn: Connection, run_id: UUID, kind: str, **payload: Any) -> None:
        conn.execute(
            "INSERT INTO app.run_events(run_id,kind,safe_payload) VALUES (%s,%s,%s)",
            (run_id, kind, Jsonb(payload)),
        )

    def _run(self, conn: Connection, run_id: UUID, epoch: int | None = None) -> Row:
        row = conn.execute(
            "SELECT r.*,t.thread_id,t.mandate_id,t.status AS ticket_status,"
            "h.project_id,h.repository,h.external_key,h.revision AS thread_revision "
            "FROM app.runs r JOIN app.tickets t USING(ticket_id) "
            "JOIN app.threads h USING(thread_id) WHERE r.run_id=%s FOR UPDATE OF r,t,h",
            (run_id,),
        ).fetchone()
        if row is None:
            raise DomainError("NOT_FOUND", "Run was not found", 404)
        if row["project_id"] != self.settings.project_id:
            raise DomainError("PROJECT_DENIED", "Run is outside the configured project", 403)
        if epoch is not None and (row["execution_epoch"] != epoch or row["status"] in TERMINAL):
            raise DomainError("FENCED", "Execution is no longer authorized")
        return row

    def ingest(self, event: TriggerInput, permit: LivePermit | None = None) -> EventReceipt:
        if event.project_id != self.settings.project_id:
            raise DomainError("PROJECT_DENIED", "Project is outside the configured scope", 403)
        if event.repository != self.settings.repository.lower():
            raise DomainError(
                "REPOSITORY_DENIED", "Repository is outside the configured scope", 403
            )
        if event.actor not in {actor.lower() for actor in self.settings.allowed_actors}:
            raise DomainError("ACTOR_DENIED", "Actor is not authorized", 403)
        # R1 live requires the reviewed runner; ordinary ingress cannot enable paid execution.
        if event.execution_mode != "fixture":
            if permit is None or not self.settings.allow_paid:
                raise DomainError(
                    "LIVE_PROFILE_REQUIRED", "Live ingress requires a reviewed profile", 403
                )
            permit.validate()
        payload = event.model_dump(mode="json")
        payload_hash = digest(payload)
        with self.transaction() as conn:
            existing = conn.execute(
                "SELECT * FROM app.trigger_events WHERE source=%s AND external_event_id=%s",
                (event.source, event.external_event_id),
            ).fetchone()
            if existing:
                if existing["payload_hash"] != payload_hash:
                    raise DomainError(
                        "EVENT_ID_REUSED", "Delivery identifier has different content"
                    )
                return self._receipt(existing, duplicate=True)
            external_key = f"{event.repository}#issue:{event.issue_number}"
            conn.execute(
                "INSERT INTO app.threads(thread_id,project_id,repository,external_key) "
                "VALUES (%s,%s,%s,%s) ON CONFLICT(project_id,external_key) DO NOTHING",
                (uuid4(), event.project_id, event.repository, external_key),
            )
            thread = conn.execute(
                "UPDATE app.threads SET latest_seq=latest_seq+1,revision=revision+1 "
                "WHERE project_id=%s AND external_key=%s RETURNING *",
                (event.project_id, external_key),
            ).fetchone()
            assert thread is not None
            subscriptions = conn.execute(
                "SELECT s.run_id,s.role,r.ticket_id,r.graph_version,r.execution_mode "
                "FROM app.subscriptions s "
                "JOIN app.runs r USING(run_id) WHERE s.thread_id=%s AND s.active "
                "ORDER BY s.role,s.run_id",
                (thread["thread_id"],),
            ).fetchall()
            primary = next((s for s in subscriptions if s["role"] == "primary"), None)
            if permit is not None:
                permit.validate_event(
                    event,
                    follow_up=bool(
                        primary
                        and primary["graph_version"] in DUPLEX_VERSIONS | {"system-reply-v1"}
                    ),
                )
            route = "unrouted"
            ticket_id, run_id = None, None
            if primary:
                if primary["execution_mode"] != event.execution_mode:
                    raise DomainError("EXECUTION_MODE_MISMATCH", "Follow-up mode differs from Run")
                pending = conn.execute(
                    "SELECT count(*) AS n,coalesce(sum(octet_length(e.payload::text)),0) AS bytes "
                    "FROM app.mailbox m JOIN app.trigger_events e USING(event_id) "
                    "WHERE m.run_id=%s AND m.consumed_at IS NULL",
                    (primary["run_id"],),
                ).fetchone()
                assert pending is not None
                jobs = conn.execute(
                    "SELECT count(*) AS n FROM app.read_jobs WHERE run_id=%s "
                    "AND status IN ('queued','running')",
                    (primary["run_id"],),
                ).fetchone()
                assert jobs is not None
                incoming_bytes = conn.execute(
                    "SELECT octet_length(%s::jsonb::text) AS n", (Jsonb(payload),)
                ).fetchone()
                assert incoming_bytes is not None
                if (
                    pending["n"] + jobs["n"] >= 64
                    or pending["bytes"] + incoming_bytes["n"] + jobs["n"] * 1024 > 65536
                ):
                    raise DomainError("MAILBOX_FULL", "Run mailbox reached its input limit", 429)
                route = "direct"
                ticket_id, run_id = primary["ticket_id"], primary["run_id"]
            elif (
                event.body.startswith(COMMAND)
                and event.body[len(COMMAND) :].strip()
                and not conn.execute(
                    "SELECT 1 FROM app.router_inbox i JOIN app.trigger_events e "
                    "USING(event_id) WHERE e.thread_id=%s",
                    (thread["thread_id"],),
                ).fetchone()
            ):
                ticket_id, run_id = self._create_ticket(conn, event, thread, permit)
                route = "created"
                subscriptions = [
                    *subscriptions,
                    {"run_id": run_id, "role": "primary", "ticket_id": ticket_id},
                ]
            event_id = uuid4()
            snapshot = [{"run_id": str(s["run_id"]), "role": s["role"]} for s in subscriptions]
            row = conn.execute(
                "INSERT INTO app.trigger_events(event_id,source,external_event_id,thread_id,"
                "thread_seq,payload_hash,payload,ticket_id,run_id,route,route_snapshot) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
                (
                    event_id,
                    event.source,
                    event.external_event_id,
                    thread["thread_id"],
                    thread["latest_seq"],
                    payload_hash,
                    Jsonb(payload),
                    ticket_id,
                    run_id,
                    route,
                    Jsonb(snapshot),
                ),
            ).fetchone()
            for subscription in subscriptions:
                if subscription["role"] == "primary":
                    conn.execute(
                        "INSERT INTO app.mailbox(run_id,event_id,seq) VALUES (%s,%s,%s)",
                        (subscription["run_id"], event_id, thread["latest_seq"]),
                    )
                else:
                    conn.execute(
                        "INSERT INTO app.observer_deliveries(run_id,event_id,source_thread_id,seq) "
                        "VALUES (%s,%s,%s,%s)",
                        (
                            subscription["run_id"],
                            event_id,
                            thread["thread_id"],
                            thread["latest_seq"],
                        ),
                    )
            if route == "unrouted":
                pending_router = conn.execute(
                    "SELECT count(*) AS n FROM app.router_inbox"
                ).fetchone()
                assert pending_router is not None
                if pending_router["n"] >= 128:
                    raise DomainError("ROUTER_BACKLOG_FULL", "Unrouted event queue is full", 429)
                conn.execute("INSERT INTO app.router_inbox(event_id) VALUES (%s)", (event_id,))
            elif run_id:
                self._event(conn, run_id, "input_accepted", event_id=str(event_id), route=route)
                if primary and primary["graph_version"] in DUPLEX_VERSIONS | {"system-reply-v1"}:
                    run = self._run(conn, run_id)
                    in_flight = conn.execute(
                        "SELECT 1 FROM app.actions WHERE run_id=%s "
                        "AND status IN ('dispatching','outcome_unknown')",
                        (run_id,),
                    ).fetchone()
                    if run["graph_version"] == "system-reply-v1" and not in_flight:
                        self._invalidate_proposals(conn, run_id)
                        conn.execute(
                            (
                                "UPDATE app.runs SET "
                                "graph_version=%s,status='queued',execution_epoch=execution_epoch+"
                                "1,proposal_generation=proposal_generation+1,reason_code='CLARIFIC"
                                "ATION_RECEIVED' WHERE run_id=%s"
                            ),
                            (
                                "r4-experts-v1"
                                if self.settings.graph_version == "r4-experts-v1"
                                else "r2-mailbox-v1",
                                run_id,
                            ),
                        )
                    elif run["status"] == "waiting_approval" and not in_flight:
                        self._invalidate_proposals(conn, run_id)
                        conn.execute(
                            "UPDATE app.runs SET status='queued',execution_epoch=execution_epoch+1,"
                            "reason_code='REPLAN_REQUIRED' WHERE run_id=%s",
                            (run_id,),
                        )
            assert row is not None
            return self._receipt(row)

    def _create_ticket(
        self,
        conn: Connection,
        event: TriggerInput,
        thread: Row,
        permit: LivePermit | None = None,
        *,
        question: str | None = None,
        basis: str = "explicit_command",
    ) -> tuple[UUID, UUID]:
        mandate_id, ticket_id, run_id = uuid4(), uuid4(), uuid4()
        scope = {
            "repository": event.repository,
            "issue_number": event.issue_number,
            "actions": ["issue_comment"],
            "basis": basis,
        }
        if permit is not None:
            scope["profile_hash"] = permit.profile.profile_hash
            scope["model_config"] = permit.profile.model_dump(mode="json")
            conn.execute(
                "INSERT INTO app.authorization_budgets(profile_hash,limit_micros,call_limit) "
                "VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                (
                    permit.profile.profile_hash,
                    permit.profile.max_cost_micros,
                    permit.profile.max_model_calls,
                ),
            )
        conn.execute(
            "INSERT INTO app.mandates(mandate_id,project_id,actor,scope,"
            "policy_version,execution_mode) "
            "VALUES (%s,%s,%s,%s,%s,%s)",
            (
                mandate_id,
                event.project_id,
                event.actor,
                Jsonb(scope),
                self.settings.policy_version,
                event.execution_mode,
            ),
        )
        conn.execute(
            "INSERT INTO app.budgets(mandate_id,limit_micros) VALUES (%s,%s)",
            (mandate_id, permit.profile.max_cost_micros if permit else 0),
        )
        conn.execute(
            "INSERT INTO app.tickets(ticket_id,mandate_id,thread_id,status) "
            "VALUES (%s,%s,%s,'open')",
            (ticket_id, mandate_id, thread["thread_id"]),
        )
        conn.execute(
            "INSERT INTO app.runs(run_id,ticket_id,attempt_no,graph_version,execution_mode,"
            "status,question,input_revision) VALUES (%s,%s,1,%s,%s,'queued',%s,%s)",
            (
                run_id,
                ticket_id,
                self.settings.graph_version,
                event.execution_mode,
                question if question is not None else event.body[len(COMMAND) :].strip(),
                thread["revision"],
            ),
        )
        conn.execute(
            "INSERT INTO app.subscriptions(thread_id,run_id,role) VALUES (%s,%s,'primary')",
            (thread["thread_id"], run_id),
        )
        return ticket_id, run_id

    def follow_up(
        self, run_id: UUID, client_id: str, body: str, permit: LivePermit | None = None
    ) -> EventReceipt:
        with self.transaction() as conn:
            run = self._run(conn, run_id)
            existing = conn.execute(
                "SELECT * FROM app.trigger_events WHERE source='cli' AND external_event_id=%s",
                (client_id,),
            ).fetchone()
            if existing:
                if existing["run_id"] != run_id or existing["payload"].get("body") != body:
                    raise DomainError(
                        "EVENT_ID_REUSED", "Delivery identifier has different content"
                    )
                return self._receipt(existing, duplicate=True)
            if (
                run["graph_version"] not in DUPLEX_VERSIONS | {"system-reply-v1"}
                or run["status"] in TERMINAL
            ):
                raise DomainError("RUN_INPUT_CLOSED", "This Run cannot receive follow-up input")
            primary = conn.execute(
                "SELECT 1 FROM app.subscriptions WHERE run_id=%s AND active AND role='primary'",
                (run_id,),
            ).fetchone()
            if not primary:
                raise DomainError("RUN_INPUT_CLOSED", "Run no longer owns this conversation")
            return self.ingest(
                TriggerInput(
                    source="cli",
                    external_event_id=client_id,
                    project_id=run["project_id"],
                    repository=run["repository"],
                    issue_number=int(run["external_key"].rsplit(":", 1)[1]),
                    actor=self.settings.local_actor,
                    body=body,
                    execution_mode=run["execution_mode"],
                ),
                permit,
            )

    def is_own_result(self, repository: str, issue_number: int, body: str) -> bool:
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT 1 FROM app.actions a JOIN app.runs r USING(run_id) "
                "JOIN app.tickets t USING(ticket_id) JOIN app.threads h USING(thread_id) "
                "WHERE h.project_id=%s AND h.repository=%s AND h.external_key=%s "
                "AND a.payload->>'body'=%s "
                "AND a.status IN ('dispatching','outcome_unknown','succeeded')",
                (
                    self.settings.project_id,
                    repository.lower(),
                    f"{repository.lower()}#issue:{issue_number}",
                    body,
                ),
            ).fetchone()
            return row is not None

    @staticmethod
    def _receipt(row: Row, duplicate: bool = False) -> EventReceipt:
        return EventReceipt(
            event_id=row["event_id"],
            thread_id=row["thread_id"],
            thread_seq=row["thread_seq"],
            ticket_id=row["ticket_id"],
            run_id=row["run_id"],
            route=row["route"],
            duplicate=duplicate,
        )

    def begin_run(self, run_id: UUID) -> Row:
        with self.transaction() as conn:
            row = self._run(conn, run_id)
            if row["status"] != "queued":
                raise DomainError("RUN_NOT_QUEUED", "Run cannot start in its current state")
            if (
                row["graph_version"] not in DUPLEX_VERSIONS
                and row["thread_revision"] != row["input_revision"]
            ):
                raise DomainError("STALE_INPUT", "New input requires explicit intake/replanning")
            updated = conn.execute(
                "UPDATE app.runs SET status='running',execution_epoch=execution_epoch+1 "
                "WHERE run_id=%s RETURNING execution_epoch",
                (run_id,),
            ).fetchone()
            assert updated is not None
            conn.execute(
                "UPDATE app.mailbox SET delivered_at=coalesce(delivered_at,now()),"
                "read_at=coalesce(read_at,now()) WHERE run_id=%s "
                "AND seq=(SELECT min(seq) FROM app.mailbox WHERE run_id=%s)",
                (run_id, run_id),
            )
            self._event(conn, run_id, "run_started", execution_epoch=updated["execution_epoch"])
            return {**row, **updated, "status": "running"}

    def prepare_intake(self, run_id: UUID, epoch: int) -> Row:
        """Prepare immutable graph input; consumption belongs to the next graph node."""
        with self.transaction() as conn:
            run = self._run(conn, run_id, epoch)
            if run["status"] != "running":
                raise DomainError("FENCED", "Intake requires the running execution")
            messages = conn.execute(
                "SELECT m.*,e.kind,e.payload FROM app.mailbox m "
                "JOIN app.trigger_events e USING(event_id) WHERE m.run_id=%s ORDER BY m.seq",
                (run_id,),
            ).fetchall()
            if not messages:
                raise DomainError("INPUT_MISSING", "Run has no durable initial input")
            if conn.execute(
                "SELECT 1 FROM app.mailbox WHERE run_id=%s AND consumed_at IS NULL "
                "AND expires_at<=now()",
                (run_id,),
            ).fetchone():
                raise DomainError("MAILBOX_EXPIRED", "Unconsumed input expired and needs review")
            cursor = messages[-1]["seq"]
            existing = conn.execute(
                "SELECT * FROM app.run_inputs WHERE run_id=%s AND cursor=%s", (run_id, cursor)
            ).fetchone()
            if existing is not None:
                return existing
            versions = conn.execute(
                "SELECT count(DISTINCT input_revision) AS n FROM app.run_inputs WHERE run_id=%s",
                (run_id,),
            ).fetchone()
            current = conn.execute(
                "SELECT 1 FROM app.run_inputs WHERE run_id=%s AND input_revision=%s",
                (run_id, run["thread_revision"]),
            ).fetchone()
            assert versions is not None
            if versions["n"] >= 3 and not current:
                raise DomainError("REPLAN_LIMIT", "Run reached its bounded input-version limit")
            users = [m for m in messages if m["kind"] == "user"]
            initial_ids = set(run["initial_event_ids"])
            followups = (
                [m for m in users if str(m["event_id"]) not in initial_ids]
                if initial_ids
                else users[1:]
            )
            question = run["question"] + "".join(
                f"\n\nFollow-up [{m['seq']}]: {m['payload']['body']}" for m in followups
            )
            if len(question.encode()) > 65536:
                raise DomainError("CONTEXT_LIMIT", "Input snapshot exceeds its context bound")
            event_ids = [str(m["event_id"]) for m in messages]
            snapshot_hash = digest(
                {
                    "cursor": cursor,
                    "revision": run["thread_revision"],
                    "question": question,
                    "event_ids": event_ids,
                }
            )
            snapshot = conn.execute(
                "INSERT INTO app.run_inputs(snapshot_id,run_id,cursor,input_revision,question,"
                "event_ids,snapshot_hash) VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING *",
                (
                    uuid4(),
                    run_id,
                    cursor,
                    run["thread_revision"],
                    question,
                    Jsonb(event_ids),
                    snapshot_hash,
                ),
            ).fetchone()
            conn.execute(
                "UPDATE app.mailbox SET delivered_at=coalesce(delivered_at,now()),"
                "read_at=coalesce(read_at,now()) WHERE run_id=%s AND seq<=%s",
                (run_id, cursor),
            )
            assert snapshot is not None
            self._event(
                conn,
                run_id,
                "intake_prepared",
                snapshot_id=str(snapshot["snapshot_id"]),
                cursor=cursor,
                input_revision=run["thread_revision"],
            )
            return snapshot

    def acknowledge_intake(self, run_id: UUID, epoch: int, snapshot_id: UUID) -> Row:
        with self.transaction() as conn:
            run = self._run(conn, run_id, epoch)
            if run["status"] != "running":
                raise DomainError("FENCED", "Input consumption requires a running execution")
            snapshot = conn.execute(
                "SELECT * FROM app.run_inputs WHERE run_id=%s AND snapshot_id=%s",
                (run_id, snapshot_id),
            ).fetchone()
            if snapshot is None:
                raise DomainError("INPUT_SNAPSHOT_MISSING", "Graph input is not in the ledger")
            if snapshot["cursor"] <= run["mailbox_cursor"]:
                if snapshot["cursor"] < run["mailbox_cursor"]:
                    current = conn.execute(
                        "SELECT * FROM app.run_inputs WHERE run_id=%s AND cursor=%s",
                        (run_id, run["mailbox_cursor"]),
                    ).fetchone()
                    if current is None:
                        raise DomainError(
                            "INPUT_SNAPSHOT_MISSING", "Consumption snapshot is missing"
                        )
                    return current
                return snapshot
            conn.execute(
                "UPDATE app.mailbox SET consumed_at=coalesce(consumed_at,now()) "
                "WHERE run_id=%s AND seq<=%s",
                (run_id, snapshot["cursor"]),
            )
            conn.execute(
                "UPDATE app.runs SET mailbox_cursor=%s,input_revision=%s WHERE run_id=%s",
                (snapshot["cursor"], snapshot["input_revision"], run_id),
            )
            self._event(
                conn,
                run_id,
                "input_consumed",
                snapshot_id=str(snapshot_id),
                cursor=snapshot["cursor"],
                input_revision=snapshot["input_revision"],
            )
            return snapshot

    def _invalidate_proposals(self, conn: Connection, run_id: UUID) -> int:
        rows = conn.execute(
            "UPDATE app.actions SET status='expired' WHERE run_id=%s "
            "AND status IN ('proposed','approved','ready') RETURNING action_id",
            (run_id,),
        ).fetchall()
        conn.execute(
            "UPDATE app.outbox SET status='cancelled' WHERE action_id IN "
            "(SELECT action_id FROM app.actions WHERE run_id=%s AND status='expired')",
            (run_id,),
        )
        return len(rows)

    def pause(self, run_id: UUID) -> Row:
        with self.transaction() as conn:
            run = self._run(conn, run_id)
            if run["graph_version"] not in DUPLEX_VERSIONS:
                raise DomainError("CONTROL_UNSUPPORTED", "Pause requires the R2 graph")
            if run["status"] in TERMINAL or run["status"] == "paused":
                return run
            invalidated = self._invalidate_proposals(conn, run_id)
            conn.execute(
                "UPDATE app.runs SET status='paused',execution_epoch=execution_epoch+1,"
                "proposal_generation=proposal_generation+%s,reason_code='USER_PAUSED' "
                "WHERE run_id=%s",
                (int(invalidated > 0), run_id),
            )
            conn.execute(
                "UPDATE app.read_jobs SET status='queued',lease_token=NULL "
                "WHERE run_id=%s AND status='running'",
                (run_id,),
            )
            self._event(conn, run_id, "run_paused")
            return {**run, "status": "paused", "execution_epoch": run["execution_epoch"] + 1}

    def requeue_input(self, run_id: UUID, epoch: int) -> None:
        with self.transaction() as conn:
            run = self._run(conn, run_id, epoch)
            if run["status"] != "running":
                raise DomainError("FENCED", "Only a running execution can replan")
            conn.execute(
                "UPDATE app.runs SET status='queued',reason_code='REPLAN_REQUIRED' WHERE run_id=%s",
                (run_id,),
            )
            self._event(conn, run_id, "replan_queued")

    def submit_read_job(self, run_id: UUID, epoch: int, step_key: str) -> Row:
        with self.transaction() as conn:
            run = self._run(conn, run_id, epoch)
            if run["status"] != "running":
                raise DomainError("FENCED", "Read job requires a running execution")
            conn.execute(
                "INSERT INTO app.read_jobs(job_id,run_id,step_key,execution_epoch,status) "
                "VALUES (%s,%s,%s,%s,'queued') ON CONFLICT(run_id,step_key) DO NOTHING",
                (uuid4(), run_id, step_key, epoch),
            )
            job = conn.execute(
                "SELECT * FROM app.read_jobs WHERE run_id=%s AND step_key=%s", (run_id, step_key)
            ).fetchone()
            assert job is not None
            return job

    def claim_read_job(self, job_id: UUID, epoch: int) -> Row:
        with self.transaction() as conn:
            job = conn.execute(
                "SELECT * FROM app.read_jobs WHERE job_id=%s FOR UPDATE", (job_id,)
            ).fetchone()
            if job is None:
                raise DomainError("NOT_FOUND", "Read job was not found", 404)
            run = self._run(conn, job["run_id"], epoch)
            if run["status"] != "running" or job["status"] != "queued":
                raise DomainError("FENCED", "Read job is not available for this execution")
            claimed = conn.execute(
                "UPDATE app.read_jobs SET status='running',execution_epoch=%s,lease_token=%s "
                "WHERE job_id=%s RETURNING *",
                (epoch, uuid4(), job_id),
            ).fetchone()
            assert claimed is not None
            self._event(conn, job["run_id"], "tool_started", job_id=str(job_id))
            return claimed

    def finish_read_job(
        self, job_id: UUID, lease: UUID, content: bytes, uri: str, version: str
    ) -> Row:
        if len(content) > 65536:
            raise DomainError("TOOL_OUTPUT_LIMIT", "Read result exceeds its byte bound")
        content_hash = self.artifacts.put(content)
        with self.transaction() as conn:
            job = conn.execute(
                "SELECT * FROM app.read_jobs WHERE job_id=%s FOR UPDATE", (job_id,)
            ).fetchone()
            if job is None or job["status"] != "running" or job["lease_token"] != lease:
                raise DomainError("FENCED", "Read result lease is no longer current")
            run = self._run(conn, job["run_id"], job["execution_epoch"])
            if run["status"] != "running":
                raise DomainError("FENCED", "Read result no longer owns a running execution")
            conn.execute(
                "INSERT INTO app.artifacts(content_hash,byte_count) VALUES (%s,%s) "
                "ON CONFLICT DO NOTHING",
                (content_hash, len(content)),
            )
            result = conn.execute(
                "UPDATE app.read_jobs SET status='succeeded',lease_token=NULL,content_hash=%s,"
                "source_uri=%s,source_version=%s WHERE job_id=%s RETURNING *",
                (content_hash, uri, version, job_id),
            ).fetchone()
            thread = conn.execute(
                "UPDATE app.threads SET latest_seq=latest_seq+1 "
                "WHERE thread_id=%s RETURNING latest_seq",
                (run["thread_id"],),
            ).fetchone()
            assert thread is not None and result is not None
            payload = {"job_id": str(job_id), "content_hash": content_hash}
            event_id = uuid4()
            conn.execute(
                "INSERT INTO app.trigger_events(event_id,source,external_event_id,thread_id,"
                "thread_seq,payload_hash,payload,ticket_id,run_id,route,route_snapshot,kind) "
                "VALUES (%s,'tool',%s,%s,%s,%s,%s,%s,%s,'direct',%s,'tool_result')",
                (
                    event_id,
                    str(job_id),
                    run["thread_id"],
                    thread["latest_seq"],
                    digest(payload),
                    Jsonb(payload),
                    run["ticket_id"],
                    run["run_id"],
                    Jsonb([{"run_id": str(run["run_id"]), "role": "primary"}]),
                ),
            )
            conn.execute(
                "INSERT INTO app.mailbox(run_id,event_id,seq) VALUES (%s,%s,%s)",
                (run["run_id"], event_id, thread["latest_seq"]),
            )
            self._event(
                conn, run["run_id"], "tool_completed", job_id=str(job_id), event_id=str(event_id)
            )
            return result

    def fail_read_job(self, job_id: UUID, lease: UUID) -> None:
        with self.transaction() as conn:
            job = conn.execute(
                "UPDATE app.read_jobs SET status='failed',lease_token=NULL "
                "WHERE job_id=%s AND lease_token=%s AND status='running' RETURNING run_id",
                (job_id, lease),
            ).fetchone()
            if job is not None:
                self._event(conn, job["run_id"], "tool_failed", job_id=str(job_id))

    def record_read(
        self,
        evidence: Evidence,
        epoch: int,
        content: bytes,
        *,
        r4_origin: dict[str, Any] | None = None,
    ) -> None:
        if self.artifacts.put(content) != evidence.content_hash:
            raise DomainError("EVIDENCE_REJECTED", "Evidence does not match source bytes")
        with self.transaction() as conn:
            run = self._run(conn, evidence.run_id, epoch)
            if run["status"] != "running" or run["project_id"] != evidence.project_id:
                raise DomainError("EVIDENCE_REJECTED", "Evidence is outside the current execution")
            conn.execute(
                "INSERT INTO app.artifacts(content_hash,byte_count) VALUES (%s,%s) "
                "ON CONFLICT DO NOTHING",
                (evidence.content_hash, len(content)),
            )
            conn.execute(
                "INSERT INTO app.evidence(evidence_id,run_id,project_id,source_uri,source_version,"
                "content_hash,start_line,end_line,tool_call_id) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                tuple(evidence.model_dump().values()),
            )
            if r4_origin is not None:
                conn.execute(
                    (
                        "INSERT INTO app.r4_reads(evidence_id,role,task_id,origin) VALUES "
                        "(%s,%s,%s,%s)"
                    ),
                    (
                        evidence.evidence_id,
                        r4_origin["role"],
                        r4_origin["task_id"],
                        Jsonb(r4_origin["origin"]),
                    ),
                )
            self._event(
                conn, evidence.run_id, "evidence_read", evidence_id=str(evidence.evidence_id)
            )

    def get_evidence(self, run_id: UUID, evidence_id: UUID) -> Row | None:
        with self.transaction() as conn:
            run = self._run(conn, run_id)
            return conn.execute(
                "SELECT * FROM app.evidence WHERE evidence_id=%s AND run_id=%s AND project_id=%s",
                (evidence_id, run_id, run["project_id"]),
            ).fetchone()

    def commit_report(
        self,
        run_id: UUID,
        epoch: int,
        report: bytes,
        verdict: Verification,
        *,
        reply_kind: str = "result",
        in_reply_to_event_id: UUID | None = None,
        stored_report_hash: str | None = None,
    ) -> UUID | None:
        if stored_report_hash is None:
            content_hash = self.artifacts.put(report)
        else:
            if self.artifacts.read(stored_report_hash) != report:
                raise DomainError("ARTIFACT_MISMATCH", "Pre-stored report does not match its bytes")
            content_hash = stored_report_hash
        with self.transaction() as conn:
            run = self._run(conn, run_id, epoch)
            dedupe_key = (
                f"{run['ticket_id']}:result:r{run['input_revision']}:g{run['proposal_generation']}"
                if run["graph_version"] in DUPLEX_VERSIONS
                else f"{run['ticket_id']}:result:v1"
            )
            existing = conn.execute(
                "SELECT action_id FROM app.actions WHERE dedupe_key=%s",
                (dedupe_key,),
            ).fetchone()
            if existing:
                return UUID(str(existing["action_id"]))
            if run["status"] != "running":
                raise DomainError("INVALID_RUN_STATE", "Run cannot commit a report now")
            if run["thread_revision"] != run["input_revision"]:
                raise DomainError("STALE_INPUT", "New input requires explicit intake/replanning")
            conn.execute(
                "INSERT INTO app.artifacts(content_hash,byte_count) VALUES (%s,%s) "
                "ON CONFLICT DO NOTHING",
                (content_hash, len(report)),
            )
            conn.execute(
                "INSERT INTO app.verdicts(verdict_id,run_id,report_hash,passed,details) "
                "VALUES (%s,%s,%s,%s,%s) ON CONFLICT(run_id,report_hash) DO NOTHING",
                (uuid4(), run_id, content_hash, verdict.passed, Jsonb(verdict.model_dump())),
            )
            if not verdict.passed:
                conn.execute(
                    "UPDATE app.runs SET status='failed',reason_code='EVIDENCE_REJECTED',"
                    "report_hash=%s WHERE run_id=%s",
                    (content_hash, run_id),
                )
                self._end_ticket(conn, run, "failed")
                self._event(conn, run_id, "verification_failed", reasons=verdict.reasons)
                return None
            self.routing.require_primary(run_id, run["thread_id"])
            target = f"github:{run['external_key']}"
            grant = conn.execute(
                "SELECT * FROM app.target_write_grants WHERE target=%s FOR UPDATE", (target,)
            ).fetchone()
            if grant and grant["active"] and grant["run_id"] != run_id:
                raise DomainError("TARGET_BUSY", "Another run owns the target write grant")
            token = grant["fencing_token"] + 1 if grant else 1
            conn.execute(
                "INSERT INTO app.target_write_grants(target,run_id,execution_epoch,fencing_token) "
                "VALUES (%s,%s,%s,%s) ON CONFLICT(target) DO UPDATE SET "
                "run_id=excluded.run_id,execution_epoch=excluded.execution_epoch,"
                "fencing_token=excluded.fencing_token,active=true",
                (target, run_id, epoch, token),
            )
            action_id = uuid4()
            payload = {
                "repository": run["repository"],
                "issue_number": int(run["external_key"].rsplit(":", 1)[1]),
                "body": report.decode() + f"\n\n<!-- mgar-action:{action_id} -->",
            }
            payload_hash = digest(
                {
                    "type": "issue_comment",
                    "target": target,
                    "payload": payload,
                    "base_revision": run["thread_revision"],
                }
            )
            conn.execute(
                "INSERT INTO app.actions(action_id,run_id,action_type,target,payload,payload_hash,"
                "base_revision,execution_epoch,fencing_token,dedupe_key,status,expires_at,"
                "reply_kind,in_reply_to_event_id) "
                "VALUES (%s,%s,'issue_comment',%s,%s,%s,%s,%s,%s,%s,'proposed',now()+%s,%s,%s)",
                (
                    action_id,
                    run_id,
                    target,
                    Jsonb(payload),
                    payload_hash,
                    run["thread_revision"],
                    epoch,
                    token,
                    dedupe_key,
                    timedelta(seconds=self.settings.approval_ttl_seconds),
                    reply_kind,
                    in_reply_to_event_id,
                ),
            )
            conn.execute(
                "UPDATE app.runs SET status='waiting_approval',report_hash=%s WHERE run_id=%s",
                (content_hash, run_id),
            )
            # R1 only consumes its initial command. Later events remain durable for R2 intake.
            conn.execute(
                "UPDATE app.mailbox SET consumed_at=now() WHERE run_id=%s "
                "AND seq=(SELECT min(seq) FROM app.mailbox WHERE run_id=%s)",
                (run_id, run_id),
            )
            self._event(
                conn,
                run_id,
                "approval_requested",
                action_id=str(action_id),
                payload_hash=payload_hash,
            )
            return action_id

    def _end_ticket(self, conn: Connection, run: Row, outcome: str) -> None:
        conn.execute(
            "UPDATE app.tickets SET status='ended',outcome=%s WHERE ticket_id=%s",
            (outcome, run["ticket_id"]),
        )
        conn.execute("UPDATE app.subscriptions SET active=false WHERE run_id=%s", (run["run_id"],))
        conn.execute(
            "UPDATE app.target_write_grants SET active=false WHERE run_id=%s "
            "AND NOT EXISTS (SELECT 1 FROM app.actions WHERE run_id=%s "
            "AND status IN ('dispatching','outcome_unknown'))",
            (run["run_id"], run["run_id"]),
        )

    def get_run(self, run_id: UUID) -> Row:
        with self.transaction() as conn:
            return self._run(conn, run_id)

    def list_runs(self) -> list[Row]:
        with self.transaction() as conn:
            return conn.execute(
                "SELECT r.* FROM app.runs r JOIN app.tickets t USING(ticket_id) "
                "JOIN app.threads h USING(thread_id) WHERE h.project_id=%s "
                "ORDER BY r.created_at,r.run_id",
                (self.settings.project_id,),
            ).fetchall()

    def evidence_by_id(self, evidence_id: UUID) -> Row:
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM app.evidence WHERE evidence_id=%s AND project_id=%s",
                (evidence_id, self.settings.project_id),
            ).fetchone()
            if row is None:
                raise DomainError("NOT_FOUND", "Evidence was not found", 404)
            self.tools.check_evidence(evidence_id)
            return row

    def artifact_bytes(self, content_hash: str) -> bytes:
        with self.transaction() as conn:
            authorized = conn.execute(
                "SELECT 1 FROM app.evidence WHERE project_id=%s AND content_hash=%s UNION ALL "
                "SELECT 1 FROM app.runs r JOIN app.tickets t USING(ticket_id) "
                "JOIN app.threads h USING(thread_id) WHERE h.project_id=%s AND r.report_hash=%s",
                (self.settings.project_id, content_hash, self.settings.project_id, content_hash),
            ).fetchone()
            if authorized is None:
                raise DomainError("NOT_FOUND", "Artifact was not found in this project", 404)
            refs = conn.execute(
                "SELECT evidence_id FROM app.evidence WHERE project_id=%s AND content_hash=%s",
                (self.settings.project_id, content_hash),
            ).fetchall()
            reports = conn.execute(
                "SELECT run_id FROM app.runs WHERE report_hash=%s", (content_hash,)
            ).fetchall()
        # A hash is a locator, never a permission grant.
        for ref in refs:
            self.tools.check_evidence(ref["evidence_id"])
        for report in reports:
            self.tools.resolution(report["run_id"])
        return self.artifacts.read(content_hash)

    def events(self, run_id: UUID, after: int = 0) -> list[Row]:
        with self.transaction() as conn:
            self._run(conn, run_id)
            return conn.execute(
                "SELECT * FROM app.run_events WHERE run_id=%s AND event_id>%s ORDER BY event_id",
                (run_id, after),
            ).fetchall()

    def actions(self, run_id: UUID) -> list[Row]:
        with self.transaction() as conn:
            self._run(conn, run_id)
            self.tools.resolution(run_id)
            return conn.execute(
                "SELECT * FROM app.actions WHERE run_id=%s ORDER BY created_at", (run_id,)
            ).fetchall()

    def _action(self, conn: Connection, action_id: UUID) -> tuple[Row, Row]:
        reference = conn.execute(
            "SELECT run_id FROM app.actions WHERE action_id=%s", (action_id,)
        ).fetchone()
        if reference is None:
            raise DomainError("NOT_FOUND", "Action was not found", 404)
        run = self._run(conn, reference["run_id"])
        action = conn.execute(
            "SELECT *,expires_at>now() AS unexpired FROM app.actions WHERE action_id=%s FOR UPDATE",
            (action_id,),
        ).fetchone()
        assert action is not None
        return action, run

    def _validate_action(self, conn: Connection, action: Row, run: Row) -> None:
        if action["action_type"] == "draft_pr":
            self.repairs.validate_action(action, run)
        if run["graph_version"] == "r4-experts-v1":
            self.tools.validate_publication(run["run_id"])
        if (
            run["status"] != "waiting_approval"
            or run["execution_epoch"] != action["execution_epoch"]
        ):
            raise DomainError("FENCED", "Action execution no longer owns this run")
        if not action["unexpired"] or run["thread_revision"] != action["base_revision"]:
            raise DomainError("STALE_ACTION", "Action expired or its input version changed")
        expected = digest(
            {
                "type": action["action_type"],
                "target": action["target"],
                "payload": action["payload"],
                "base_revision": action["base_revision"],
            }
        )
        if expected != action["payload_hash"]:
            raise DomainError("PAYLOAD_CHANGED", "Action payload no longer matches its digest")
        grant = conn.execute(
            "SELECT * FROM app.target_write_grants WHERE target=%s AND active "
            "AND run_id=%s AND execution_epoch=%s AND fencing_token=%s",
            (action["target"], run["run_id"], action["execution_epoch"], action["fencing_token"]),
        ).fetchone()
        primary = conn.execute(
            "SELECT 1 FROM app.subscriptions WHERE run_id=%s AND thread_id=%s "
            "AND active AND role='primary'",
            (run["run_id"], run["thread_id"]),
        ).fetchone()
        if not grant or not primary:
            raise DomainError("WRITE_GRANT_DENIED", "Current primary write grant is required", 403)
        mandate = conn.execute(
            "SELECT scope,policy_version FROM app.mandates WHERE mandate_id=%s",
            (run["mandate_id"],),
        ).fetchone()
        assert mandate is not None
        scope = mandate["scope"]
        if (
            mandate["policy_version"] != self.settings.policy_version
            or action["action_type"] not in scope["actions"]
            or action["payload"]["repository"] != scope["repository"]
            or action["payload"]["issue_number"] != scope["issue_number"]
        ):
            raise DomainError("MANDATE_DENIED", "Action exceeds the current mandate", 403)
        if run["execution_mode"] == "live":
            used = conn.execute(
                "SELECT count(*) AS n FROM app.actions a JOIN app.runs r USING(run_id) "
                "JOIN app.tickets t USING(ticket_id) JOIN app.mandates m USING(mandate_id) "
                "WHERE m.scope->>'profile_hash'=%s "
                "AND a.status IN ('dispatching','outcome_unknown','succeeded')",
                (scope.get("profile_hash"),),
            ).fetchone()
            assert used is not None
            if used["n"] >= scope.get("model_config", {}).get("max_result_comments", 1):
                raise DomainError(
                    "LIVE_EFFECT_LIMIT", "Profile result-comment limit is exhausted", 403
                )

    def approve(self, action_id: UUID, payload_hash: str, actor: str) -> Row:
        if actor.lower() not in {a.lower() for a in self.settings.allowed_actors}:
            raise DomainError("ACTOR_DENIED", "Actor cannot approve this action", 403)
        with self.transaction() as conn:
            action, run = self._action(conn, action_id)
            if action["payload_hash"] != payload_hash:
                raise DomainError("APPROVAL_HASH_MISMATCH", "Approval is for a different payload")
            if action["status"] in {"ready", "dispatching", "succeeded", "outcome_unknown"}:
                return action
            if action["status"] != "proposed":
                raise DomainError("INVALID_ACTION_STATE", "Action is not awaiting approval")
            self._validate_action(conn, action, run)
            conn.execute(
                "INSERT INTO app.approvals(approval_id,action_id,actor,payload_hash) "
                "VALUES (%s,%s,%s,%s)",
                (uuid4(), action_id, actor, payload_hash),
            )
            conn.execute("UPDATE app.actions SET status='ready' WHERE action_id=%s", (action_id,))
            conn.execute(
                "INSERT INTO app.outbox(outbox_id,action_id,status) VALUES (%s,%s,'ready')",
                (uuid4(), action_id),
            )
            self._event(
                conn, run["run_id"], "action_approved", action_id=str(action_id), actor=actor
            )
            return {**action, "status": "ready"}

    def claim_effect(
        self, execution_mode: str, action_types: tuple[str, ...] = ("issue_comment",)
    ) -> Row | None:
        with self.transaction() as conn:
            reference = conn.execute(
                "SELECT o.action_id FROM app.outbox o JOIN app.actions a USING(action_id) "
                "JOIN app.runs r USING(run_id) WHERE o.status='ready' AND a.status='ready' "
                "AND r.execution_mode=%s AND a.action_type=ANY(%s) AND o.next_retry_at<=now() "
                "ORDER BY o.created_at FOR UPDATE OF o SKIP LOCKED LIMIT 1",
                (execution_mode, list(action_types)),
            ).fetchone()
            if reference is None:
                return None
            action, run = self._action(conn, reference["action_id"])
            try:
                self._validate_action(conn, action, run)
            except DomainError as exc:
                conn.execute(
                    "UPDATE app.actions SET status='expired',reason_code=%s WHERE action_id=%s",
                    (exc.code, action["action_id"]),
                )
                conn.execute(
                    "UPDATE app.outbox SET status='cancelled' WHERE action_id=%s",
                    (action["action_id"],),
                )
                self._event(conn, run["run_id"], "action_blocked", reason_code=exc.code)
                return None
            lease_token = uuid4()
            conn.execute(
                "UPDATE app.actions SET status='dispatching',lease_token=%s WHERE action_id=%s",
                (lease_token, action["action_id"]),
            )
            conn.execute(
                "UPDATE app.outbox SET status='dispatching',lease_token=%s,"
                "lease_until=now()+interval '5 minutes',attempts=attempts+1 WHERE action_id=%s",
                (lease_token, action["action_id"]),
            )
            self._event(
                conn, run["run_id"], "action_dispatching", action_id=str(action["action_id"])
            )
            return {**action, "lease_token": lease_token, "execution_mode": execution_mode}

    def effect_result(
        self,
        action_id: UUID,
        lease_token: UUID,
        *,
        external_id: str | None = None,
        unknown: bool = False,
        failed: bool = False,
    ) -> None:
        if sum([external_id is not None, unknown, failed]) != 1:
            raise ValueError("Exactly one effect outcome is required")
        with self.transaction() as conn:
            action, run = self._action(conn, action_id)
            if action["status"] != "dispatching" or action["lease_token"] != lease_token:
                raise DomainError("FENCED", "Effect claim is no longer current")
            status = "outcome_unknown" if unknown else "failed" if failed else "succeeded"
            conn.execute(
                "UPDATE app.actions SET status=%s,external_id=%s,lease_token=NULL "
                "WHERE action_id=%s",
                (status, external_id, action_id),
            )
            conn.execute(
                "UPDATE app.outbox SET status=%s,lease_token=NULL,lease_until=NULL "
                "WHERE action_id=%s",
                (status, action_id),
            )
            self._apply_effect_outcome(conn, run, status)
            self._event(conn, run["run_id"], "action_" + status, action_id=str(action_id))

    def _apply_effect_outcome(self, conn: Connection, run: Row, status: str) -> None:
        if status == "outcome_unknown":
            conn.execute(
                "UPDATE app.runs SET reason_code='OUTCOME_UNKNOWN' WHERE run_id=%s",
                (run["run_id"],),
            )
            return
        if run["graph_version"] == "system-reply-v1" and run["status"] not in TERMINAL:
            if run["thread_revision"] > run["input_revision"]:
                conn.execute(
                    "UPDATE app.runs SET status='queued',graph_version=%s,"
                    "reason_code='CLARIFICATION_RECEIVED',execution_epoch=execution_epoch+1,"
                    "proposal_generation=proposal_generation+1 WHERE run_id=%s",
                    (
                        "r4-experts-v1"
                        if self.settings.graph_version == "r4-experts-v1"
                        else "r2-mailbox-v1",
                        run["run_id"],
                    ),
                )
            else:
                conn.execute(
                    "UPDATE app.runs SET status='waiting_input',"
                    "reason_code='CLARIFICATION_REQUIRED' "
                    "WHERE run_id=%s",
                    (run["run_id"],),
                )
            self._event(conn, run["run_id"], "system_reply_resolved", status=status)
            return
        if run["graph_version"] in DUPLEX_VERSIONS and run["status"] not in TERMINAL:
            if run["status"] == "paused":
                self._event(conn, run["run_id"], "paused_effect_resolved", status=status)
                return
            if run["thread_revision"] > run["input_revision"]:
                conn.execute(
                    "UPDATE app.runs SET status='queued',reason_code='REPLAN_REQUIRED',"
                    "execution_epoch=execution_epoch+1,proposal_generation=proposal_generation+1 "
                    "WHERE run_id=%s",
                    (run["run_id"],),
                )
                self._event(conn, run["run_id"], "follow_up_after_effect", status=status)
                return
        if run["status"] not in TERMINAL:
            conn.execute(
                "UPDATE app.runs SET status=%s,outcome=%s,reason_code=NULL WHERE run_id=%s",
                (
                    "succeeded" if status == "succeeded" else "failed",
                    "published" if status == "succeeded" else "failed",
                    run["run_id"],
                ),
            )
            self._end_ticket(conn, run, "published" if status == "succeeded" else "failed")
        else:
            # A late remote receipt is recorded without resurrecting a cancelled Run.
            self._end_ticket(conn, run, run["outcome"] or run["status"])

    def reconcile_effect(self, action_id: UUID, external_id: str | None) -> bool:
        with self.transaction() as conn:
            action, run = self._action(conn, action_id)
            if action["status"] != "outcome_unknown":
                raise DomainError("NOT_UNKNOWN", "Only unknown effects can be reconciled")
            if external_id is None:
                # Absence in a bounded query is not proof the remote operation never happened.
                return False
            conn.execute(
                "UPDATE app.actions SET status='succeeded',external_id=%s WHERE action_id=%s",
                (external_id, action_id),
            )
            conn.execute(
                "UPDATE app.outbox SET status='succeeded' WHERE action_id=%s",
                (action_id,),
            )
            self._apply_effect_outcome(conn, run, "succeeded")
            self._event(conn, run["run_id"], "action_reconciled", action_id=str(action_id))
            return True

    def recover(self) -> dict[str, int]:
        """Startup recovery, before starting any Dispatcher or Worker."""
        with self.transaction() as conn:
            unknown = conn.execute(
                "UPDATE app.actions SET status='outcome_unknown',lease_token=NULL "
                "WHERE status='dispatching' RETURNING run_id"
            ).fetchall()
            conn.execute(
                "UPDATE app.outbox SET status='outcome_unknown',lease_token=NULL,lease_until=NULL "
                "WHERE status='dispatching'"
            )
            conn.execute(
                "UPDATE app.repair_publication_steps SET status='outcome_unknown' "
                "WHERE status='dispatching'"
            )
            for row in unknown:
                conn.execute(
                    "UPDATE app.runs SET reason_code='OUTCOME_UNKNOWN' WHERE run_id=%s",
                    (row["run_id"],),
                )
            usage = conn.execute(
                "UPDATE app.usage_calls SET status='unknown' WHERE status='reserved' "
                "RETURNING call_id"
            ).fetchall()
            paused = conn.execute(
                "UPDATE app.runs SET status='paused',execution_epoch=execution_epoch+1,"
                "reason_code='PROCESS_RESTARTED' WHERE status='running' RETURNING run_id"
            ).fetchall()
            for row in paused:
                self._event(conn, row["run_id"], "run_recovery_required")
            jobs = conn.execute(
                "UPDATE app.read_jobs j SET status='queued',lease_token=NULL "
                "FROM app.runs r WHERE j.run_id=r.run_id AND j.status='running' "
                "AND r.status NOT IN ('cancelled','failed','succeeded') RETURNING j.job_id"
            ).fetchall()
            conn.execute(
                "UPDATE app.read_jobs j SET status='cancelled',lease_token=NULL "
                "FROM app.runs r WHERE j.run_id=r.run_id "
                "AND r.status IN ('cancelled','failed','succeeded') "
                "AND j.status IN ('queued','running')"
            )
            self.routing.recover()
            self.knowledge.recover()
            self.tools.recover()
            return {
                "unknown_actions": len(unknown),
                "paused_runs": len(paused),
                "unknown_usage": len(usage),
                "requeued_read_jobs": len(jobs),
            }

    def cancel(self, run_id: UUID) -> Row:
        with self.transaction() as conn:
            run = self._run(conn, run_id)
            if run["status"] in TERMINAL:
                return run
            conn.execute(
                "UPDATE app.runs SET status='cancelled',outcome='cancelled',"
                "execution_epoch=execution_epoch+1 WHERE run_id=%s",
                (run_id,),
            )
            conn.execute(
                "UPDATE app.actions SET status='cancelled' WHERE run_id=%s "
                "AND status IN ('proposed','approved','ready')",
                (run_id,),
            )
            conn.execute(
                "UPDATE app.outbox SET status='cancelled' WHERE action_id IN "
                "(SELECT action_id FROM app.actions WHERE run_id=%s AND status='cancelled')",
                (run_id,),
            )
            conn.execute(
                "UPDATE app.read_jobs SET status='cancelled',lease_token=NULL "
                "WHERE run_id=%s AND status IN ('queued','running')",
                (run_id,),
            )
            self._end_ticket(conn, run, "cancelled")
            self._event(conn, run_id, "run_cancelled")
            return {**run, "status": "cancelled", "outcome": "cancelled"}

    def resume(self, run_id: UUID) -> UUID:
        with self.transaction() as conn:
            run = self._run(conn, run_id)
            if run["status"] != "paused" or run["ticket_status"] != "open":
                raise DomainError("NOT_RESUMABLE", "Run is not safely resumable")
            if run["graph_version"] in DUPLEX_VERSIONS:
                unresolved = conn.execute(
                    "SELECT 1 FROM app.actions WHERE run_id=%s "
                    "AND status IN ('dispatching','outcome_unknown') UNION ALL "
                    "SELECT 1 FROM app.usage_calls WHERE run_id=%s "
                    "AND (status IN ('reserved','unknown') OR result IS NULL)",
                    (run_id, run_id),
                ).fetchone()
                if unresolved:
                    raise DomainError("RECOVERY_BLOCKED", "Unresolved calls or effects need review")
                completed = conn.execute(
                    "SELECT 1 FROM app.actions WHERE run_id=%s AND status='succeeded' "
                    "AND base_revision=%s",
                    (run_id, run["thread_revision"]),
                ).fetchone()
                if completed:
                    conn.execute(
                        "UPDATE app.runs SET status='succeeded',outcome='published',"
                        "reason_code=NULL WHERE run_id=%s",
                        (run_id,),
                    )
                    self._end_ticket(conn, run, "published")
                else:
                    conn.execute(
                        "UPDATE app.runs SET status='queued',"
                        "reason_code='RESUME_FROM_CHECKPOINT' WHERE run_id=%s",
                        (run_id,),
                    )
                    self._event(conn, run_id, "run_checkpoint_resume")
                return run_id
            pending = conn.execute(
                "SELECT 1 FROM app.actions WHERE run_id=%s UNION ALL "
                "SELECT 1 FROM app.usage_calls WHERE run_id=%s "
                "AND status IN ('reserved','unknown')",
                (run_id, run_id),
            ).fetchone()
            if pending:
                raise DomainError("RECOVERY_BLOCKED", "Unresolved effects require reconciliation")
            known = conn.execute(
                "SELECT result FROM app.usage_calls WHERE run_id=%s AND status='known'", (run_id,)
            ).fetchone()
            if known is not None:
                if known["result"] is None:
                    raise DomainError(
                        "RECOVERY_BLOCKED", "Known model failure requires a reviewed attempt"
                    )
                conn.execute(
                    "UPDATE app.runs SET status='queued',reason_code='RESUME_FROM_CHECKPOINT' "
                    "WHERE run_id=%s",
                    (run_id,),
                )
                self._event(conn, run_id, "run_checkpoint_resume")
                return run_id
            replacement = uuid4()
            conn.execute(
                "INSERT INTO app.runs(run_id,ticket_id,attempt_no,graph_version,execution_mode,"
                "status,question,input_revision) VALUES (%s,%s,%s,%s,%s,'queued',%s,%s)",
                (
                    replacement,
                    run["ticket_id"],
                    run["attempt_no"] + 1,
                    run["graph_version"],
                    run["execution_mode"],
                    run["question"],
                    run["input_revision"],
                ),
            )
            conn.execute(
                "UPDATE app.runs SET status='failed',reason_code='REPLACED_BY_NEW_ATTEMPT' "
                "WHERE run_id=%s",
                (run_id,),
            )
            conn.execute("UPDATE app.subscriptions SET active=false WHERE run_id=%s", (run_id,))
            conn.execute(
                "INSERT INTO app.subscriptions(thread_id,run_id,role) VALUES (%s,%s,'primary')",
                (run["thread_id"], replacement),
            )
            conn.execute(
                "INSERT INTO app.mailbox(run_id,event_id,seq) SELECT %s,event_id,seq "
                "FROM app.mailbox WHERE run_id=%s AND consumed_at IS NULL",
                (replacement, run_id),
            )
            self._event(conn, replacement, "run_replacement_created", previous_run_id=str(run_id))
            return replacement

    def fail_run(self, run_id: UUID, reason_code: str, epoch: int | None = None) -> None:
        with self.transaction() as conn:
            run = self._run(conn, run_id)
            if run["status"] in TERMINAL | {"waiting_approval", "paused"} or (
                epoch is not None and run["execution_epoch"] != epoch
            ):
                return
            state = (
                "paused"
                if reason_code
                in {
                    "GRAPH_VERSION_UNSUPPORTED",
                    "R4_LIVE_PROFILE_REQUIRED",
                    "R4_SOURCE_PROFILE_CHANGED",
                }
                else "waiting_input"
                if reason_code
                in {"STALE_INPUT", "REPLAN_LIMIT", "CONTEXT_LIMIT", "MAILBOX_EXPIRED"}
                else "failed"
            )
            conn.execute(
                "UPDATE app.runs SET status=%s,reason_code=%s,execution_epoch=execution_epoch+1 "
                "WHERE run_id=%s",
                (state, reason_code, run_id),
            )
            if state == "failed":
                self._end_ticket(conn, run, "failed")
            self._event(conn, run_id, "run_" + state, reason_code=reason_code)

    def reserve_call(self, run_id: UUID, epoch: int, step_key: str, permit: LivePermit) -> UUID:
        permit.validate()
        if not self.settings.allow_paid:
            raise DomainError("PAID_NOT_AUTHORIZED", "Server has not enabled paid execution", 403)
        profile = permit.profile
        amount = profile.reservation_micros
        with self.transaction() as conn:
            run = self._run(conn, run_id, epoch)
            scope = conn.execute(
                "SELECT scope FROM app.mandates WHERE mandate_id=%s", (run["mandate_id"],)
            ).fetchone()
            assert scope is not None
            if (
                run["execution_mode"] != "live"
                or run["status"] != "running"
                or scope["scope"].get("profile_hash") != profile.profile_hash
            ):
                raise DomainError(
                    "MODEL_SCOPE_DENIED", "Model call differs from the run mandate", 403
                )
            if conn.execute(
                "SELECT 1 FROM app.usage_calls WHERE run_id=%s AND step_key=%s", (run_id, step_key)
            ).fetchone():
                raise DomainError("MODEL_CALL_ALREADY_RECORDED", "Model step cannot be resent")
            trial_budget_hash = self.trials.reserve(conn, profile.profile_hash, amount)
            reserved = conn.execute(
                "UPDATE app.authorization_budgets SET reserved_micros=reserved_micros+%s,"
                "used_calls=used_calls+1 WHERE profile_hash=%s AND used_calls<call_limit "
                "AND reserved_micros+settled_micros+%s<=limit_micros "
                "RETURNING profile_hash",
                (amount, profile.profile_hash, amount),
            ).fetchone()
            local = conn.execute(
                "UPDATE app.budgets SET reserved_micros=reserved_micros+%s WHERE mandate_id=%s "
                "AND reserved_micros+settled_micros+%s<=limit_micros RETURNING mandate_id",
                (amount, run["mandate_id"], amount),
            ).fetchone()
            if reserved is None or local is None:
                raise DomainError(
                    "BUDGET_EXHAUSTED", "Model call exceeds its remaining budget", 403
                )
            call_id = uuid4()
            conn.execute(
                "INSERT INTO app.usage_calls(call_id,run_id,step_key,status,reserved_micros,"
                "profile_hash,trial_budget_hash) VALUES (%s,%s,%s,'reserved',%s,%s,%s)",
                (call_id, run_id, step_key, amount, profile.profile_hash, trial_budget_hash),
            )
            self._event(
                conn, run_id, "model_reserved", call_id=str(call_id), reserved_micros=amount
            )
            return call_id

    def model_result(
        self, run_id: UUID, epoch: int, step_key: str, permit: LivePermit
    ) -> Investigation | None:
        return self.typed_model_result(run_id, epoch, step_key, permit, Investigation)

    def typed_model_result(
        self, run_id: UUID, epoch: int, step_key: str, permit: LivePermit, result_type: type[TModel]
    ) -> TModel | None:
        with self.transaction() as conn:
            run = self._run(conn, run_id, epoch)
            if run["status"] != "running":
                raise DomainError("FENCED", "Model step no longer owns a running execution")
            call = conn.execute(
                "SELECT * FROM app.usage_calls WHERE run_id=%s AND step_key=%s",
                (run_id, step_key),
            ).fetchone()
            if call is None:
                return None
            if call["profile_hash"] != permit.profile.profile_hash:
                raise DomainError("MODEL_SCOPE_DENIED", "Recorded call belongs to another profile")
            if call["status"] != "known":
                raise DomainError("USAGE_UNKNOWN", "Recorded model call cannot be resent")
            if call["result"] is None:
                raise DomainError(
                    "MODEL_RESULT_UNAVAILABLE", "Known failed model step cannot be resent"
                )
            return result_type.model_validate(call["result"])

    def record_model_input(self, run_id: UUID, epoch: int, step: str, body: Row) -> None:
        import json

        encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
        content_hash = self.artifacts.put(encoded)
        with self.transaction() as conn:
            run = self._run(conn, run_id, epoch)
            if run["status"] != "running":
                raise DomainError("FENCED", "Model input belongs to a stopped execution")
            conn.execute(
                (
                    "INSERT INTO app.artifacts(content_hash,byte_count) VALUES "
                    "(%s,%s) ON CONFLICT DO NOTHING"
                ),
                (content_hash, len(encoded)),
            )
            self._event(
                conn, run_id, "model_input_recorded", content_hash=content_hash, step_key=step
            )

    def record_model_response(self, call_id: UUID, status: int, body: bytes) -> None:
        if len(body) > 262144:
            raise DomainError("MODEL_RESPONSE_LIMIT", "Provider response exceeds its byte bound")
        content_hash = self.artifacts.put(body)
        with self.transaction() as conn:
            call = conn.execute(
                "SELECT run_id FROM app.usage_calls WHERE call_id=%s", (call_id,)
            ).fetchone()
            if call is None:
                raise DomainError("CALL_NOT_FOUND", "Model call was not found", 404)
            self._run(conn, call["run_id"])
            conn.execute(
                "INSERT INTO app.artifacts(content_hash,byte_count) VALUES (%s,%s) "
                "ON CONFLICT DO NOTHING",
                (content_hash, len(body)),
            )
            self._event(
                conn,
                call["run_id"],
                "model_response_recorded",
                call_id=str(call_id),
                http_status=status,
                content_hash=content_hash,
            )

    def settle_call(
        self,
        call_id: UUID,
        cost_micros: int | None,
        usage: Row | None = None,
        result: Model | None = None,
    ) -> None:
        if cost_micros is not None and (type(cost_micros) is not int or cost_micros < 0):
            raise ValueError("Known cost must be a nonnegative integer")
        with self.transaction() as conn:
            call = conn.execute(
                "SELECT * FROM app.usage_calls WHERE call_id=%s FOR UPDATE", (call_id,)
            ).fetchone()
            if call is None:
                raise DomainError("CALL_NOT_FOUND", "Model call was not found", 404)
            if call["status"] == "known":
                if call["settled_micros"] != cost_micros:
                    raise DomainError(
                        "SETTLEMENT_CONFLICT", "Call already has a different settlement"
                    )
                return
            run = self._run(conn, call["run_id"])
            if cost_micros is None or cost_micros > call["reserved_micros"]:
                conn.execute(
                    "UPDATE app.usage_calls SET status='unknown' WHERE call_id=%s", (call_id,)
                )
                conn.execute(
                    "UPDATE app.runs SET reason_code='USAGE_UNKNOWN' WHERE run_id=%s",
                    (run["run_id"],),
                )
                self._event(conn, run["run_id"], "model_usage_unknown", call_id=str(call_id))
                return
            ledgers = [
                ("authorization_budgets", "profile_hash", call["profile_hash"]),
                ("budgets", "mandate_id", run["mandate_id"]),
            ]
            if call["trial_budget_hash"] is not None:
                ledgers.append(("authorization_budgets", "profile_hash", call["trial_budget_hash"]))
            for table, key, value in ledgers:
                conn.execute(
                    f"UPDATE app.{table} SET reserved_micros=reserved_micros-%s,"
                    f"settled_micros=settled_micros+%s WHERE {key}=%s",
                    (call["reserved_micros"], cost_micros, value),
                )
            conn.execute(
                "UPDATE app.usage_calls SET status='known',settled_micros=%s,usage=%s,result=%s "
                "WHERE call_id=%s",
                (
                    cost_micros,
                    Jsonb(usage or {}),
                    Jsonb(result.model_dump(mode="json")) if result else None,
                    call_id,
                ),
            )
            self._event(
                conn,
                run["run_id"],
                "model_settled",
                call_id=str(call_id),
                settled_micros=cost_micros,
            )
