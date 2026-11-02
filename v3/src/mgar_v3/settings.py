from pathlib import Path
from typing import Any

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MGAR_V3_", env_file=ROOT / ".env", extra="ignore")

    database_url: SecretStr
    redis_url: SecretStr = SecretStr("redis://127.0.0.1:56379/0")
    local_token: SecretStr = Field(min_length=24)
    github_webhook_secret: SecretStr = SecretStr("")
    artifact_root: Path = ROOT / ".local/artifacts"
    project_id: str = "orders-demo"
    repository: str = "loz888/magr-v3"
    local_actor: str = "loz888"
    allowed_actors: list[str] = ["loz888"]
    policy_version: str = "r1-explicit-command-v1"
    max_body_bytes: int = Field(default=65536, ge=1024, le=1048576)
    approval_ttl_seconds: int = Field(default=3600, ge=60, le=86400)
    allow_paid: bool = False
    allow_external_write: bool = False
    deepseek_api_key: SecretStr | None = None
    github_read_token: SecretStr | None = None
    github_write_token: SecretStr | None = None
    live_profile_path: Path | None = None
    live_authorization_path: Path | None = None

    @field_validator("allow_paid", "allow_external_write", mode="before")
    @classmethod
    def explicit_enable_only(cls, value: Any) -> bool:
        # Deployment flags only enable on the documented literal 1; unknown values fail closed.
        return value is True or value == "1"
