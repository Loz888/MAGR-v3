import json
import os
import signal
import time
from concurrent.futures import Future, ThreadPoolExecutor
from threading import Event as ThreadEvent
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from mgar_v3.coordination_eval.domain import ROUTER_SYSTEM, Event, Plan, Route
from mgar_v3.coordination_eval.provider import FixedInvestigator, Slots, SourceCache
from mgar_v3.coordination_eval.store import DATA, Journal
from mgar_v3.domain import DomainError
from mgar_v3.graph.runner import Runner
from mgar_v3.models.structured import StructuredModel


class Coordinator:
    def __init__(self, journal: Journal, slots: Slots, *, resume: bool = False):
        self.journal, self.slots, self.engine = journal, slots, journal.engine
        self.resume = resume
        self.cache = SourceCache(journal)
        self.release = ThreadEvent()
        self.workers: dict[UUID, Future[UUID | None]] = {}
        self.pool = ThreadPoolExecutor(max_workers=2)

    def start_worker(self, run_id: UUID, event_key: str) -> None:
        if run_id in self.workers:
            return
        entered = ThreadEvent()
        investigator = FixedInvestigator(
            self.slots, "worker:" + event_key, run_id, self.cache, entered, self.release
        )
        self.workers[run_id] = self.pool.submit(Runner(self.engine, investigator).execute, run_id)
        if not entered.wait(15):
            self.workers[run_id].result(timeout=1)
            raise TimeoutError("Worker did not enter its actual source-read boundary")

    def decision(self, events: list[Event]) -> tuple[Plan, list[dict[str, Any]]]:
        keys = [e.key for e in events]
        with self.engine.transaction() as conn:
            prior = conn.execute(
                "SELECT * FROM coordination_eval.batches WHERE trial_id=%s AND event_keys=%s",
                (self.journal.id, Jsonb(keys)),
            ).fetchone()
            if prior:
                ordinal, snapshot = prior["ordinal"], prior["snapshot"]
            else:
                count = conn.execute(
                    "SELECT count(*) AS n FROM coordination_eval.batches WHERE trial_id=%s",
                    (self.journal.id,),
                ).fetchone()
                assert count is not None
                ordinal = count["n"]
                snapshot = {
                    "events": [e.model_dump(exclude={"phase"}, mode="json") for e in events],
                    "primary_runs": json.loads(json.dumps(self.journal.targets(), default=str)),
                }
                conn.execute(
                    "INSERT INTO coordination_eval.batches(trial_id,ordinal,event_keys,snapshot) "
                    "VALUES (%s,%s,%s,%s)",
                    (self.journal.id, ordinal, Jsonb(keys), Jsonb(snapshot)),
                )
        if prior and prior["plan"]:
            return Plan.model_validate(prior["plan"]), snapshot["primary_runs"]
        key = f"router:{ordinal}"
        run_id = self.slots.control_run(key)
        run = self.engine.get_run(run_id)
        replay = run["status"] != "queued"
        if replay:
            if not self.resume:
                raise DomainError("COORD_CONTINUATION_REQUIRED", "Review the interrupted trial")
            with self.engine.transaction() as conn:
                known = conn.execute(
                    "SELECT * FROM app.usage_calls WHERE run_id=%s AND step_key='coord-route:v1'",
                    (run_id,),
                ).fetchone()
            if not known or known["status"] != "known" or not known["result"]:
                raise DomainError("USAGE_UNKNOWN", "Only a known result can resume without resend")
            old_epoch = run["execution_epoch"]
            if run["status"] == "running":
                self.engine.pause(run_id)
            self.engine.resume(run_id)
            try:
                self.engine.recovery.heartbeat(run_id, old_epoch)
            except DomainError as exc:
                if exc.code != "FENCED":
                    raise
                self.journal.audit("old_epoch_fenced", run_id=str(run_id), old_epoch=old_epoch)
            else:
                raise AssertionError("Old Router epoch survived explicit recovery")
        run = self.engine.begin_run(run_id)
        started = time.perf_counter()
        with self.engine.transaction() as conn:
            before_calls = conn.execute(
                "SELECT count(*) AS n FROM app.usage_calls WHERE run_id=%s", (run_id,)
            ).fetchone()
            assert before_calls is not None
        plan = StructuredModel(self.engine, self.slots.permit(key), self.slots.client).complete(
            run_id, run["execution_epoch"], "coord-route:v1", ROUTER_SYSTEM, snapshot, Plan
        )
        elapsed = time.perf_counter() - started
        if len(plan.routes) != len(keys) or {r.event_key for r in plan.routes} != set(keys):
            raise DomainError("COORD_PLAN_INCOMPLETE", "One decision per frozen event is required")
        # Intentional whole-process cut after a real settled response, before applying events.
        if self.journal.trial.get("fault_after_known") and ordinal == 0 and not self.resume:
            self.journal.audit(
                "known_router_killpoint", run_id=str(run_id), epoch=run["execution_epoch"]
            )
            os.kill(os.getpid(), signal.SIGKILL)
        with self.engine.transaction() as conn:
            conn.execute(
                "UPDATE coordination_eval.batches SET control_run_id=%s,plan=%s,"
                "completed_at=clock_timestamp(),duration_seconds=%s,replay_seconds=%s "
                "WHERE trial_id=%s AND ordinal=%s",
                (
                    run_id,
                    Jsonb(plan.model_dump(mode="json")),
                    elapsed,
                    elapsed if replay else None,
                    self.journal.id,
                    ordinal,
                ),
            )
        self.slots.finish_control(run_id, run["execution_epoch"], plan.model_dump(mode="json"))
        if replay:
            with self.engine.transaction() as conn:
                after_calls = conn.execute(
                    "SELECT count(*) AS n FROM app.usage_calls WHERE run_id=%s", (run_id,)
                ).fetchone()
                assert after_calls is not None and after_calls["n"] == before_calls["n"]
            self.journal.audit("known_router_reused", run_id=str(run_id), new_model_calls=0)
        return plan, snapshot["primary_runs"]

    def apply(self, event: Event, route: Route, expected: dict[str, Any] | None = None) -> None:
        initial = event.key in {"e01", "e02"}
        worker_key = "worker:" + ("e01" if event.issue == 0 else "e02")
        permit = self.slots.permit(worker_key) if event.issue in {0, 1} else None
        try:
            run_id = self.journal.apply(
                event,
                route,
                permit,
                self.slots.hash,
                self.slots.item(worker_key)["case_id"] if initial else None,
                expected,
            )
        except DomainError as exc:
            if exc.code in {"USAGE_UNKNOWN", "BUDGET_EXHAUSTED"}:
                raise
            self.journal.finish(event.key, "failed", reason=exc.code)
            return
        if route.action == "start" and run_id is not None:
            self.start_worker(run_id, event.key)

    def process(self, events: list[Event]) -> None:
        arm = self.journal.trial["arm"]
        remaining = []
        for event in events:
            if arm == "F" and (target := self.journal.target(event)):
                self.apply(
                    event,
                    Route(event_key=event.key, action="deliver", target_run_id=target["run_id"]),
                )
                self.journal.audit("subscription_direct", key=event.key)
            elif (
                arm == "F"
                and event.issue in {0, 1}
                and not event.body.startswith("/mgar investigate ")
            ):
                # A failed cold start does not authorize unlimited replacement model calls.
                self.journal.finish(event.key, "failed", reason="INITIAL_ROUTE_UNAVAILABLE")
            elif arm == "D":
                # This operator table is intentionally unavailable to E/F's model path.
                operator = json.loads((DATA / "operator.json").read_text())
                target = self.journal.target(event)
                self.apply(
                    event,
                    Route(
                        event_key=event.key,
                        action=operator[event.key],
                        target_run_id=target["run_id"]
                        if operator[event.key] == "deliver" and target
                        else None,
                    ),
                )
                self.journal.audit("manual_operator", key=event.key)
            else:
                remaining.append(event)
        batches = [[e] for e in remaining] if arm == "E" else [remaining] if remaining else []
        for batch in batches:
            try:
                plan, targets = self.decision(batch)
            except DomainError as exc:
                if exc.code in {"USAGE_UNKNOWN", "BUDGET_EXHAUSTED"}:
                    raise
                for event in batch:
                    self.journal.finish(event.key, "failed", reason=exc.code)
                self.journal.audit(
                    "known_router_failure", keys=[e.key for e in batch], code=exc.code
                )
                continue
            for event in batch:
                route = next(r for r in plan.routes if r.event_key == event.key)
                expected = next(
                    (t for t in targets if str(t["run_id"]) == str(route.target_run_id)), None
                )
                self.apply(event, route, expected)

    def execute(self) -> dict[str, Any]:
        if not self.resume:
            self.cache.prepare(self.journal.trial["cache"] == "warm")
        started = time.perf_counter()
        events = [
            Event.model_validate(e)
            for e in json.loads((DATA / "events.json").read_text())["events"]
        ]
        try:
            for phase in range(6):
                pending = []
                for position, event in enumerate(events):
                    if event.phase != phase:
                        continue
                    row, duplicate = self.journal.accept(event, position)
                    if duplicate or row["action"] is not None:
                        continue
                    if event.actor == "outsider":
                        self.journal.deny_actor(event)
                    else:
                        pending.append(event)
                self.process(pending)
                if phase == 2:
                    self.release.set()
            for run_id, future in self.workers.items():
                try:
                    future.result(timeout=120)
                except DomainError as exc:
                    self.engine.fail_run(run_id, exc.code)
                    self.journal.audit("worker_failure", run_id=str(run_id), code=exc.code)
                    if exc.code == "USAGE_UNKNOWN":
                        raise
            self.journal.audit("trial_complete", seconds=time.perf_counter() - started)
            return self.journal.export()
        finally:
            self.release.set()
            self.pool.shutdown(wait=True, cancel_futures=True)
