from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from mgar_v3.domain import DomainError, Model, TriggerInput, digest
from mgar_v3.repair.domain import RepairScope
from mgar_v3.settings import Settings


class LiveProfile(Model):
    investigation_version: Literal["r1-quotes-v1", "r4-experts-v1", "r5-repair-v1"] = "r1-quotes-v1"
    repair: RepairScope | None = Field(default=None, exclude_if=lambda value: value is None)
    knowledge_snapshot_ids: list[str] = Field(default_factory=list, max_length=2)
    rag_config_hash: str | None = None
    data_fixture_hash: str | None = None
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
    max_result_comments: Annotated[int, Field(strict=True, ge=1, le=3)] = 1
    action_type: Literal["issue_comment", "draft_pr"] = "issue_comment"

    @model_validator(mode="after")
    def fixed_source_required(self) -> Self:
        if self.investigation_version == "r5-repair-v1":
            from mgar_v3.repair.workspace import validate_path

            if (
                self.action_type != "draft_pr"
                or self.repair is None
                or self.source_kind != "file"
                or self.max_result_comments != 1
                or not self.expected_source_hash
            ):
                raise ValueError("R5 requires its explicit immutable source and Draft PR scope")
            validate_path(self.repair.workspace_prefix)
            if self.source_path != self.repair.workspace_prefix + "/orders.go":
                raise ValueError("R5 supports the bounded Go order function only")
            roots = self.repair.predecessor_budget_hashes
            if len(set(roots)) != len(roots) or any(
                len(h) != 64 or any(c not in "0123456789abcdef" for c in h) for h in roots
            ):
                raise ValueError("R5 cumulative predecessors must be distinct SHA256 values")
        elif self.action_type != "issue_comment" or self.repair is not None:
            raise ValueError("Repair scopes cannot be enabled under an R1/R4 profile")
        if self.investigation_version == "r4-experts-v1":
            from uuid import UUID

            if (
                not self.knowledge_snapshot_ids
                or not self.rag_config_hash
                or not self.data_fixture_hash
            ):
                raise ValueError(
                    "R4 live requires frozen knowledge, model configuration and data hashes"
                )
            for value in self.knowledge_snapshot_ids:
                UUID(value)
            for value in (self.rag_config_hash, self.data_fixture_hash):
                if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
                    raise ValueError("R4 source hashes must be SHA256 values")
        elif self.knowledge_snapshot_ids or self.rag_config_hash or self.data_fixture_hash:
            raise ValueError("R4 source configuration requires its explicit profile version")
        if self.source_kind == "file" and not self.source_ref:
            raise ValueError("File reads require an immutable commit")
        if self.source_kind == "issue" and not self.expected_source_hash:
            raise ValueError("Issue reads require a frozen title/body hash")
        return self

    @property
    def profile_hash(self) -> str:
        payload = self.model_dump(mode="json")
        if self.investigation_version != "r5-repair-v1":
            payload.pop("repair", None)  # Preserve every previously authorized profile hash.
        if self.investigation_version == "r1-quotes-v1":
            for key in (
                "investigation_version",
                "knowledge_snapshot_ids",
                "rag_config_hash",
                "data_fixture_hash",
            ):
                payload.pop(key)
        if self.max_model_calls == 1:
            # Single-call profiles retain the hash used before configurable call limits.
            payload.pop("max_model_calls")
        if self.max_result_comments == 1:
            payload.pop("max_result_comments")
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

    def validate_event(self, event: TriggerInput, *, follow_up: bool = False) -> None:
        self.validate()
        profile = self.profile
        if (
            event.project_id != profile.project_id
            or event.repository != profile.repository.lower()
            or event.issue_number != profile.issue_number
            or event.actor != profile.actor.lower()
            or (not follow_up and event.body != "/mgar investigate " + profile.question)
        ):
            raise DomainError("LIVE_SCOPE_DENIED", "Event differs from the reviewed live case", 403)


def load_permit(profile_path: Path, authorization_path: Path, settings: Settings) -> LivePermit:
    return LivePermit(
        LiveProfile.model_validate_json(profile_path.read_bytes()),
        Authorization.model_validate_json(authorization_path.read_bytes()),
        settings,
    )
