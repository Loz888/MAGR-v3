from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import Field

from mgar_v3.domain import Model

Arm = Literal["A", "B", "C"]


class Citation(Model):
    source_id: str
    quote: Annotated[str, Field(min_length=1, max_length=1200)]


class Answer(Model):
    status: Literal["supported", "insufficient", "denied"]
    values: dict[str, str] = Field(default_factory=dict, max_length=12)
    citations: list[Citation] = Field(default_factory=list, max_length=6)
    explanation: str = Field(default="", max_length=1000)
    diff: str | None = Field(default=None, max_length=32768)


class Summary(Model):
    notes: str = Field(max_length=1200)
    need_more: bool = False


class EvaluationAuthorization(Model):
    manifest_hash: str
    approved_by: str
    expires_at: datetime
    max_new_calls: Annotated[int, Field(strict=True, ge=1, le=270)]
    max_new_cost_micros: Annotated[int, Field(strict=True, gt=0, le=1971451)]
    allow_paid: bool = False
    allow_external_write: Literal[False] = False


class Case(Model):
    id: str
    category: Literal["qa", "cross", "repair", "negative"]
    question: str
    required_roles: list[Literal["knowledge", "repo", "data"]]
    document_id: str | None = None
    rows: list[dict[str, Any]] | None = None
    negative_condition: (
        Literal[
            "missing_data", "missing_knowledge", "forbidden_write", "version_conflict", "revoked"
        ]
        | None
    ) = None
    repair_variant: int | None = None
    source_path: str


class Source(Model):
    id: str
    role: Literal["knowledge", "repo", "data"]
    text: str
    version: str
