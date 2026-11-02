from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

from pydantic import Field

from mgar_v3.domain import DomainError, Model, digest
from mgar_v3.live import LiveProfile
from mgar_v3.settings import ROOT, Settings


class RouterProfile(Model):
    project_id: str
    repository: str
    actor: str
    budget_profile_hash: Annotated[str, Field(pattern="^[a-f0-9]{64}$")]
    model_id: str
    price_version: str
    input_micros_per_million: Annotated[int, Field(strict=True, gt=0)]
    output_micros_per_million: Annotated[int, Field(strict=True, gt=0)]
    context_tokens: Annotated[int, Field(strict=True, ge=4096, le=1048576)]
    max_output_tokens: Annotated[int, Field(strict=True, ge=256, le=4096)] = 2048
    max_input_bytes: Annotated[int, Field(strict=True, ge=4096, le=65536)] = 65536

    @property
    def profile_hash(self) -> str:
        return digest(self.model_dump(mode="json"))

    @property
    def reservation_micros(self) -> int:
        return (
            self.context_tokens * self.input_micros_per_million
            + self.max_output_tokens * self.output_micros_per_million
            + 999999
        ) // 1000000


class RouterAuthorization(Model):
    profile_hash: str
    approved_by: str
    expires_at: datetime
    allow_paid: bool = False
    context_snapshot_hash: str | None = None


class RouterPermit:
    def __init__(
        self,
        profile: RouterProfile,
        authorization: RouterAuthorization,
        root_budget: LiveProfile,
        settings: Settings,
    ):
        self.profile, self.authorization, self.root_budget, self.settings = (
            profile,
            authorization,
            root_budget,
            settings,
        )

    def validate(self, context_hash: str) -> None:
        p, a, s = self.profile, self.authorization, self.settings
        if (
            a.profile_hash != p.profile_hash
            or a.expires_at.tzinfo is None
            or a.expires_at <= datetime.now(UTC)
            or (a.context_snapshot_hash is not None and a.context_snapshot_hash != context_hash)
        ):
            raise DomainError(
                "ROUTER_AUTHORIZATION_INVALID", "Router authorization is stale or mismatched", 403
            )
        if (
            not a.allow_paid
            or not s.allow_paid
            or p.project_id != s.project_id
            or p.repository.lower() != s.repository.lower()
            or p.actor.lower() != a.approved_by.lower()
            or p.actor.lower() != s.local_actor.lower()
            or p.actor.lower() not in {x.lower() for x in s.allowed_actors}
            or p.budget_profile_hash != self.root_budget.profile_hash
            or self.root_budget.project_id != p.project_id
            or self.root_budget.repository.lower() != p.repository.lower()
            or self.root_budget.actor.lower() != p.actor.lower()
            or p.price_version == "REVIEW_REQUIRED"
        ):
            raise DomainError("ROUTER_SCOPE_DENIED", "Router call exceeds the delegated scope", 403)


def load_router_permit(
    profile: Path,
    authorization: Path,
    settings: Settings,
    root_budget: Path = ROOT / "config/live-r1-proposed.json",
) -> RouterPermit:
    return RouterPermit(
        RouterProfile.model_validate_json(profile.read_bytes()),
        RouterAuthorization.model_validate_json(authorization.read_bytes()),
        LiveProfile.model_validate_json(root_budget.read_bytes()),
        settings,
    )
