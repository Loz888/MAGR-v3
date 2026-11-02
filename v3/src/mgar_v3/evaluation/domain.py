from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import Field

from mgar_v3.domain import Model


class TrialAuthorization(Model):
    manifest_hash: str
    predecessor_budget_hash: str
    approved_by: str
    expires_at: datetime
    additional_call_limit: Annotated[int, Field(strict=True, ge=1, le=24)]
    cumulative_cost_limit_micros: Annotated[int, Field(strict=True, gt=0, le=2000000)]
    allow_paid: bool
    allow_external_write: Literal[False] = False
    basis: str


class Interpretation(Model):
    text: Annotated[str, Field(min_length=1, max_length=1000)]
    evidence_ids: Annotated[list[UUID], Field(min_length=1, max_length=12)]


class Assessment(Model):
    status: Literal["supported", "insufficient", "denied"]
    metrics: Annotated[dict[str, str | None], Field(max_length=10)]
    conclusions: dict[str, bool | None]
    interpretations: Annotated[list[Interpretation], Field(max_length=8)]
    missing_sources: Annotated[list[Literal["knowledge", "repo", "data"]], Field(max_length=3)]
    caveats: Annotated[list[str], Field(max_length=8)]


class TrialProfile(Model):
    case_id: str
    arm: Literal["multi", "single"]
    profile: dict[str, Any]
