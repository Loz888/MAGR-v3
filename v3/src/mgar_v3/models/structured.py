"""Single-attempt typed completion under the Engine's durable paid-call ledger."""

import json
from typing import Any, TypeVar
from uuid import UUID

import httpx
from pydantic import ValidationError

from mgar_v3.coordination.engine import Engine
from mgar_v3.domain import DomainError, Model
from mgar_v3.live import LivePermit
from mgar_v3.rate_limit import acquire

T = TypeVar("T", bound=Model)


class StructuredModel:
    def __init__(self, engine: Engine, permit: LivePermit, client: httpx.Client | None = None):
        self.engine, self.permit, self.client = engine, permit, client

    def complete(
        self,
        run_id: UUID,
        epoch: int,
        step: str,
        system: str,
        payload: dict[str, Any],
        schema: type[T],
    ) -> T:
        self.permit.validate()
        existing = self.engine.typed_model_result(run_id, epoch, step, self.permit, schema)
        if existing is not None:
            return existing
        p = self.permit.profile
        key = self.engine.settings.deepseek_api_key
        if key is None:
            raise DomainError("MODEL_CREDENTIAL_REQUIRED", "DeepSeek credential is missing", 403)
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if len((system + encoded).encode()) + p.max_output_tokens + 1024 > p.context_tokens:
            raise DomainError(
                "MODEL_INPUT_LIMIT", "Structured request exceeds its conservative context bound"
            )
        body = {
            "model": p.model_id,
            "max_tokens": p.max_output_tokens,
            "response_format": {"type": "json_object"},
            "stream": False,
            "thinking": {"type": "disabled"},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": encoded},
            ],
        }
        self.engine.record_model_input(run_id, epoch, step, body)
        acquire(
            self.engine.settings.redis_url.get_secret_value(),
            "model:" + p.profile_hash + ":" + str(run_id),
        )
        call_id = self.engine.reserve_call(run_id, epoch, step, self.permit)
        client = self.client or httpx.Client(timeout=45)
        try:
            try:
                response = client.post(
                    "https://api.deepseek.com/v1/chat/completions",
                    json=body,
                    headers={"Authorization": "Bearer " + key.get_secret_value()},
                )
                self.engine.record_model_response(call_id, response.status_code, response.content)
                value = response.json()
            except (httpx.HTTPError, ValueError):
                self.engine.settle_call(call_id, None)
                raise DomainError(
                    "USAGE_UNKNOWN", "Structured model request outcome is unknown"
                ) from None
            usage = value.get("usage") if isinstance(value, dict) else None
            if p.investigation_version == "r5-repair-v1" and (
                not isinstance(value, dict) or value.get("model") != p.model_id
            ):
                self.engine.settle_call(call_id, None)
                raise DomainError(
                    "USAGE_UNKNOWN", "Provider model differs from the frozen R5 price scope"
                )
            if not isinstance(usage, dict) or any(
                type(usage.get(k)) is not int or usage[k] < 0
                for k in ("prompt_tokens", "completion_tokens")
            ):
                self.engine.settle_call(call_id, None)
                raise DomainError("USAGE_UNKNOWN", "Structured model token usage is unavailable")
            cost = (
                usage["prompt_tokens"] * p.input_micros_per_million
                + usage["completion_tokens"] * p.output_micros_per_million
                + 999999
            ) // 1000000
            if cost > p.reservation_micros:
                self.engine.settle_call(call_id, None)
                raise DomainError("USAGE_UNKNOWN", "Structured model price bound exceeded")
            if not response.is_success:
                self.engine.settle_call(call_id, cost, usage)
                raise DomainError(
                    "MODEL_PROVIDER_ERROR", "Structured model provider returned an error"
                )
            try:
                choice = value["choices"][0]
                if choice.get("finish_reason") == "length":
                    raise ValueError("length")
                result = schema.model_validate_json(choice["message"]["content"])
            except (KeyError, IndexError, TypeError, ValueError, ValidationError):
                self.engine.settle_call(call_id, cost, usage)
                raise DomainError(
                    "MODEL_INVALID_RESPONSE", "Structured model output failed its schema"
                ) from None
            self.engine.settle_call(call_id, cost, usage, result)
            return result
        finally:
            if self.client is None:
                client.close()
