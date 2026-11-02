from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field

from mgar_v3.domain import Model

RouterAction = Literal[
    "ignore",
    "create_ticket",
    "request_clarification",
    "reply_system",
    "pause_ticket",
    "resume_ticket",
    "cancel_ticket",
    "attach_observer",
]
MessageCode = Literal["clarification", "queued", "unavailable"]


class Proposal(Model):
    thread_id: UUID
    action: RouterAction
    target_id: UUID | None = None
    related_ticket_ids: list[UUID] = Field(default_factory=list, max_length=3)
    message_code: MessageCode | None = None


class Proposals(Model):
    proposals: Annotated[list[Proposal], Field(min_length=1, max_length=16)]


class RouterDecision(Proposal):
    decision_key: Annotated[str, Field(min_length=1, max_length=160)]
    input_event_ids: Annotated[list[UUID], Field(min_length=1, max_length=4)]
    expected_thread_revision: Annotated[int, Field(strict=True, ge=1)]
    policy_version: str
    context_snapshot_hash: Annotated[str, Field(pattern="^[a-f0-9]{64}$")]
    expires_at: datetime
    mandate_id: UUID | None = None
    expected_execution_epoch: Annotated[int | None, Field(strict=True, ge=0)] = None


class CorrectionInput(Model):
    event_ids: Annotated[list[UUID], Field(min_length=1, max_length=64)]
    expected_thread_revision: Annotated[int, Field(strict=True, ge=1)]
    reason: Annotated[str, Field(min_length=1, max_length=1000)]


class EventInput(Model):
    issue_number: Annotated[int, Field(strict=True, gt=0)]
    body: Annotated[str, Field(min_length=1, max_length=8000)]
    client_request_id: Annotated[str, Field(min_length=1, max_length=200)]
    source: Literal["web", "cli"] = "web"
    intent: Literal["message", "investigate"] = "message"
    execution_mode: Literal["fixture", "live"] = "fixture"
