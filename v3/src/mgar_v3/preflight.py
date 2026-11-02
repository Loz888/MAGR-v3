from pathlib import Path
from typing import Any

from mgar_v3.domain import DomainError
from mgar_v3.live import LiveProfile, load_permit
from mgar_v3.settings import Settings


def preflight(
    profile_path: Path, settings: Settings, authorization_path: Path | None = None
) -> dict[str, Any]:
    profile = LiveProfile.model_validate_json(profile_path.read_bytes())
    missing = []
    if not settings.allow_paid:
        missing.append("MGAR_V3_ALLOW_PAID=1")
    if not settings.allow_external_write:
        missing.append("MGAR_V3_ALLOW_EXTERNAL_WRITE=1")
    for field, environment in [
        (settings.deepseek_api_key, "MGAR_V3_DEEPSEEK_API_KEY"),
        (settings.github_read_token, "MGAR_V3_GITHUB_READ_TOKEN"),
        (settings.github_write_token, "MGAR_V3_GITHUB_WRITE_TOKEN"),
    ]:
        if field is None:
            missing.append(environment)
    if authorization_path is None or not authorization_path.is_file():
        missing.append("matching_user_authorization")
    else:
        try:
            load_permit(profile_path, authorization_path, settings).validate(external_write=True)
        except DomainError as exc:
            missing.append(exc.code)
    if profile.price_version == "REVIEW_REQUIRED":
        missing.append("reviewed_model_price_and_context")
    return {
        "status": "NOT_RUN",
        "ready_for_execution": not missing,
        "profile_hash": profile.profile_hash,
        "profile": profile.model_dump(mode="json"),
        "maximum_model_calls": profile.max_model_calls,
        "maximum_cost_micros": profile.max_cost_micros,
        "per_call_reservation_micros": profile.reservation_micros,
        "maximum_reservation_micros": profile.max_model_calls * profile.reservation_micros,
        "external_action": (
            f"one approved issue_comment on {profile.repository}#{profile.issue_number}"
        ),
        "missing": missing,
        "network_calls": 0,
        "paid_calls": 0,
    }
