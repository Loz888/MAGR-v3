from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from mgar_v3.domain import DomainError, Model, TriggerInput, digest
from mgar_v3.settings import Settings


class LiveProfile(Model):
    case_id: str
    project_id: str
    repository: str
    issue_number: Annotated[int, Field(strict=True, gt=0)]
    actor: str
    question: Annotated[str, Field(min_length=1, max_length=8000)]
    source_kind: Literal["file", "issue"] = "file"
    source_path: str = "README.md"
    source_ref: Annotated[str | None, Field(pattern=r"^[a-f0-9]{40}$")] = None
    expected_source_hash: Annotated[str | None, Field(pattern=r"^[a-f0-9]{64}$")] = None
    model_id: str
    price_version: str
    input_micros_per_million: Annotated[int, Field(strict=True, gt=0)]
    output_micros_per_million: Annotated[int, Field(strict=True, gt=0)]
    context_tokens: Annotated[int, Field(strict=True, ge=4096, le=1048576)]
    max_output_tokens: Annotated[int, Field(strict=True, ge=256, le=4096)] = 1024
    max_source_bytes: Annotated[int, Field(strict=True, ge=1024, le=65536)] = 16384
    max_cost_micros: Annotated[int, Field(strict=True, gt=0, le=10000000)]
    max_model_calls: Annotated[int, Field(strict=True, ge=1, le=3)] = 1
    action_type: Literal["issue_comment"] = "issue_comment"

    @model_validator(mode="after")
    def fixed_source_required(self) -> Self:
        if self.source_kind == "file" and not self.source_ref:
            raise ValueError("File reads require an immutable commit")
        if self.source_kind == "issue" and not self.expected_source_hash:
            raise ValueError("Issue reads require a frozen title/body hash")
        return self

    @property
    def profile_hash(self) -> str:
        payload = self.model_dump(mode="json")
        if self.max_model_calls == 1:
            # Single-call profiles retain the hash used before configurable call limits.
            payload.pop("max_model_calls")
        return digest(payload)

    @property
    def reservation_micros(self) -> int:
        numerator = (
            self.context_tokens * self.input_micros_per_million
            + self.max_output_tokens * self.output_micros_per_million
        )
        return (numerator + 999999) // 1000000


class Authorization(Model):
    profile_hash: str
    approved_by: str
    expires_at: datetime
    allow_paid: bool = False
    allow_external_write: bool = False


class LivePermit:
    def __init__(self, profile: LiveProfile, authorization: Authorization, settings: Settings):
        self.profile, self.authorization, self.settings = profile, authorization, settings

    def validate(self, *, external_write: bool = False) -> None:
        auth, profile = self.authorization, self.profile
        if (
            auth.profile_hash != profile.profile_hash
            or auth.expires_at.tzinfo is None
            or auth.expires_at <= datetime.now(UTC)
        ):
            raise DomainError(
                "LIVE_AUTHORIZATION_INVALID", "Live authorization is stale or mismatched", 403
            )
        if auth.approved_by.lower() not in {a.lower() for a in self.settings.allowed_actors}:
            raise DomainError("LIVE_ACTOR_DENIED", "Live approver is not configured", 403)
        if not auth.allow_paid or not self.settings.allow_paid:
            raise DomainError("PAID_NOT_AUTHORIZED", "Paid execution has not been authorized", 403)
        if profile.reservation_micros > profile.max_cost_micros:
            raise DomainError(
                "BUDGET_EXHAUSTED", "Budget cannot reserve one bounded model call", 403
            )
        if (
            profile.repository.lower() != self.settings.repository.lower()
            or profile.project_id != self.settings.project_id
        ):
            raise DomainError(
                "LIVE_SCOPE_DENIED", "Live profile differs from configured scope", 403
            )
        if external_write and (
            not auth.allow_external_write or not self.settings.allow_external_write
        ):
            raise DomainError(
                "WRITE_NOT_AUTHORIZED", "External publication has not been authorized", 403
            )

    def validate_event(self, event: TriggerInput) -> None:
        self.validate()
        profile = self.profile
        if (
            event.project_id != profile.project_id
            or event.repository != profile.repository.lower()
            or event.issue_number != profile.issue_number
            or event.actor != profile.actor.lower()
            or event.body != "/mgar investigate " + profile.question
        ):
            raise DomainError("LIVE_SCOPE_DENIED", "Event differs from the reviewed live case", 403)


def load_permit(profile_path: Path, authorization_path: Path, settings: Settings) -> LivePermit:
    return LivePermit(
        LiveProfile.model_validate_json(profile_path.read_bytes()),
        Authorization.model_validate_json(authorization_path.read_bytes()),
        settings,
    )
