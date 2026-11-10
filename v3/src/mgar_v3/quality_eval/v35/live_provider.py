"""V3.5 E02 live DeepSeek provider with hard call and cost caps.

A live arm is only constructed from an explicit authorization file.  The
authorization binds the approver, the actor, the model id, an expiry, a call
cap and a cost cap; every check fails closed before a paid request is sent.

A call whose outcome cannot be accounted (transport failure, timeout, missing
usage) raises ``USAGE_UNKNOWN`` so the structured provider records it once and
never resends it, because repeating an unaccounted paid call would double
spend.  The API key is never logged, stored in a report or echoed in an error.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from pydantic import Field

from mgar_v3.domain import DomainError, Model
from mgar_v3.quality_eval.v35 import runner
from mgar_v3.quality_eval.v35.ablations import ArmContext, ArmContextBuilder, ArmSpec
from mgar_v3.quality_eval.v35.tasks import Task
from mgar_v3.repair.providers.structured import (
    ModelPricing,
    ModelUsage,
    StructuredPatchProvider,
)

AUTHORIZATION_SCHEMA = "mgar-v35-live-authorization-v1"
DEFAULT_ENDPOINT = "https://api.deepseek.com/v1/chat/completions"
DEFAULT_MODEL = "deepseek-chat"
ALLOWED_HOST = "api.deepseek.com"
DEFAULT_PRICING = ModelPricing(
    input_micros_per_million=1_000_000,
    output_micros_per_million=2_000_000,
)
_MAX_OUTPUT_TOKENS = 8192
_ENV_KEY = "MGAR_V3_DEEPSEEK_API_KEY"
_V3_ROOT = Path(__file__).resolve().parents[4]


class LiveAuthorization(Model):
    """Explicit, time-bounded, actor-scoped paid-call authorization."""

    schema_version: str = Field(alias="schema")
    approved_by: str
    actor: str
    approved_at: datetime
    expires_at: datetime
    allow_paid: bool
    model_id: str
    max_cost_micros: int = Field(ge=1)
    max_calls: int = Field(ge=1)
    endpoint: str = DEFAULT_ENDPOINT


class LiveBudget:
    """Process-wide paid-call and cost ledger shared by every arm provider."""

    def __init__(self) -> None:
        self.calls = 0
        self.cost_micros = 0


def _dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text or text.startswith("#") or "=" not in text:
            continue
        key, _, value = text.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def actor_from_environment() -> str:
    return os.environ.get("MGAR_V3_LOCAL_ACTOR") or _dotenv(_V3_ROOT / ".env").get(
        "MGAR_V3_LOCAL_ACTOR", "loz888"
    )


def load_api_key() -> str:
    key = os.environ.get(_ENV_KEY, "").strip()
    if key:
        return key
    key = _dotenv(_V3_ROOT / ".env").get(_ENV_KEY, "").strip()
    if not key:
        raise runner.LiveNotAuthorized("no DeepSeek API key is configured")
    return key


def _check_endpoint(endpoint: str) -> None:
    parsed = httpx.URL(endpoint)
    if parsed.scheme != "https" or parsed.host != ALLOWED_HOST:
        raise runner.LiveNotAuthorized(
            f"live endpoint must be https://{ALLOWED_HOST}/…, got a different host"
        )


def load_authorization(
    path: Path,
    *,
    actor: str,
    now: datetime | None = None,
) -> LiveAuthorization:
    """Validate an authorization file; every failure blocks paid calls."""

    if not path.is_file():
        raise runner.LiveNotAuthorized(f"authorization file is absent: {path}")
    try:
        authorization = LiveAuthorization.model_validate_json(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise runner.LiveNotAuthorized(
            f"authorization file is not valid JSON: {type(exc).__name__}"
        ) from None
    if authorization.schema_version != AUTHORIZATION_SCHEMA:
        raise runner.LiveNotAuthorized("authorization schema is not supported")
    if not authorization.allow_paid:
        raise runner.LiveNotAuthorized("authorization does not allow paid calls")
    if authorization.approved_by != actor or authorization.actor != actor:
        raise runner.LiveNotAuthorized(
            "authorization actor does not match the configured local actor"
        )
    if authorization.model_id != DEFAULT_MODEL:
        raise runner.LiveNotAuthorized(f"authorization model must be {DEFAULT_MODEL}")
    _check_endpoint(authorization.endpoint)
    moment = now or datetime.now(UTC)
    approved_at = authorization.approved_at
    expires_at = authorization.expires_at
    if approved_at.tzinfo is None:
        approved_at = approved_at.replace(tzinfo=UTC)
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if moment < approved_at:
        raise runner.LiveNotAuthorized("authorization is not valid yet")
    if moment >= expires_at:
        raise runner.LiveNotAuthorized("authorization has expired")
    return authorization


class DeepSeekCompletion:
    """One raw chat-completion call behind a shared hard budget."""

    def __init__(
        self,
        *,
        api_key: str,
        authorization: LiveAuthorization,
        budget: LiveBudget,
        pricing: ModelPricing = DEFAULT_PRICING,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not api_key:
            raise runner.LiveNotAuthorized("an API key is required for a live call")
        self._api_key = api_key
        self.authorization = authorization
        self.budget = budget
        self.pricing = pricing
        self._clock = clock
        self._client = httpx.Client(
            timeout=httpx.Timeout(180.0, connect=30.0),
            transport=transport,
        )

    @property
    def calls(self) -> int:
        return self.budget.calls

    @property
    def cost_micros(self) -> int:
        return self.budget.cost_micros

    def _guard(self) -> None:
        if self.budget.calls >= self.authorization.max_calls:
            raise DomainError(
                "LIVE_CALL_BUDGET",
                "the paid-call cap in the authorization file is exhausted",
                429,
            )
        if self.budget.cost_micros >= self.authorization.max_cost_micros:
            raise DomainError(
                "LIVE_COST_BUDGET",
                "the paid-cost cap in the authorization file is exhausted",
                429,
            )

    def complete(self, *, system: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        """Send exactly one accounted request; never retry inside this method."""

        self._guard()
        body = {
            "model": self.authorization.model_id,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": _encode(payload)},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0,
            "max_tokens": _MAX_OUTPUT_TOKENS,
            "stream": False,
        }
        self.budget.calls += 1
        started = self._clock()
        try:
            response = self._client.post(
                self.authorization.endpoint,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                json=body,
            )
        except httpx.HTTPError as exc:
            raise DomainError(
                "USAGE_UNKNOWN",
                f"transport failed after dispatch ({type(exc).__name__}); "
                "the paid outcome is unknown and must not be resent",
                502,
            ) from None
        latency_ms = round((self._clock() - started) * 1000, 3)
        if response.status_code != 200:
            raise DomainError(
                f"MODEL_HTTP_{response.status_code}",
                "the live provider returned a non-success status",
                502,
            )
        try:
            data = response.json()
        except ValueError:
            raise DomainError(
                "USAGE_UNKNOWN",
                "the live reply is not JSON; the paid outcome cannot be accounted",
                502,
            ) from None
        usage = data.get("usage")
        if not isinstance(usage, Mapping):
            raise DomainError(
                "USAGE_UNKNOWN",
                "the live reply did not report token usage",
                502,
            )
        prompt_tokens = usage.get("prompt_tokens")
        completion_tokens = usage.get("completion_tokens")
        if (
            type(prompt_tokens) is not int
            or type(completion_tokens) is not int
            or prompt_tokens < 0
            or completion_tokens < 0
        ):
            raise DomainError(
                "USAGE_UNKNOWN",
                "the live reply reported invalid token usage",
                502,
            )
        cost = self.pricing.cost_micros(
            ModelUsage(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens)
        )
        self.budget.cost_micros += cost
        if self.budget.cost_micros > self.authorization.max_cost_micros:
            raise DomainError(
                "LIVE_COST_BUDGET",
                "the paid-cost cap was exceeded; spending is recorded and stopped",
                429,
            )
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise DomainError("MODEL_REPLY_EMPTY", "the live reply has no choice", 502)
        choice = choices[0] if isinstance(choices[0], Mapping) else {}
        message = choice.get("message") if isinstance(choice, Mapping) else None
        content = message.get("content") if isinstance(message, Mapping) else None
        finish = choice.get("finish_reason") if isinstance(choice, Mapping) else None
        return {
            "content": content,
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "cost_micros": cost,
            },
            "finish_reason": finish,
            "latency_ms": latency_ms,
            "model": data.get("model"),
        }


def _encode(payload: Mapping[str, Any]) -> str:
    import json

    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


class AblationLiveProvider(StructuredPatchProvider):
    """Structured provider whose planner input carries one arm's context."""

    def __init__(
        self,
        model: Any,
        *,
        task: Task,
        arm: ArmSpec,
        pricing: ModelPricing = DEFAULT_PRICING,
    ) -> None:
        super().__init__(model, pricing=pricing)
        self.builder = ArmContextBuilder(task, arm)
        self._contexts: list[ArmContext] = []

    @property
    def contexts(self) -> tuple[ArmContext, ...]:
        return tuple(self._contexts)

    def _payload(self, prompt: Any) -> dict[str, Any]:
        payload = super()._payload(prompt)
        context = self.builder.build(prompt)
        self._contexts.append(context)
        payload["ablation"] = context.payload
        return payload

    def usage(self) -> dict[str, Any]:
        stats = self.stats()
        return {
            "model_calls": stats.calls,
            "prompt_tokens": stats.prompt_tokens,
            "completion_tokens": stats.completion_tokens,
            "cost_micros": stats.cost_micros,
            "usage_source": "actual_provider_usage",
        }


def make_provider_factory(
    *,
    authorization: LiveAuthorization,
    api_key: str | None = None,
    pricing: ModelPricing = DEFAULT_PRICING,
) -> Callable[[Task, ArmSpec], AblationLiveProvider]:
    """Build one factory with a single shared paid-call ledger."""

    key = api_key or load_api_key()
    budget = LiveBudget()
    model = DeepSeekCompletion(
        api_key=key,
        authorization=authorization,
        budget=budget,
        pricing=pricing,
    )

    def factory(task: Task, arm: ArmSpec) -> AblationLiveProvider:
        return AblationLiveProvider(model, task=task, arm=arm, pricing=pricing)

    return factory
