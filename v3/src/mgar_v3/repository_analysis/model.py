"""DeepSeek JSON-mode client constrained by the repository-analysis ledger."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any, Protocol, TypeVar, cast
from uuid import UUID

import httpx
from pydantic import ValidationError

from mgar_v3.domain import DomainError, Model
from mgar_v3.rate_limit import acquire
from mgar_v3.repository_analysis.domain import AnalysisModelConfig
from mgar_v3.repository_analysis.streaming import PreviewRelay, read_completion
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
        call_limit: int | None,
        token_reservation: int = 0,
    ) -> UUID: ...

    def settle_call(
        self,
        call_id: UUID,
        cost_micros: int | None,
        usage: dict[str, Any] | None = None,
        result: Model | None = None,
    ) -> None: ...

    def record_model_input(
        self, run_id: UUID, epoch: int, step_key: str, body: dict[str, Any]
    ) -> None: ...

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
        call_limit: int | None,
        deadline_check: Callable[[], None],
    ) -> T:
        key = self.settings.deepseek_api_key
        if key is None:
            raise DomainError("MODEL_CREDENTIAL_REQUIRED", "DeepSeek credential is missing", 403)
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        messages = [{"role": "system", "content": system}, {"role": "user", "content": encoded}]
        phase = "analysis" if schema.__name__ == "AgentDecision" else "validation"

        def emit(kind: str, **data: Any) -> None:
            recorder = getattr(ledger, "stream_event", None)
            if callable(recorder):
                recorder(run_id, epoch, kind, **data)

        for attempt in range(3):
            attempt_key = step_key + ":retry" * attempt
            existing = ledger.repository_model_result(
                run_id, epoch, attempt_key, profile_hash, schema
            )
            if existing is not None:
                return existing
            deadline_check()
            input_bound = sum(len(item["content"].encode()) for item in messages) + 1024
            maximum_output = self.config.max_output_tokens
            spec_reader = getattr(ledger, "spec", None)
            if callable(spec_reader):
                spec = spec_reader(run_id)
                if input_bound > spec.limits.max_input_tokens - spec.input_tokens_used:
                    raise DomainError("INPUT_TOKEN_BUDGET", "Input token budget exhausted")
                if spec.limits.max_total_tokens is not None:
                    remaining = (
                        spec.limits.max_total_tokens
                        - spec.input_tokens_used
                        - spec.output_tokens_used
                        - spec.reserved_tokens
                    )
                    maximum_output = min(maximum_output, remaining - input_bound)
                    if maximum_output < 512:
                        raise DomainError("TOKEN_BUDGET_EXHAUSTED", "Token budget exhausted")
            if input_bound + maximum_output > self.config.context_tokens:
                raise DomainError("MODEL_INPUT_LIMIT", "Request exceeds the model context window")
            reservation = (
                input_bound * self.config.input_micros_per_million
                + maximum_output * self.config.output_micros_per_million
                + 999999
            ) // 1000000
            body = {
                "model": self.config.model_id,
                "max_tokens": maximum_output,
                "temperature": 0,
                "response_format": {"type": "json_object"},
                "stream": True,
                "stream_options": {"include_usage": True},
                "messages": messages,
            }
            ledger.record_model_input(run_id, epoch, attempt_key, body)
            for waiting in range(13):
                try:
                    self.rate_limit(
                        self.settings.redis_url.get_secret_value(),
                        "repo-model:" + profile_hash + ":" + str(run_id),
                    )
                    break
                except DomainError as exc:
                    if exc.code != "RATE_LIMITED":
                        raise
                    if waiting == 12:
                        raise DomainError(
                            "MODEL_RATE_LIMITED", "Model service is temporarily unavailable"
                        ) from None
                    deadline_check()
                    if callable(spec_reader):
                        current = spec_reader(run_id)
                        if current.run_status != "running" or current.execution_epoch != epoch:
                            raise DomainError(
                                "FENCED", "Analysis was paused or cancelled"
                            ) from None
                    if waiting == 0:
                        emit("analysis_waiting")
                    time.sleep(5)
            try:
                client = self.client or httpx.Client(timeout=60)
            except (OSError, httpx.HTTPError):
                raise DomainError(
                    "MODEL_CLIENT_UNAVAILABLE", "Model client is unavailable"
                ) from None
            try:
                call_id = ledger.reserve_repository_call(
                    run_id,
                    epoch,
                    attempt_key,
                    profile_hash,
                    reservation,
                    call_limit,
                    token_reservation=input_bound + maximum_output,
                )
                emit("model_stream_started", step_key=attempt_key, phase=phase)

                def send_preview(
                    field: str,
                    text: str,
                    offset: int,
                    step: str = attempt_key,
                ) -> None:
                    emit(
                        "answer_delta",
                        step_key=step,
                        field=field,
                        text=text,
                        offset=offset,
                    )

                relay = PreviewRelay(send_preview)
                try:
                    with client.stream(
                        "POST",
                        "https://api.deepseek.com/v1/chat/completions",
                        json=body,
                        headers={"Authorization": "Bearer " + key.get_secret_value()},
                    ) as response:
                        if response.status_code in {400, 401, 403, 404, 422, 429}:
                            ledger.record_model_response(
                                call_id, response.status_code, response.read()
                            )
                            ledger.settle_call(call_id, 0)
                            if response.status_code == 429 and attempt < 2:
                                try:
                                    delay = float(response.headers.get("Retry-After", "1"))
                                except ValueError:
                                    delay = 1.0
                                if delay > 10:
                                    raise DomainError(
                                        "MODEL_RATE_LIMITED", "Provider is rate limited"
                                    )
                                time.sleep(max(0, delay))
                                continue
                            raise DomainError(
                                "MODEL_PROVIDER_ERROR", "Model provider rejected request"
                            )
                        if not response.is_success:
                            ledger.record_model_response(
                                call_id, response.status_code, response.read()
                            )
                            ledger.settle_call(call_id, None)
                            raise DomainError("USAGE_UNKNOWN", "Provider outcome is unknown")
                        value, raw = read_completion(response, relay.feed, deadline_check)
                        if "text/event-stream" not in response.headers.get("content-type", ""):
                            try:
                                relay.feed(value["choices"][0]["message"]["content"])
                            except (KeyError, IndexError, TypeError):
                                pass
                        relay.flush()
                        ledger.record_model_response(call_id, response.status_code, raw)
                except (httpx.ConnectError, httpx.ConnectTimeout):
                    ledger.settle_call(call_id, 0)
                    if attempt < 2:
                        time.sleep(0.25 * (attempt + 1))
                        continue
                    raise DomainError(
                        "MODEL_CLIENT_UNAVAILABLE", "Cannot connect to provider"
                    ) from None
                except (httpx.HTTPError, ValueError, OSError):
                    ledger.settle_call(call_id, None)
                    raise DomainError(
                        "USAGE_UNKNOWN", "Stream ended without a usage receipt"
                    ) from None
                except DomainError as exc:
                    if exc.code == "DEADLINE_EXCEEDED":
                        ledger.settle_call(call_id, None)
                        raise DomainError("USAGE_UNKNOWN", "Stream deadline exceeded") from None
                    raise
                usage = value.get("usage")
                if (
                    value.get("model") != self.config.model_id
                    or not isinstance(usage, dict)
                    or any(
                        type(usage.get(k)) is not int or usage[k] < 0
                        for k in ("prompt_tokens", "completion_tokens")
                    )
                ):
                    ledger.settle_call(call_id, None)
                    raise DomainError("USAGE_UNKNOWN", "Model usage is unavailable")
                cost = (
                    usage["prompt_tokens"] * self.config.input_micros_per_million
                    + usage["completion_tokens"] * self.config.output_micros_per_million
                    + 999999
                ) // 1000000
                if (
                    cost > reservation
                    or usage["prompt_tokens"] > input_bound
                    or usage["completion_tokens"] > maximum_output
                ):
                    ledger.settle_call(call_id, None)
                    raise DomainError("USAGE_UNKNOWN", "Provider exceeded the reserved price bound")
                if not getattr(ledger, "accounts_usage", False):
                    ledger.note_input_tokens(run_id, cast(int, usage["prompt_tokens"]))
                try:
                    choice = value["choices"][0]
                    if choice.get("finish_reason") == "length":
                        raise ValueError("Truncated output")
                    result = schema.model_validate_json(choice["message"]["content"])
                except (KeyError, IndexError, TypeError, ValueError, ValidationError):
                    ledger.settle_call(call_id, cost, usage)
                    emit("draft_rejected", reason="MODEL_INVALID_RESPONSE")
                    if attempt < 2:
                        messages = messages[:2] + [
                            {
                                "role": "user",
                                "content": (
                                    "Return one complete JSON object matching the schema. "
                                    "Use every required field. "
                                    + (
                                        "An answer needs key_files and findings with exact quotes."
                                        if phase == "analysis"
                                        else "Use accepted, missing_requirements and summary only."
                                    )
                                ),
                            }
                        ]
                        continue
                    raise DomainError(
                        "MODEL_INVALID_RESPONSE", "Output failed schema validation"
                    ) from None
                ledger.settle_call(call_id, cost, usage, result)
                return result
            finally:
                if self.client is None:
                    client.close()
        raise DomainError("MODEL_INVALID_RESPONSE", "Model retry policy stopped the request")
