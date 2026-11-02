from typing import TYPE_CHECKING, Any, Literal, TypedDict, cast
from uuid import UUID

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph

from mgar_v3.domain import DomainError, Evidence, Investigation, Verification
from mgar_v3.evidence.verifier import verify
from mgar_v3.graph.checkpoints import checkpoint_saver
from mgar_v3.graph.read_jobs import ReadJobs

if TYPE_CHECKING:
    from mgar_v3.graph.runner import Runner


class DuplexState(TypedDict, total=False):
    question: str
    snapshot_id: str
    cursor: int
    input_revision: int
    evidence_id: str
    content_hash: str
    investigation: dict[str, Any]
    finding_revision: int
    verification: dict[str, Any]
    verification_revision: int
    report: str


def execute_duplex(owner: "Runner", run_id: UUID) -> UUID | None:
    engine, investigator = owner.engine, owner.investigator
    run = engine.begin_run(run_id)
    epoch = run["execution_epoch"]
    owner.execution_epoch = epoch
    investigator.execution_epoch = epoch
    if run["execution_mode"] != investigator.mode:
        raise DomainError("EXECUTION_MODE_MISMATCH", "Investigator and Run modes differ")

    def intake(state: DuplexState) -> DuplexState:
        snapshot = engine.prepare_intake(run_id, epoch)
        return {
            "question": snapshot["question"],
            "snapshot_id": str(snapshot["snapshot_id"]),
            "cursor": snapshot["cursor"],
            "input_revision": snapshot["input_revision"],
        }

    def replan(state: DuplexState) -> DuplexState:
        snapshot = engine.acknowledge_intake(run_id, epoch, UUID(state["snapshot_id"]))
        return {
            "snapshot_id": str(snapshot["snapshot_id"]),
            "cursor": snapshot["cursor"],
            "input_revision": snapshot["input_revision"],
            "question": snapshot["question"],
        }

    def next_step(state: DuplexState) -> Literal["read", "investigate", "verify", "finish"]:
        if "evidence_id" not in state:
            return "read"
        if state.get("finding_revision") != state["input_revision"]:
            return "investigate"
        if state.get("verification_revision") != state["input_revision"]:
            return "verify"
        return "finish"

    def read_source(state: DuplexState) -> DuplexState:
        job = ReadJobs(engine).execute(run_id, epoch, investigator.read)
        content = engine.artifacts.read(job["content_hash"])
        # A re-entered node reuses the job's stable Evidence/tool-call identity.
        evidence_id = UUID(str(job["job_id"]))
        if engine.get_evidence(run_id, evidence_id) is None:
            engine.record_read(
                Evidence(
                    evidence_id=evidence_id,
                    run_id=run_id,
                    project_id=run["project_id"],
                    source_uri=job["source_uri"],
                    source_version=job["source_version"],
                    content_hash=job["content_hash"],
                    start_line=1,
                    end_line=len(content.decode().splitlines()),
                    tool_call_id=evidence_id,
                ),
                epoch,
                content,
            )
        return {"evidence_id": str(evidence_id), "content_hash": job["content_hash"]}

    def investigate(state: DuplexState) -> DuplexState:
        investigator.model_step_key = f"investigate:r{state['input_revision']}"
        finding = investigator.investigate(
            state["question"],
            engine.artifacts.read(state["content_hash"]).decode(),
            UUID(state["evidence_id"]),
        )
        return {
            "investigation": finding.model_dump(mode="json"),
            "finding_revision": state["input_revision"],
        }

    def verify_result(state: DuplexState) -> DuplexState:
        finding = Investigation.model_validate(state["investigation"])
        verdict = verify(engine, run_id, finding)
        lines = [
            "# MGAR investigation",
            "",
            f"Execution mode: {run['execution_mode']}",
            "",
            f"Input revision: {state['input_revision']}",
            "",
            "Mechanical evidence verification: " + ("PASS" if verdict.passed else "FAIL"),
            "",
            "## Source observations",
        ]
        for claim in finding.claims:
            lines.extend(["", f"- {claim.statement} [evidence:{claim.evidence_id}]"])
        lines.extend(
            [
                "",
                "## Open questions",
                *[f"- {q}" for q in finding.open_questions],
                "",
                "Verifier scope: source reads, hashes, locators and exact quotations.",
            ]
        )
        return {
            "verification": verdict.model_dump(mode="json"),
            "verification_revision": state["input_revision"],
            "report": "\n".join(lines),
        }

    builder = StateGraph(DuplexState)
    builder.add_node("intake", intake)
    builder.add_node("replan", replan)
    builder.add_node("read", read_source)
    builder.add_node("investigate", investigate)
    builder.add_node("verify", verify_result)
    builder.add_edge(START, "intake")
    builder.add_edge("intake", "replan")
    builder.add_conditional_edges(
        "replan",
        next_step,
        {
            "read": "read",
            "investigate": "investigate",
            "verify": "verify",
            "finish": END,
        },
    )
    for step in ["read", "investigate", "verify"]:
        builder.add_edge(step, "intake")
    with checkpoint_saver(engine.settings.database_url.get_secret_value()) as saver:
        graph = builder.compile(checkpointer=saver)
        config: RunnableConfig = {"configurable": {"thread_id": str(run_id)}, "recursion_limit": 64}
        checkpoint = saver.get_tuple(config)
        snapshot = graph.get_state(config) if checkpoint is not None else None
        # An unfinished node re-enters from its checkpoint. An ended graph starts at intake
        # with its previous values, so new business input cannot be hidden by an old END.
        initial = (
            None
            if snapshot and snapshot.next
            else snapshot.values
            if snapshot
            else {"question": run["question"]}
        )
        state = cast(
            DuplexState, graph.invoke(cast(DuplexState | None, initial), config, durability="sync")
        )
    # Recheck evidence after recovery; no checkpoint verdict can authorize publication.
    state.update(verify_result(state))
    try:
        return engine.commit_report(
            run_id,
            epoch,
            state["report"].encode(),
            Verification.model_validate(state["verification"]),
        )
    except DomainError as exc:
        if exc.code != "STALE_INPUT":
            raise
        engine.requeue_input(run_id, epoch)
        return None
