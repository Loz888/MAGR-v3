from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import Field

from mgar_v3.domain import Model

Role = Literal["knowledge", "repo", "data"]


class Assignment(Model):
    task_id: str
    role: Role
    question: Annotated[str, Field(min_length=1, max_length=8000)]
    round_no: Annotated[int, Field(strict=True, ge=0, le=2)]
    input_revision: int
    snapshot_ids: Annotated[list[UUID], Field(max_length=2)] = Field(default_factory=list)


class InvestigationPlan(Model):
    question: str
    tasks: Annotated[list[Assignment], Field(min_length=1, max_length=3)]
    required_roles: Annotated[list[Role], Field(min_length=1, max_length=3)]
    max_rounds: Literal[2] = 2
    policy: Literal["readonly-investigation-v1"] = "readonly-investigation-v1"


class Fact(Model):
    evidence_id: UUID
    text: Annotated[str, Field(min_length=1, max_length=2000)]
    quote: Annotated[str | None, Field(max_length=2000)] = None
    cell: dict[str, Any] | None = None


class Finding(Model):
    task_id: str
    role: Role
    status: Literal["succeeded", "missing", "failed"]
    facts: Annotated[list[Fact], Field(max_length=8)] = Field(default_factory=list)
    open_questions: Annotated[list[str], Field(max_length=8)] = Field(default_factory=list)
    followups: Annotated[list[str], Field(max_length=2)] = Field(default_factory=list)
    error_code: str | None = None


class Resolution(Model):
    verified_facts: Annotated[list[Fact], Field(max_length=24)]
    hypotheses: Annotated[list[str], Field(max_length=8)]
    contradictions: Annotated[list[str], Field(max_length=8)]
    missing_evidence: Annotated[list[str], Field(max_length=12)]
    next_steps: Annotated[list[str], Field(max_length=8)]
    completed_roles: list[Role]
    complete: bool


class ToolRequest(Model):
    tool: Literal[
        "search_knowledge",
        "read_knowledge",
        "recall_memory",
        "read_repository",
        "describe_tables",
        "query_readonly",
    ]
    arguments: dict[str, Any] = Field(default_factory=dict)


def merge_findings(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    from mgar_v3.domain import DomainError, digest

    for key, value in right.items():
        if key in left and digest(left[key]) != digest(value):
            raise DomainError(
                "FINDING_CONFLICT", "The same task returned different immutable results"
            )
    return {**left, **right}
