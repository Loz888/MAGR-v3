"""DeepSeek JSON-mode client constrained by the repository-analysis ledger."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, Protocol, TypeVar, cast
from uuid import UUID

import httpx
from pydantic import ValidationError

from mgar_v3.domain import DomainError, Model
from mgar_v3.rate_limit import acquire
from mgar_v3.repository_analysis.domain import AnalysisModelConfig
from mgar_v3.settings import Settings

T = TypeVar("T", bound=Model)


class AnalysisLedger(Protocol):
    def repository_model_result(
        self,
        run_id: UUID,
        epoch: int,
        step_key: str,
        profile_hash: str,
        schema: type[T],
    ) -> T | None: ...

    def reserve_repository_call(
        self,
        run_id: UUID,
        epoch: int,
        step_key: str,
        profile_hash: str,
        reservation_micros: int,
        call_limit: int,
    ) -> UUID: ...

    def settle_call(
        self,
        call_id: UUID,
        cost_micros: int | None,
        usage: dict[str, Any] | None = None,
        result: Model | None = None,
    ) -> None: ...

    def record_model_input(self, run_id: UUID, epoch: int, step_key: str, body: dict[str, Any]) -> None: ...

    def record_model_response(self, call_id: UUID, status: int, body: bytes) -> None: ...

    def note_input_tokens(self, run_id: UUID, tokens: int) -> None: ...


class DeepSeekAnalysisModel:
    def __init__(
        self,
        settings: Settings,
        config: AnalysisModelConfig,
        *,
        client: httpx.Client | None = None,
        rate_limit: Any = acquire,
    ):
        self.settings = settings
        self.config = config
        self.client = client
        self.rate_limit = rate_limit

    def complete(
        self,
        ledger: AnalysisLedger,
        run_id: UUID,
        epoch: int,
        step_key: str,
        profile_hash: str,
        system: str,
        payload: dict[str, Any],
        schema: type[T],
        *,
        call_limit: int,
        deadline_check: Callable[[], None],
    ) -> T:
        key = self.settings.deepseek_api_key
        if key is None:
            raise DomainError("MODEL_CREDENTIAL_REQUIRED", "DeepSeek credential is missing", 403)
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if len((system + encoded).encode()) + self.config.max_output_tokens + 1024 > (
            self.config.context_tokens
        ):
            raise DomainError(
                "MODEL_INPUT_LIMIT", "Repository analysis request exceeds its context bound"
            )
        body = {
            "model": self.config.model_id,
            "max_tokens": self.config.max_output_tokens,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "stream": False,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": encoded},
            ],
        }
        for attempt in range(3):
            attempt_key = step_key if attempt == 0 else step_key + ":retry" * attempt
            existing = ledger.repository_model_result(
                run_id, epoch, attempt_key, profile_hash, schema
            )
            if existing is not None:
                return existing
            deadline_check()
            if attempt == 1:
                body = {
                    **body,
                    "messages": body["messages"]
                    + [
                        {
                            "role": "user",
                            "content": (
                                "Your previous reply was empty, truncated or failed the JSON "
                                "schema. Reply again with ONE compact JSON object: thought under "
                                "200 characters, at most 2 items per report section, quotes under "
                                "200 characters, no markdown, no prose outside the JSON. "
                                "action=insufficient requires report=null and a non-empty "
                                "insufficient_reason; action=tool requires report=null and at "
                                "least one tool_call."
                            ),
                        }
                    ],
                }
            ledger.record_model_input(run_id, epoch, attempt_key, body)
            self.rate_limit(
                self.settings.redis_url.get_secret_value(),
                "repo-model:" + profile_hash + ":" + str(run_id),
            )
            try:
                client = self.client or httpx.Client(timeout=60)
            except (OSError, httpx.HTTPError):
                raise DomainError(
                    "MODEL_CLIENT_UNAVAILABLE", "Model client setup failed before paid reservation"
                ) from None
            try:
                call_id = ledger.reserve_repository_call(
                    run_id,
                    epoch,
                    attempt_key,
                    profile_hash,
                    self.config.reservation_micros,
                    call_limit,
                )
                try:
                    response = client.post(
                        "https://api.deepseek.com/v1/chat/completions",
                        json=body,
                        headers={"Authorization": "Bearer " + key.get_secret_value()},
                    )
                    ledger.record_model_response(call_id, response.status_code, response.content)
                    value = response.json()
                except (httpx.HTTPError, ValueError, OSError):
                    ledger.settle_call(call_id, None)
                    raise DomainError(
                        "USAGE_UNKNOWN", "Repository analysis model outcome is unknown"
                    ) from None
                usage = value.get("usage") if isinstance(value, dict) else None
                if (not isinstance(value, dict) or value.get("model") != self.config.model_id) or (
                    not isinstance(usage, dict)
                    or any(
                        type(usage.get(k)) is not int or usage[k] < 0
                        for k in ("prompt_tokens", "completion_tokens")
                    )
                ):
                    ledger.settle_call(call_id, None)
                    raise DomainError(
                        "USAGE_UNKNOWN", "Repository analysis token usage is unavailable"
                    )
                cost = (
                    usage["prompt_tokens"] * self.config.input_micros_per_million
                    + usage["completion_tokens"] * self.config.output_micros_per_million
                    + 999999
                ) // 1000000
                if cost > self.config.reservation_micros:
                    ledger.settle_call(call_id, None)
                    raise DomainError(
                        "USAGE_UNKNOWN", "Repository analysis price bound exceeded"
                    )
                ledger.note_input_tokens(run_id, cast(int, usage["prompt_tokens"]))
                if not response.is_success:
                    ledger.settle_call(call_id, cost, usage)
                    raise DomainError(
                        "MODEL_PROVIDER_ERROR", "Repository analysis provider returned an error"
                    )
                try:
                    choice = value["choices"][0]
                    if choice.get("finish_reason") == "length":
                        raise ValueError("length")
                    result = schema.model_validate_json(choice["message"]["content"])
                except (KeyError, IndexError, TypeError, ValueError, ValidationError):
                    ledger.settle_call(call_id, cost, usage)
                    if attempt < 2:
                        continue
                    raise DomainError(
                        "MODEL_INVALID_RESPONSE", "Repository analysis output failed its schema"
                    ) from None
                ledger.settle_call(call_id, cost, usage, result)
                return result
            finally:
                if self.client is None:
                    client.close()
        raise DomainError(
            "MODEL_INVALID_RESPONSE", "Repository analysis output failed its schema"
        )
