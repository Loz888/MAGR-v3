"""Bounded expert interface with an explicit deterministic protocol provider."""

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from mgar_v3.coordination.engine import Engine
    from mgar_v3.live import LivePermit
from uuid import UUID

from mgar_v3.agents.domain import Assignment, Fact, Finding
from mgar_v3.agents.verify import cell_text
from mgar_v3.mcp.r4 import TaskTools


class ExpertProvider(Protocol):
    mode: str

    def investigate(self, assignment: Assignment, tools: TaskTools) -> Finding: ...


class FixtureExperts:
    mode = "fixture"

    def investigate(self, assignment: Assignment, tools: TaskTools) -> Finding:
        facts: list[Fact] = []
        if assignment.role == "knowledge":
            tools.call("recall_memory", {})
            for snapshot in assignment.snapshot_ids:
                hits = tools.call(
                    "search_knowledge",
                    {"query": assignment.question[:2000], "snapshot_id": str(snapshot), "top_k": 2},
                )
                for hit in hits["hits"][:2]:
                    result = tools.call(
                        "read_knowledge",
                        {"chunk_id": hit["chunk_id"], "snapshot_id": str(snapshot)},
                    )
                    quote = result["content"][:1800]
                    facts.append(
                        Fact(evidence_id=UUID(result["evidence_id"]), text=quote, quote=quote)
                    )
        elif assignment.role == "repo":
            result = tools.call("read_repository", {})
            quote = result["content"][:1800]
            facts.append(Fact(evidence_id=UUID(result["evidence_id"]), text=quote, quote=quote))
        else:
            source = tools.call("describe_tables", {})
            result = tools.call(
                "query_readonly",
                {
                    "sql": f"SELECT count(*) AS events, count(DISTINCT order_id) AS orders, "
                    f"sum(amount) AS amount FROM {source['schema']}.orders WHERE status = %s",
                    "parameters": ["paid"],
                },
            )
            import json

            data = json.loads(result["content"])
            for index, column in enumerate(data["columns"]):
                value = data["rows"][0][index]
                facts.append(
                    Fact(
                        evidence_id=UUID(result["evidence_id"]),
                        text=cell_text(column["name"], value, column["type_oid"]),
                        cell={
                            "row": 0,
                            "column": index,
                            "value": value,
                            "type_oid": column["type_oid"],
                        },
                    )
                )
        return Finding(
            task_id=assignment.task_id,
            role=assignment.role,
            status="succeeded" if facts else "missing",
            facts=facts,
            open_questions=[] if facts else ["No readable knowledge source is fixed to this Run."],
        )


class DeepSeekExperts(FixtureExperts):
    """Scoped live source observations; Data uses the fixed paid-event audit query."""

    mode = "live"

    def __init__(self, engine: "Engine", permit: "LivePermit") -> None:
        self.engine, self.permit = engine, permit
        if permit.profile.investigation_version != "r4-experts-v1":
            from mgar_v3.domain import DomainError

            raise DomainError(
                "R4_LIVE_PROFILE_REQUIRED",
                "R1 authorization does not authorize an R4 expert run",
                403,
            )

    def investigate(self, assignment: Assignment, tools: TaskTools) -> Finding:
        from mgar_v3.models.deepseek import DeepSeekInvestigator

        self.permit.validate()
        if assignment.role == "data":
            return super().investigate(assignment, tools)
        if assignment.role == "knowledge":
            tools.call("recall_memory", {})
            hit = None
            fixed = None
            for snapshot in assignment.snapshot_ids:
                result = tools.call(
                    "search_knowledge",
                    {"query": assignment.question[:2000], "snapshot_id": str(snapshot), "top_k": 1},
                )
                if result["hits"]:
                    hit = result["hits"][0]
                    fixed = snapshot
                    break
            if hit is None:
                return Finding(
                    task_id=assignment.task_id,
                    role=assignment.role,
                    status="missing",
                    open_questions=["No readable source matched the fixed knowledge snapshots."],
                )
            source = tools.call(
                "read_knowledge", {"chunk_id": hit["chunk_id"], "snapshot_id": str(fixed)}
            )
        else:
            source = tools.call("read_repository", {})
        finding = DeepSeekInvestigator(self.engine, self.permit).investigate(
            tools.engine.tools._grant(
                tools.token,
                "read_repository" if assignment.role == "repo" else "read_knowledge",
                charge=False,
            )["run_id"],
            assignment.question,
            source["content"],
            UUID(source["evidence_id"]),
            execution_epoch=tools.engine.tools._grant(
                tools.token,
                "read_repository" if assignment.role == "repo" else "read_knowledge",
                charge=False,
            )["execution_epoch"],
            step_key="r4:" + assignment.task_id,
        )
        return Finding(
            task_id=assignment.task_id,
            role=assignment.role,
            status="succeeded",
            facts=[
                Fact(evidence_id=c.evidence_id, text=c.statement, quote=c.quote)
                for c in finding.claims[:8]
            ],
            open_questions=finding.open_questions[:8],
        )
