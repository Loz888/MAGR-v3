import hashlib
import json
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


def digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(encoded).hexdigest()


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TriggerInput(Model):
    source: Literal["github", "cli"]
    external_event_id: Annotated[str, Field(min_length=1, max_length=200)]
    project_id: Annotated[str, Field(min_length=1, max_length=100)]
    repository: Annotated[str, Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")]
    issue_number: Annotated[int, Field(strict=True, gt=0)]
    actor: Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,100}$")]
    body: Annotated[str, Field(min_length=1, max_length=16000)]
    execution_mode: Literal["fixture", "live"] = "fixture"

    @field_validator("repository", "actor")
    @classmethod
    def normalize_identity(cls, value: str) -> str:
        return value.lower()


class EventReceipt(Model):
    event_id: UUID
    thread_id: UUID
    thread_seq: int
    ticket_id: UUID | None
    run_id: UUID | None
    route: Literal["created", "direct", "unrouted"]
    duplicate: bool = False


class Evidence(Model):
    evidence_id: UUID
    run_id: UUID
    project_id: str
    source_uri: str
    source_version: str
    content_hash: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    start_line: Annotated[int, Field(strict=True, ge=1)]
    end_line: Annotated[int, Field(strict=True, ge=1)]
    tool_call_id: UUID


class Claim(Model):
    statement: Annotated[str, Field(min_length=1, max_length=2000)]
    evidence_id: UUID
    quote: Annotated[str, Field(min_length=1, max_length=2000)]


class Investigation(Model):
    claims: Annotated[list[Claim], Field(min_length=1, max_length=10)]
    open_questions: list[str] = Field(default_factory=list, max_length=10)


class Verification(Model):
    passed: bool
    checks: list[str]
    reasons: list[str]
    scope: Literal["mechanical_evidence_only"] = "mechanical_evidence_only"


class ApprovalInput(Model):
    action_id: UUID
    payload_hash: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class RunInput(Model):
    project_id: str
    question: Annotated[str, Field(min_length=1, max_length=8000)]
    issue_number: Annotated[int, Field(strict=True, gt=0)]
    client_request_id: Annotated[str, Field(min_length=1, max_length=200)]
    execution_mode: Literal["fixture", "live"] = "fixture"


class ControlInput(Model):
    action: Literal["pause", "cancel", "resume"]


class MessageInput(Model):
    client_request_id: Annotated[str, Field(min_length=1, max_length=200)]
    body: Annotated[str, Field(min_length=1, max_length=16000)]


class DomainError(Exception):
    def __init__(self, code: str, message: str, status: int = 409):
        super().__init__(message)
        self.code = code
        self.status = status
