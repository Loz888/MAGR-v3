"Run-local checkpoints; Engine alone records candidates, decisions and write intents."

import hashlib
from typing import TYPE_CHECKING, Any, Protocol, TypedDict, cast
from uuid import UUID

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from mgar_v3.coordination.engine import Engine
from mgar_v3.domain import DomainError
from mgar_v3.graph.checkpoints import checkpoint_saver
from mgar_v3.repair.domain import PatchReply, Snapshot
from mgar_v3.repair.hidden import suite
from mgar_v3.repair.sandbox import Sandbox
from mgar_v3.repair.source import working_files
from mgar_v3.repair.workspace import apply_diff, fixture_diff, recount_diff

if TYPE_CHECKING:
    from mgar_v3.graph.runner import Runner


class PatchProvider(Protocol):
    mode: str

    def propose(
        self, snapshot: Snapshot, question: str, attempt: int, feedback: str
    ) -> PatchReply: ...


class FixturePatcher:
    mode = "fixture"

    def propose(self, snapshot: Snapshot, question: str, attempt: int, feedback: str) -> PatchReply:
        return PatchReply(
            diff=fixture_diff(snapshot, attempt),
            explanation="Controlled engineering fixture; no model call.",
        )


class RepairState(TypedDict, total=False):
    input_revision: int
    question: str
    action_id: str
    reason: str


def config(run_id: UUID, revision: int, generation: int) -> RunnableConfig:
    return {"configurable": {"thread_id": f"{run_id}:repair:{revision}:{generation}"}}


def builder(
    engine: Engine, run_id: UUID, epoch: int, selected: PatchProvider
) -> StateGraph[RepairState]:
    store = engine.repairs

    def intake(state: RepairState) -> RepairState:
        value = engine.prepare_intake(run_id, epoch)
        value = engine.acknowledge_intake(run_id, epoch, value["snapshot_id"])
        return {"input_revision": value["input_revision"], "question": value["question"]}

    def repair(state: RepairState) -> RepairState:
        revision = state["input_revision"]
        snapshot = store.snapshot(run_id)
        box = Sandbox(str(run_id))
        # Recovery only removes containers owned by this Run. No host path is exposed to candidates.
        box.cleanup()

        def test(key: str, files: dict[str, str], expected: list[str]) -> dict[str, Any]:
            previous = store.step(run_id, epoch, revision, key)
            if previous:
                return previous
            remaining = store.remaining(run_id, epoch)
            if remaining <= 0:
                raise DomainError("REPAIR_DEADLINE", "Repair deadline exhausted")
            proof, raw = box.run(files, expected, timeout=min(30.0, remaining))
            proof["log_hash"] = store.artifact(raw)
            return store.finish(run_id, epoch, revision, key, proof)

        baseline = test(
            "baseline", working_files(snapshot, snapshot.files), snapshot.expected_tests
        )
        if (
            baseline["passed"]
            or baseline["stop_reason"] is not None
            or baseline["exit_code"] != 1
            or not set(snapshot.expected_tests) <= set(baseline.get("failed_tests", []))
        ):
            store.waiting(run_id, epoch, "BASELINE_NOT_REPRODUCED")
            return {"reason": "BASELINE_NOT_REPRODUCED"}
        with engine.transaction() as conn:
            manual = conn.execute(
                (
                    "SELECT diff_hash FROM app.repair_manual_inputs WHERE run_id=%s "
                    "AND input_revision=%s"
                ),
                (run_id, revision),
            ).fetchone()
        feedback = "The original public test fails. Return a bounded unified diff only."
        for attempt in range(1 if manual else 3):
            if store.remaining(run_id, epoch) <= 0:
                break
            key = f"patch:{attempt}"
            proposal = store.step(run_id, epoch, revision, key)
            if proposal is None:
                # The provider receives public source and feedback only.
                try:
                    reply = (
                        PatchReply(
                            diff=engine.artifacts.read(manual["diff_hash"]).decode(),
                            explanation="User edit",
                        )
                        if manual
                        else selected.propose(snapshot, state["question"], attempt, feedback)
                    )
                except DomainError as exc:
                    if exc.code not in {"MODEL_INVALID_RESPONSE", "MODEL_RESULT_UNAVAILABLE"}:
                        raise
                    proposal = store.finish(run_id, epoch, revision, key, {"error": exc.code})
                    feedback = "Previous model response failed the required JSON/diff schema."
                    continue
                proposal = store.finish(run_id, epoch, revision, key, reply.model_dump(mode="json"))
            if proposal.get("error"):
                feedback = "Previous model response failed the required JSON/diff schema."
                continue
            try:
                candidate_diff = recount_diff(proposal["diff"])
                files = apply_diff(snapshot, candidate_diff)
            except DomainError as exc:
                feedback = "Patch rejected by the source policy: " + exc.code + ": " + str(exc)
                store.finish(
                    run_id, epoch, revision, f"validation:{attempt}", {"reason": exc.code}
                ) if store.step(run_id, epoch, revision, f"validation:{attempt}") is None else None
                continue
            if candidate_diff != proposal["diff"]:
                key = f"normalization:{attempt}"
                if store.step(run_id, epoch, revision, key) is None:
                    store.finish(
                        run_id,
                        epoch,
                        revision,
                        key,
                        {
                            "algorithm": "hunk_counts_v1",
                            "raw_diff_hash": hashlib.sha256(proposal["diff"].encode()).hexdigest(),
                            "normalized_diff_hash": hashlib.sha256(
                                candidate_diff.encode()
                            ).hexdigest(),
                        },
                    )
            work = working_files(snapshot, files)
            public = test(f"public:{attempt}", work, snapshot.expected_tests)
            hidden = test(
                f"hidden:{attempt}",
                {**work, "zz_mgar_hidden_test.go": suite()},
                [*snapshot.expected_tests, "TestR5Hidden"],
            )
            candidate = store.candidate(
                run_id, epoch, revision, attempt, candidate_diff, files, public, hidden
            )
            if candidate["verified"]:
                try:
                    return {"action_id": str(store.propose(run_id, epoch, revision, candidate))}
                except DomainError as exc:
                    if exc.code != "STALE_INPUT":
                        raise
                    engine.requeue_input(run_id, epoch)
                    return {"reason": "STALE_INPUT"}
            feedback = (
                "Public tests: "
                + ("passed" if public["passed"] else "failed")
                + ". Independent verification: "
                + ("passed" if hidden["passed"] else "failed")
                + "."
            )
        store.waiting(run_id, epoch, "REPAIR_EXHAUSTED")
        return {"reason": "REPAIR_EXHAUSTED"}

    def approval(state: RepairState) -> RepairState:
        decision = interrupt(
            {
                "action_id": state["action_id"],
                "review": "Review the complete diff, base and protected test receipts.",
            }
        )
        with engine.transaction() as conn:
            action, current = engine._action(conn, UUID(state["action_id"]))
            if (
                not isinstance(decision, dict)
                or decision.get("payload_hash") != action["payload_hash"]
                or action["status"] not in {"ready", "dispatching", "succeeded", "outcome_unknown"}
            ):
                raise DomainError(
                    "APPROVAL_REQUIRED", "Checkpoint input cannot approve a business action"
                )
            if current["run_id"] != run_id:
                raise DomainError("FENCED", "Approval Run differs")
        return {}

    graph = StateGraph(RepairState)
    graph.add_node("intake", intake)
    graph.add_node("repair", repair)
    graph.add_node("approval", approval)
    graph.add_edge(START, "intake")
    graph.add_edge("intake", "repair")
    graph.add_conditional_edges(
        "repair",
        lambda state: "approval" if state.get("action_id") else "end",
        {"approval": "approval", "end": END},
    )
    graph.add_edge("approval", END)
    return graph


def execute_repair(
    owner: "Runner", run_id: UUID, provider: PatchProvider | None = None
) -> UUID | None:
    engine = owner.engine
    run = engine.begin_run(run_id)
    epoch = run["execution_epoch"]
    owner.execution_epoch = epoch
    selected = provider or FixturePatcher()
    if run["execution_mode"] == "live" and provider is None:
        from mgar_v3.repair.live import DeepSeekPatcher, repair_permit

        permit = repair_permit(engine.settings)
        with engine.transaction() as conn:
            mandate = conn.execute(
                "SELECT scope FROM app.mandates WHERE mandate_id=%s", (run["mandate_id"],)
            ).fetchone()
        if not mandate or mandate["scope"].get("profile_hash") != permit.profile.profile_hash:
            raise DomainError(
                "R5_SCOPE_CHANGED", "Run belongs to another reviewed repair profile", 403
            )
        selected = DeepSeekPatcher(engine, run_id, epoch, permit)
    if run["execution_mode"] != selected.mode:
        raise DomainError(
            "R5_PROFILE_REQUIRED",
            "R5 live needs its own reviewed source, budget and write authorization",
            403,
        )
    with checkpoint_saver(engine.settings.database_url.get_secret_value()) as saver:
        graph = builder(engine, run_id, epoch, selected).compile(checkpointer=saver)
        cfg = config(run_id, run["thread_revision"], run["proposal_generation"])
        old = graph.get_state(cfg)
        state = cast(
            RepairState,
            graph.invoke(
                None if old.next else {"question": run["question"]}, cfg, durability="sync"
            ),
        )
    return UUID(state["action_id"]) if state.get("action_id") else None


def acknowledge_approval(engine: Engine, run_id: UUID, payload_hash: str) -> None:
    run = engine.get_run(run_id)
    if run["graph_version"] != "r5-repair-v1":
        return
    with checkpoint_saver(engine.settings.database_url.get_secret_value()) as saver:
        graph = builder(engine, run_id, run["execution_epoch"], FixturePatcher()).compile(
            checkpointer=saver
        )
        cfg = config(run_id, run["thread_revision"], run["proposal_generation"])
        if graph.get_state(cfg).interrupts:
            graph.invoke(Command(resume={"payload_hash": payload_hash}), cfg, durability="sync")
