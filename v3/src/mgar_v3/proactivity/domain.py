from datetime import datetime
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from mgar_v3.domain import Model, digest

EventKind = Literal["ci_failure", "dependency_update", "pr_comment"]


class Policy(Model):
    repository: str
    issue_number: Annotated[int, Field(strict=True, gt=0)] = 1
    kinds: Annotated[list[EventKind], Field(min_length=1, max_length=3)]
    allowed_actors: Annotated[list[str], Field(min_length=1, max_length=10)]
    required_labels: list[str] = Field(default_factory=list, max_length=8)
    source_ref: Annotated[str, Field(pattern=r"^[a-f0-9]{40}$")]
    question: Annotated[str, Field(min_length=1, max_length=8000)]
    expires_at: datetime
    max_runs: Annotated[int, Field(strict=True, ge=1, le=10)] = 1
    max_reads: Annotated[int, Field(strict=True, ge=1, le=200)] = 8
    max_model_calls: Annotated[int, Field(strict=True, ge=0, le=3)] = 0
    max_cost_micros: Annotated[int, Field(strict=True, ge=0, le=20000)] = 0
    execution_mode: Literal["fixture", "live"] = "fixture"
    graph_version: Literal["r1-quotes-v1", "r4-experts-v1"] = "r1-quotes-v1"
    model_profile_hash: str | None = None

    @model_validator(mode="after")
    def bounded(self) -> Self:
        if self.expires_at.tzinfo is None:
            raise ValueError("Policy expiry requires timezone")
        if self.execution_mode == "live" and (
            not self.model_profile_hash or self.max_model_calls == 0 or self.max_cost_micros == 0
        ):
            raise ValueError("Live speculation needs a separate bound paid permit and budget")
        if self.execution_mode == "fixture" and (self.max_model_calls or self.max_cost_micros):
            raise ValueError("Fixture speculation cannot grant paid calls")
        return self

    @property
    def policy_hash(self) -> str:
        return digest(self.model_dump(mode="json"))


class Activation(Model):
    policy_hash: str
    mode: Literal["auto", "revoked"]


class ProactiveEvent(Model):
    external_event_id: Annotated[str, Field(min_length=1, max_length=160)]
    repository: str
    actor: str
    kind: EventKind
    issue_number: Annotated[int, Field(strict=True, gt=0)]
    source_ref: Annotated[str, Field(pattern=r"^[a-f0-9]{40}$")]
    labels: list[str] = Field(default_factory=list, max_length=8)
    body: str = Field(default="", max_length=8000)
