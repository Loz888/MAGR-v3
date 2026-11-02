import os
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
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
    graph_version: Literal["r1-quotes-v1", "r2-mailbox-v1", "r4-experts-v1"] = "r4-experts-v1"
    max_body_bytes: int = Field(default=65536, ge=1024, le=1048576)
    approval_ttl_seconds: int = Field(default=3600, ge=60, le=86400)
    allow_paid: bool = False
    allow_external_write: bool = False
    deepseek_api_key: SecretStr | None = None
    github_read_token: SecretStr | None = None
    github_write_token: SecretStr | None = None
    github_mcp_binary: Path | None = None
    github_use_gh_token: bool = False
    live_profile_path: Path | None = None
    live_authorization_path: Path | None = None
    router_mode: Literal["disabled", "fixture", "live"] = "fixture"
    router_batch_max: int = Field(default=16, ge=1, le=16)
    router_context_bytes: int = Field(default=65536, ge=4096, le=65536)
    router_decision_ttl_seconds: int = Field(default=120, ge=1, le=300)
    router_profile_path: Path | None = None
    router_authorization_path: Path | None = None
    max_concurrent_runs: int = Field(default=2, ge=1, le=4)
    rag_mode: Literal["fixture", "local"] = "fixture"
    data_profile_path: Path = ROOT / ".local/data-reader.json"
    r4_tool_base_url: str | None = None
    r4_sql_ttl_seconds: int = Field(default=300, ge=1, le=3600)

    @field_validator("r4_tool_base_url")
    @classmethod
    def local_tool_server(cls, value: str | None) -> str | None:
        if value is not None:
            parsed = urlsplit(value)
            if (
                parsed.scheme != "http"
                or parsed.hostname not in {"localhost", "127.0.0.1"}
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
                or parsed.path not in {"", "/"}
            ):
                raise ValueError("R4 task tokens may only be sent to the local MCP authority")
        return value

    @field_validator("allow_paid", "allow_external_write", mode="before")
    @classmethod
    def explicit_enable_only(cls, value: Any) -> bool:
        # Deployment flags only enable on the documented literal 1; unknown values fail closed.
        return value is True or value == "1"

    @model_validator(mode="after")
    def configured_github_reference(self) -> "Settings":
        if self.github_use_gh_token:
            token = os.environ.get("GH_TOKEN")
            if token:
                if self.github_read_token is None:
                    self.github_read_token = SecretStr(token)
                if self.github_write_token is None:
                    self.github_write_token = SecretStr(token)
        return self
