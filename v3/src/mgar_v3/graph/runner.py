import hashlib
from pathlib import Path
from typing import Any, TypedDict, cast
from uuid import UUID, uuid4

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph

from mgar_v3.coordination.engine import Engine
from mgar_v3.domain import Claim, DomainError, Evidence, Investigation, Verification
from mgar_v3.evidence.verifier import verify
from mgar_v3.graph.checkpoints import checkpoint_saver
from mgar_v3.settings import ROOT


class GraphState(TypedDict, total=False):
    question: str
    evidence_id: str
    content_hash: str
    investigation: dict[str, Any]
    verification: dict[str, Any]
    report: str


class FixtureInvestigator:
    mode = "fixture"
    execution_epoch: int = 0
    model_step_key: str = "investigate:v1"

    def __init__(self, path: Path = ROOT / "fixtures/order-service.md"):
        self.path = path

    def read(self) -> tuple[bytes, str, str]:
        content = self.path.read_bytes()
        return content, "fixture://order-service/notes", hashlib.sha256(content).hexdigest()

    def investigate(self, question: str, content: str, evidence_id: UUID) -> Investigation:
        observations = [line for line in content.splitlines() if line.startswith("Observation:")]
        return Investigation(
            claims=[
                Claim(statement=line, quote=line, evidence_id=evidence_id)
                for line in observations[:10]
            ],
            open_questions=[
                "Fixture execution checks the protocol; root cause is not established."
            ],
        )


class Runner:
    def __init__(self, engine: Engine, investigator: FixtureInvestigator | None = None):
        self.engine = engine
        self.investigator = investigator or FixtureInvestigator()

    def execute(self, run_id: UUID) -> UUID | None:
        from mgar_v3.recovery.heartbeat import worker_heartbeat

        with worker_heartbeat(
            self.engine.recovery, run_id, lambda: getattr(self, "execution_epoch", None)
        ):
            return self._execute(run_id)

    def _execute(self, run_id: UUID) -> UUID | None:
        if self.engine.commerce.binding(run_id):
            return self.engine.commerce.execute(self, run_id)
        if self.engine.get_run(run_id)["graph_version"] == "r5-repair-v1":
            from mgar_v3.repair.graph import execute_repair

            return execute_repair(self, run_id)
        if self.engine.get_run(run_id)["graph_version"] == "r2-mailbox-v1":
            from mgar_v3.graph.duplex import execute_duplex

            return execute_duplex(self, run_id)
        if self.engine.get_run(run_id)["graph_version"] == "r4-experts-v1":
            from mgar_v3.graph.experts import execute_experts

            return execute_experts(self, run_id)
        if self.engine.get_run(run_id)["graph_version"] != "r1-quotes-v1":
            raise DomainError(
                "GRAPH_VERSION_UNSUPPORTED", "Graph version requires its original implementation"
            )
        run = self.engine.begin_run(run_id)
        epoch = run["execution_epoch"]
        self.execution_epoch = epoch
        self.investigator.execution_epoch = epoch
        if run["execution_mode"] != self.investigator.mode:
            raise DomainError("EXECUTION_MODE_MISMATCH", "Investigator and Run modes differ")

        def read_source(state: GraphState) -> GraphState:
            content, uri, version = self.investigator.read()
            evidence = Evidence(
                evidence_id=uuid4(),
                run_id=run_id,
                project_id=run["project_id"],
                source_uri=uri,
                source_version=version,
                content_hash=hashlib.sha256(content).hexdigest(),
                start_line=1,
                end_line=len(content.decode().splitlines()),
                tool_call_id=uuid4(),
            )
            self.engine.record_read(evidence, epoch, content)
            return {"evidence_id": str(evidence.evidence_id), "content_hash": evidence.content_hash}

        def investigate(state: GraphState) -> GraphState:
            content = self.engine.artifacts.read(state["content_hash"]).decode()
            finding = self.investigator.investigate(
                state["question"], content, UUID(state["evidence_id"])
            )
            return {"investigation": finding.model_dump(mode="json")}

        def verify_result(state: GraphState) -> GraphState:
            finding = Investigation.model_validate(state["investigation"])
            verdict = verify(self.engine, run_id, finding)
            lines = [
                "# MGAR investigation",
                "",
                f"Execution mode: {run['execution_mode']}",
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
            return {"verification": verdict.model_dump(mode="json"), "report": "\n".join(lines)}

        builder = StateGraph(GraphState)
        builder.add_node("read_source", read_source)
        builder.add_node("investigate", investigate)
        builder.add_node("verify", verify_result)
        builder.add_edge(START, "read_source")
        builder.add_edge("read_source", "investigate")
        builder.add_edge("investigate", "verify")
        builder.add_edge("verify", END)
        # This connection cannot own or change global business state.
        with checkpoint_saver(self.engine.settings.database_url.get_secret_value()) as saver:
            graph = builder.compile(checkpointer=saver)
            config: RunnableConfig = {"configurable": {"thread_id": str(run_id)}}
            checkpoint = saver.get_tuple(config)
            resume = run["reason_code"] == "RESUME_FROM_CHECKPOINT" and checkpoint is not None
            state = graph.invoke(
                None if resume else {"question": run["question"]}, config, durability="sync"
            )
        # Checkpoints never promote an old mechanical verdict to current business truth.
        final_state = cast(GraphState, state)
        final_state.update(verify_result(final_state))
        return self.engine.commit_report(
            run_id,
            epoch,
            final_state["report"].encode(),
            Verification.model_validate(final_state["verification"]),
        )
