"""Local administrator accounts, cookie sessions and encrypted analysis configuration."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any
from uuid import UUID, uuid4

import psycopg
from cryptography.fernet import Fernet, InvalidToken
from pydantic import Field, SecretStr, field_validator

from mgar_v3.coordination.engine import Engine
from mgar_v3.domain import DomainError, Model
from mgar_v3.repository_analysis.domain import (
    REPOSITORY_ANALYSIS_GRAPH,
    AnalysisAuthorization,
    AnalysisLimits,
    AnalysisModelConfig,
    parse_repository_url,
)
from mgar_v3.settings import Settings

SESSION_COOKIE = "reposage_session"
SESSION_SECONDS = 7 * 24 * 60 * 60


class LoginInput(Model):
    username: Annotated[str, Field(min_length=3, max_length=80)]
    password: SecretStr

    @field_validator("username")
    @classmethod
    def canonical_username(cls, value: str) -> str:
        value = value.strip().lower()
        if not re.fullmatch(r"[a-z0-9_.@-]{3,80}", value):
            raise ValueError("Use 3-80 letters, numbers, dots, @, underscores or hyphens")
        return value


class AnalysisSettingsInput(Model):
    deepseek_api_key: SecretStr | None = None
    github_read_token: SecretStr | None = None
    model_id: Annotated[str, Field(min_length=1, max_length=100)] = "deepseek-chat"
    allowed_repositories: Annotated[list[str], Field(min_length=1, max_length=100)] = Field(
        default_factory=lambda: ["*"]
    )
    limits: AnalysisLimits = Field(
        default_factory=lambda: AnalysisLimits(
            max_model_calls=None,
            max_tool_calls=None,
            max_input_tokens=500000,
            max_total_tokens=500000,
            max_seconds=3600,
        )
    )
    authorization_days: Annotated[int, Field(strict=True, ge=1, le=365)] = 30
    allow_paid: bool = Field(default=False, strict=True)
    expected_revision: Annotated[int, Field(strict=True, ge=0)] = 0

    @field_validator("deepseek_api_key", "github_read_token")
    @classmethod
    def bounded_secret(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return None
        raw = value.get_secret_value().strip()
        if len(raw) > 4096 or any(char in raw for char in "\r\n\0"):
            raise ValueError("Invalid credential")
        return SecretStr(raw) if raw else None

    @field_validator("allowed_repositories")
    @classmethod
    def repositories(cls, values: list[str]) -> list[str]:
        if values == ["*"]:
            return values
        result = []
        for value in values:
            identity, ref, commit = parse_repository_url("https://github.com/" + value.strip())
            if ref or commit:
                raise ValueError("Use owner/repository, without a branch or commit")
            result.append(identity.full_name)
        return sorted(set(result))


def password_hash(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    value = hashlib.scrypt(
        password.encode(), salt=salt, n=16384, r=8, p=1, dklen=64, maxmem=64 * 1024 * 1024
    )
    return "scrypt$" + salt.hex() + "$" + value.hex()


def password_matches(password: str, encoded: str | None) -> bool:
    # Missing accounts use the same expensive operation as an existing account.
    salt = bytes.fromhex(encoded.split("$")[1]) if encoded else b"\0" * 16
    candidate = password_hash(password, salt)
    return encoded is not None and hmac.compare_digest(candidate, encoded)


@dataclass(frozen=True)
class BrowserSession:
    user_id: UUID
    username: str
    csrf_token: str


class WebAccounts:
    def __init__(self, engine: Engine):
        self.engine = engine

    def initialized(self) -> bool:
        with self.engine.transaction() as conn:
            return conn.execute("SELECT 1 FROM app.web_accounts").fetchone() is not None

    def create_owner(self, request: LoginInput) -> UUID:
        password = request.password.get_secret_value()
        if not 10 <= len(password) <= 128:
            raise DomainError("PASSWORD_TOO_SHORT", "密码应为 10 至 128 个字符。", 400)
        encoded = password_hash(password)
        user_id = uuid4()
        try:
            with self.engine.transaction() as conn:
                conn.execute(
                    "INSERT INTO app.web_accounts(user_id,username,password_hash) "
                    "VALUES (%s,%s,%s)",
                    (user_id, request.username, encoded),
                )
        except psycopg.errors.UniqueViolation as exc:
            raise DomainError(
                "ACCOUNT_ALREADY_EXISTS", "管理员账户已创建，请直接登录。", 409
            ) from exc
        return user_id

    def login(self, request: LoginInput, client: str) -> UUID:
        password = request.password.get_secret_value()
        if not 1 <= len(password) <= 128:
            raise DomainError("LOGIN_FAILED", "用户名或密码不正确。", 401)
        bucket = hashlib.sha256(client.encode()).hexdigest()
        with self.engine.transaction() as conn:
            attempt = conn.execute(
                "INSERT INTO app.web_login_attempts(bucket,attempts,expires_at) "
                "VALUES (%s,1,now()+interval '10 minutes') ON CONFLICT (bucket) DO UPDATE SET "
                "attempts=CASE WHEN web_login_attempts.expires_at<now() THEN 1 "
                "ELSE web_login_attempts.attempts+1 END, "
                "expires_at=CASE WHEN web_login_attempts.expires_at<now() "
                "THEN now()+interval '10 minutes' ELSE web_login_attempts.expires_at END "
                "RETURNING attempts",
                (bucket,),
            ).fetchone()
            account = conn.execute(
                "SELECT user_id,password_hash FROM app.web_accounts WHERE username=%s",
                (request.username,),
            ).fetchone()
        if attempt and attempt["attempts"] > 10:
            raise DomainError("LOGIN_RATE_LIMITED", "尝试次数过多，请在十分钟后重试。", 429)
        if not password_matches(password, account["password_hash"] if account else None):
            raise DomainError("LOGIN_FAILED", "用户名或密码不正确。", 401)
        with self.engine.transaction() as conn:
            conn.execute("DELETE FROM app.web_login_attempts WHERE bucket=%s", (bucket,))
        assert account is not None
        return UUID(str(account["user_id"]))

    def issue_session(self, user_id: UUID) -> str:
        token = secrets.token_urlsafe(48)
        with self.engine.transaction() as conn:
            conn.execute("DELETE FROM app.web_sessions WHERE expires_at<now()")
            conn.execute(
                "INSERT INTO app.web_sessions(token_hash,user_id,csrf_token,expires_at) "
                "VALUES (%s,%s,%s,%s)",
                (
                    hashlib.sha256(token.encode()).hexdigest(),
                    user_id,
                    secrets.token_urlsafe(32),
                    datetime.now(UTC) + timedelta(seconds=SESSION_SECONDS),
                ),
            )
        return token

    def session(self, token: str) -> BrowserSession | None:
        if not token or len(token) > 256:
            return None
        with self.engine.transaction() as conn:
            row = conn.execute(
                "SELECT u.user_id,u.username,s.csrf_token FROM app.web_sessions s "
                "JOIN app.web_accounts u USING(user_id) WHERE token_hash=%s AND expires_at>now()",
                (hashlib.sha256(token.encode()).hexdigest(),),
            ).fetchone()
        return BrowserSession(**row) if row else None

    def revoke(self, token: str) -> None:
        with self.engine.transaction() as conn:
            conn.execute(
                "DELETE FROM app.web_sessions WHERE token_hash=%s",
                (hashlib.sha256(token.encode()).hexdigest(),),
            )


class WebAnalysisSettings:
    def __init__(self, engine: Engine, settings: Settings):
        self.engine = engine
        self.base = settings
        self.key_path = settings.artifact_root.parent / "web" / "credentials.key"

    def _cipher(self, *, create: bool = False) -> Fernet:
        if create and not self.key_path.exists():
            self.key_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd, temporary = tempfile.mkstemp(dir=self.key_path.parent, prefix=".credentials-")
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(Fernet.generate_key())
                    stream.flush()
                    os.fsync(stream.fileno())
                try:
                    os.link(temporary, self.key_path)
                except FileExistsError:
                    pass
                directory = os.open(self.key_path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            finally:
                os.unlink(temporary)
        try:
            return Fernet(self.key_path.read_bytes())
        except (OSError, ValueError) as exc:
            raise DomainError(
                "SETTINGS_KEY_UNAVAILABLE", "无法读取配置加密密钥，请恢复本机配置备份。", 503
            ) from exc

    def snapshot(self) -> tuple[int, dict[str, Any] | None]:
        with self.engine.transaction() as conn:
            row = conn.execute(
                "SELECT revision,encrypted_payload FROM app.web_analysis_settings "
                "ORDER BY revision DESC LIMIT 1"
            ).fetchone()
        if row is None:
            return 0, None
        try:
            payload = json.loads(self._cipher().decrypt(row["encrypted_payload"].encode()))
        except (InvalidToken, ValueError) as exc:
            raise DomainError("SETTINGS_UNREADABLE", "无法解密保存的模型配置。", 503) from exc
        return int(row["revision"]), payload

    def effective(self, payload: dict[str, Any] | None) -> Settings:
        if payload is None:
            return self.base
        updates: dict[str, Any] = {"allow_paid": payload["allow_paid"]}
        for key in ("deepseek_api_key", "github_read_token"):
            if payload.get(key):
                updates[key] = SecretStr(payload[key])
        return self.base.model_copy(update=updates)

    def save(self, request: AnalysisSettingsInput, user: BrowserSession) -> None:
        revision, previous = self.snapshot()
        previous = previous or {}
        if revision != request.expected_revision:
            raise DomainError("SETTINGS_STALE", "配置已更新，请重新打开设置后再保存。", 409)
        effective = self.effective(previous or None)
        authorization = AnalysisAuthorization(
            approved_by=self.base.local_actor,
            expires_at=datetime.now(UTC) + timedelta(days=request.authorization_days),
            allow_paid=request.allow_paid,
            allowed_repositories=request.allowed_repositories,
            limits=request.limits,
            model=AnalysisModelConfig(model_id=request.model_id, max_output_tokens=8192),
        )
        payload = {
            **previous,
            "allow_paid": request.allow_paid,
            "authorization": authorization.model_dump(mode="json"),
        }
        for key in ("deepseek_api_key", "github_read_token"):
            supplied = getattr(request, key)
            existing = getattr(effective, key)
            if supplied is not None:
                payload[key] = supplied.get_secret_value()
            elif existing is not None:
                payload[key] = existing.get_secret_value()
        encrypted = self._cipher(create=True).encrypt(json.dumps(payload).encode()).decode()
        with self.engine.transaction() as conn:
            current = conn.execute(
                "SELECT coalesce(max(revision),0) AS revision FROM app.web_analysis_settings"
            ).fetchone()
            if current is None or current["revision"] != revision:
                raise DomainError("SETTINGS_STALE", "配置已更新，请重新打开设置。", 409)
            if conn.execute(
                "SELECT 1 FROM app.runs WHERE graph_version=%s AND execution_mode='live' "
                "AND status IN ('queued','running') LIMIT 1",
                (REPOSITORY_ANALYSIS_GRAPH,),
            ).fetchone():
                raise DomainError(
                    "ANALYSIS_BUSY", "有真实分析正在执行，请先暂停或等待完成后再修改配置。", 409
                )
            conn.execute(
                "INSERT INTO app.web_analysis_settings(encrypted_payload,updated_by) "
                "VALUES (%s,%s)",
                (encrypted, user.user_id),
            )
