"""Structured model patch provider (V3.5 R01).

The model may only answer with one strict JSON object describing full-file
edits.  Every edit carries the pre-image SHA-256 of the version the model was
allowed to see, so a stale plan is rejected instead of being applied.  Diffs,
hunks, line numbers, shell commands and new dependency manifests are never
accepted as change facts; the trusted worktree owns application and review
output.

A paid call whose usage or outcome is unknown is recorded once and never
resent, because repeating an unaccounted paid tool call would double spend.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Annotated, Any, Literal, NoReturn, Protocol

from pydantic import Field

from mgar_v3.context.builder import (
    ContextBuilder,
    RepairContextRequest,
)
from mgar_v3.domain import DomainError, Model
from mgar_v3.repair.repair_agent import (
    PatchPlan,
    PatchProposalRejected,
    RepairPrompt,
)
from mgar_v3.repair.worktree import (
    FileEdit,
    is_protected_path,
    line_delta,
    sha256_text,
    validate_patch_content,
    validate_patch_path,
)

_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_COMMAND_KEYS = frozenset({"command", "commands", "cmd", "shell", "argv", "exec", "script"})
_DIFF_KEYS = frozenset({"diff", "patch", "unified_diff", "hunks"})
_ENVELOPE_KEYS = frozenset({"summary", "edits"})
_EDIT_KEYS = frozenset({"path", "operation", "expected_sha256", "content"})
_OPERATIONS = frozenset({"update", "create", "delete"})
_PARSE_ERRORS = frozenset({"PATCH_FORMAT", "PATCH_TRUNCATED"})
_UNKNOWN_CODES = frozenset({"PATCH_OUTCOME_UNKNOWN", "PATCH_USAGE_MISSING"})

SYSTEM_PROMPT = (
    "You are a bounded repair planner. Return exactly one JSON object with keys "
    '"summary" and "edits". "summary" is a short string. "edits" is a non-empty list. '
    'Each edit has exactly "path", "operation", "expected_sha256" and "content". '
    '"operation" is update, create or delete. "content" is the complete replacement '
    "file text, never a diff, hunk, line number or patch fragment. update and delete "
    "must repeat the supplied sha256 for that path; create must omit it and is only "
    'valid for an absent path. Never output "command", "shell", "argv", "diff" or '
    '"patch" keys and never ask to run a program. Edit only paths listed in '
    "allowed_paths. Never modify dependency manifests, configuration, hidden paths "
    "or files outside allowed_paths. If feedback lists a rejection, correct it "
    "instead of repeating the same edit. Return JSON only, without Markdown fences."
)


class ModelUsage(Model):
    """Provider-reported token usage for one call; cost may be derived."""

    prompt_tokens: Annotated[int, Field(ge=0)]
    completion_tokens: Annotated[int, Field(ge=0)]
    cost_micros: Annotated[int, Field(ge=0)] | None = None


class ModelPricing(Model):
    """Conservative per-million-token prices used when the reply has no cost."""

    input_micros_per_million: Annotated[int, Field(ge=0)]
    output_micros_per_million: Annotated[int, Field(ge=0)]

    def cost_micros(self, usage: ModelUsage) -> int:
        numerator = (
            usage.prompt_tokens * self.input_micros_per_million
            + usage.completion_tokens * self.output_micros_per_million
        )
        return (numerator + 999_999) // 1_000_000


class ProviderCall(Model):
    """One redacted call record: no prompt, source or model text is retained."""

    attempt: Annotated[int, Field(ge=1, le=10)]
    status: Literal["accepted", "rejected"]
    code: str
    prompt_tokens: Annotated[int, Field(ge=0)] | None = None
    completion_tokens: Annotated[int, Field(ge=0)] | None = None
    cost_micros: Annotated[int, Field(ge=0)] | None = None
    latency_ms: Annotated[float, Field(ge=0)] | None = None
    response_sha256: str | None = None
    edits: Annotated[int, Field(ge=0)] = 0
    retryable: bool = True


class ProviderStats(Model):
    """Aggregated paid-call, token, cost and structured-success accounting."""

    calls: Annotated[int, Field(ge=0)] = 0
    accepted: Annotated[int, Field(ge=0)] = 0
    rejected: Annotated[int, Field(ge=0)] = 0
    parse_errors: Annotated[int, Field(ge=0)] = 0
    unknown_outcomes: Annotated[int, Field(ge=0)] = 0
    prompt_tokens: Annotated[int, Field(ge=0)] = 0
    completion_tokens: Annotated[int, Field(ge=0)] = 0
    cost_micros: Annotated[int, Field(ge=0)] = 0

    @property
    def success_rate(self) -> float:
        return self.accepted / self.calls if self.calls else 0.0


class RepairModel(Protocol):
    """A single raw completion call behind the Engine's paid-call ledger."""

    def complete(self, *, system: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        """Return content, usage, model, latency and finish_reason for one call."""
        ...


def _sha_or_none(value: Any) -> str | None:
    return sha256_text(value) if isinstance(value, str) else None


def _count(value: Any) -> int | None:
    if type(value) is int and value >= 0:
        return value
    return None


class StructuredPatchProvider:
    """Validate model JSON into a PatchPlan of authorized full-file edits."""

    def __init__(
        self,
        model: RepairModel,
        *,
        pricing: ModelPricing | None = None,
        max_prompt_bytes: int = 262_144,
    ) -> None:
        if max_prompt_bytes < 1:
            raise ValueError("max_prompt_bytes must be positive")
        self.model = model
        self.pricing = pricing
        self.max_prompt_bytes = max_prompt_bytes
        self._calls: list[ProviderCall] = []
        self._unknown: set[tuple[str, str, int]] = set()

    # -- public surface -------------------------------------------------

    @property
    def calls(self) -> tuple[ProviderCall, ...]:
        return tuple(self._calls)

    def stats(self) -> ProviderStats:
        calls = self._calls
        return ProviderStats(
            calls=len(calls),
            accepted=sum(item.status == "accepted" for item in calls),
            rejected=sum(item.status == "rejected" for item in calls),
            parse_errors=sum(item.code in _PARSE_ERRORS for item in calls),
            unknown_outcomes=sum(item.code in _UNKNOWN_CODES for item in calls),
            prompt_tokens=sum(item.prompt_tokens or 0 for item in calls),
            completion_tokens=sum(item.completion_tokens or 0 for item in calls),
            cost_micros=sum(item.cost_micros or 0 for item in calls),
        )

    def propose(self, prompt: RepairPrompt) -> PatchPlan:
        key = (prompt.repository, prompt.objective, prompt.attempt)
        if key in self._unknown:
            raise PatchProposalRejected(
                "PATCH_OUTCOME_UNKNOWN",
                "A previous model call for this attempt has an unknown outcome; "
                "it will not be sent again",
                retryable=False,
            )
        if not prompt.allowed_paths:
            raise PatchProposalRejected(
                "PATCH_PATH_DENIED",
                "Repair prompt has no authorized paths",
                retryable=False,
            )
        try:
            payload = self._payload(prompt)
        except DomainError as exc:
            raise PatchProposalRejected(
                "PATCH_INPUT_LIMIT",
                str(exc)[:1000] or "Repair prompt exceeds the model input bound",
                retryable=False,
            ) from None
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if len(encoded.encode()) > self.max_prompt_bytes:
            raise PatchProposalRejected(
                "PATCH_INPUT_LIMIT",
                "Repair prompt exceeds the model input bound",
                retryable=False,
            )
        try:
            reply = self.model.complete(system=SYSTEM_PROMPT, payload=payload)
        except DomainError as exc:
            if exc.code == "USAGE_UNKNOWN":
                self._unknown.add(key)
                self._record(
                    attempt=prompt.attempt,
                    status="rejected",
                    code="PATCH_OUTCOME_UNKNOWN",
                    retryable=False,
                )
                raise PatchProposalRejected(
                    "PATCH_OUTCOME_UNKNOWN",
                    "The model call outcome is unknown; refusing to repeat it",
                    retryable=False,
                ) from None
            self._record(
                attempt=prompt.attempt,
                status="rejected",
                code=exc.code,
                retryable=False,
            )
            raise PatchProposalRejected(
                exc.code, str(exc)[:1000] or "Model call failed", retryable=False
            ) from None

        content = reply.get("content")
        usage = self._usage(reply.get("usage"))
        record = self._record_fields(usage, reply, content)
        if usage is None:
            self._unknown.add(key)
            return self._reject(
                prompt,
                "PATCH_USAGE_MISSING",
                "Model reply did not report token usage; the paid call cannot be accounted",
                record,
                retryable=False,
            )
        finish = reply.get("finish_reason")
        if finish == "length":
            return self._reject(
                prompt, "PATCH_TRUNCATED", "Model output reached its length limit", record
            )
        if not isinstance(content, str) or not content.strip():
            return self._reject(
                prompt, "PATCH_FORMAT", "Model reply did not contain a JSON object", record
            )
        raw = self._json_object(content, prompt, record)
        return self._plan(raw, prompt, record)

    # -- prompt construction -------------------------------------------

    def _payload(self, prompt: RepairPrompt) -> dict[str, Any]:
        """Build the bounded payload through the shared ContextBuilder policy.

        Investigation and Repair therefore share one budget, truncation and
        invariant implementation.  The hard repair constraints (allowed paths,
        attempt, response schema and every pre-image sha256) stay pinned while
        only file previews and feedback may shrink.
        """

        bundle = ContextBuilder(
            max_tokens=max(512, self.max_prompt_bytes // 3)
        ).build_repair(
            RepairContextRequest(
                repository=prompt.repository,
                objective=prompt.objective,
                language=prompt.language,
                attempt=prompt.attempt,
                max_attempts=prompt.max_attempts,
                allowed_paths=list(prompt.allowed_paths),
                files=dict(prompt.files),
                feedback=[
                    item.model_dump(mode="json") for item in prompt.feedback
                ],
                response_schema={
                    "summary": "string",
                    "edits": [
                        {
                            "path": "one allowed path",
                            "operation": "update|create|delete",
                            "expected_sha256": "latest sha256 for update/delete",
                            "content": "complete replacement text",
                        }
                    ],
                },
                max_bytes=self.max_prompt_bytes,
            )
        )
        return bundle.payload

    # -- reply parsing --------------------------------------------------

    def _usage(self, value: Any) -> ModelUsage | None:
        if not isinstance(value, Mapping):
            return None
        prompt_tokens = _count(value.get("prompt_tokens"))
        completion_tokens = _count(value.get("completion_tokens"))
        if prompt_tokens is None or completion_tokens is None:
            return None
        cost = _count(value.get("cost_micros"))
        usage = ModelUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_micros=cost,
        )
        if usage.cost_micros is None and self.pricing is not None:
            usage = usage.model_copy(update={"cost_micros": self.pricing.cost_micros(usage)})
        return usage

    def _record_fields(
        self, usage: ModelUsage | None, reply: Mapping[str, Any], content: Any
    ) -> dict[str, Any]:
        latency_value = reply.get("latency_ms")
        latency: float | None = None
        if (
            isinstance(latency_value, (int, float))
            and not isinstance(latency_value, bool)
            and latency_value >= 0
        ):
            latency = float(latency_value)
        return {
            "prompt_tokens": usage.prompt_tokens if usage else None,
            "completion_tokens": usage.completion_tokens if usage else None,
            "cost_micros": usage.cost_micros if usage else None,
            "latency_ms": latency,
            "response_sha256": _sha_or_none(content),
        }

    def _json_object(
        self, content: str, prompt: RepairPrompt, record: dict[str, Any]
    ) -> dict[str, Any]:
        text = content.strip()
        if text.startswith("```"):
            return self._reject(
                prompt, "PATCH_FORMAT", "Markdown fences are not accepted", record
            )
        try:
            value = json.loads(text)
        except json.JSONDecodeError as exc:
            return self._reject(
                prompt,
                "PATCH_FORMAT",
                f"Model reply is not valid JSON at line {exc.lineno} column {exc.colno}",
                record,
            )
        if not isinstance(value, dict):
            return self._reject(
                prompt, "PATCH_FORMAT", "Model reply must be a JSON object", record
            )
        if any(key in value for key in _COMMAND_KEYS):
            return self._reject(
                prompt,
                "PATCH_COMMAND_REJECTED",
                "Model reply may not carry shell or command fields",
                record,
            )
        if any(key in value for key in _DIFF_KEYS):
            return self._reject(
                prompt,
                "PATCH_DIFF_UNSUPPORTED",
                "Unified diff output is review-only and cannot be applied as a change",
                record,
            )
        extra = sorted(set(value) - _ENVELOPE_KEYS)
        if extra:
            return self._reject(
                prompt,
                "PATCH_FORMAT",
                "Model reply has unsupported keys: " + ", ".join(extra[:8]),
                record,
            )
        return value

    # -- plan validation ------------------------------------------------

    def _plan(
        self, raw: Mapping[str, Any], prompt: RepairPrompt, record: dict[str, Any]
    ) -> PatchPlan:
        limits = prompt.limits
        summary = raw.get("summary")
        if not isinstance(summary, str) or not summary.strip() or len(summary) > 2000:
            return self._reject(
                prompt, "PATCH_FORMAT", "summary must be a non-empty bounded string", record
            )
        edits = raw.get("edits")
        if not isinstance(edits, list) or not edits:
            return self._reject(
                prompt, "PATCH_FORMAT", "edits must be a non-empty list", record
            )
        if len(edits) > min(16, limits.max_files):
            return self._reject(
                prompt, "PATCH_LIMIT", "Patch changes too many files", record
            )
        seen: set[str] = set()
        built: list[FileEdit] = []
        deleted = 0
        added_lines = removed_lines = total_bytes = 0
        for item in edits:
            if not isinstance(item, Mapping):
                return self._reject(
                    prompt, "PATCH_FORMAT", "Each edit must be a JSON object", record
                )
            if any(key in item for key in _COMMAND_KEYS):
                return self._reject(
                    prompt,
                    "PATCH_COMMAND_REJECTED",
                    "Edits may not carry shell or command fields",
                    record,
                )
            if any(key in item for key in _DIFF_KEYS):
                return self._reject(
                    prompt,
                    "PATCH_DIFF_UNSUPPORTED",
                    "Edits must carry full replacement content, not a diff",
                    record,
                )
            extra = sorted(set(item) - _EDIT_KEYS)
            if extra:
                return self._reject(
                    prompt,
                    "PATCH_FORMAT",
                    "Edit has unsupported keys: " + ", ".join(extra[:8]),
                    record,
                )
            try:
                edit, added, removed, written = self._edit(item, prompt, seen)
            except DomainError as exc:
                return self._reject(prompt, exc.code, str(exc), record)
            seen.add(edit.path)
            built.append(edit)
            added_lines += added
            removed_lines += removed
            total_bytes += written
            deleted += edit.operation == "delete"

        if deleted > limits.max_deleted_files:
            return self._reject(
                prompt, "PATCH_DELETE_DENIED", "Patch deletes more files than allowed", record
            )
        if added_lines + removed_lines > limits.max_changed_lines:
            return self._reject(
                prompt, "PATCH_LIMIT", "Patch changes too many lines", record
            )
        if total_bytes > limits.max_total_bytes:
            return self._reject(
                prompt, "PATCH_LIMIT", "Patch exceeds the total byte budget", record
            )
        plan = PatchPlan(summary=summary.strip(), edits=built)
        self._record(
            attempt=prompt.attempt,
            status="accepted",
            code="PATCH_ACCEPTED",
            edits=len(built),
            **record,
        )
        return plan

    def _edit(
        self, item: Mapping[str, Any], prompt: RepairPrompt, seen: set[str]
    ) -> tuple[FileEdit, int, int, int]:
        limits = prompt.limits
        path_value = item.get("path")
        if not isinstance(path_value, str) or not path_value:
            raise DomainError("PATCH_FORMAT", "Edit path must be a non-empty string", 400)
        path = validate_patch_path(path_value)
        if path in seen:
            raise DomainError("PATCH_FORMAT", "Patch edits the same path twice", 400)
        if path not in prompt.allowed_paths:
            raise DomainError(
                "PATCH_PATH_DENIED", f"{path!r} is not an authorized repair path", 403
            )
        if is_protected_path(path, limits.protected_globs):
            raise DomainError(
                "PATCH_PROTECTED", f"{path!r} is a protected configuration file", 403
            )
        operation = item.get("operation")
        if operation not in _OPERATIONS:
            raise DomainError("PATCH_FORMAT", "Edit operation is not supported", 400)
        before = prompt.files.get(path)
        expected = item.get("expected_sha256")
        content = item.get("content")
        if operation == "create":
            if not limits.allow_new_files:
                raise DomainError("PATCH_CREATE_DENIED", "New files are disabled", 403)
            if before is not None:
                raise DomainError("PATCH_CONFLICT", f"{path!r} already exists", 409)
            if expected is not None:
                raise DomainError(
                    "PATCH_FORMAT", "Create edits must not declare a pre-image hash", 400
                )
            if not isinstance(content, str):
                raise DomainError("PATCH_FORMAT", "Create edits require content", 400)
            validate_patch_content(path, content, limits)
            added, removed = line_delta("", content)
            return (
                FileEdit(path=path, operation="create", content=content),
                added,
                removed,
                len(content.encode()),
            )

        if before is None:
            raise DomainError("PATCH_NOT_FOUND", f"{path!r} does not exist", 404)
        if not isinstance(expected, str) or not _SHA256.fullmatch(expected):
            raise DomainError(
                "PATCH_FORMAT", "Update and delete edits require a SHA-256 pre-image", 400
            )
        actual = sha256_text(before)
        if expected != actual:
            raise DomainError(
                "PATCH_STALE",
                f"{path!r} changed since it was read; expected {actual}",
                409,
            )
        if operation == "delete":
            if content is not None:
                raise DomainError("PATCH_FORMAT", "Delete edits cannot carry content", 400)
            added, removed = line_delta(before, "")
            return (
                FileEdit(path=path, operation="delete", expected_sha256=expected),
                added,
                removed,
                0,
            )
        if not isinstance(content, str):
            raise DomainError("PATCH_FORMAT", "Update edits require content", 400)
        validate_patch_content(path, content, limits)
        added, removed = line_delta(before, content)
        return (
            FileEdit(
                path=path,
                operation="update",
                content=content,
                expected_sha256=expected,
            ),
            added,
            removed,
            len(content.encode()),
        )

    # -- accounting -----------------------------------------------------

    def _record(self, **values: Any) -> None:
        self._calls.append(ProviderCall(**values))

    def _reject(
        self,
        prompt: RepairPrompt,
        code: str,
        message: str,
        record: dict[str, Any],
        *,
        retryable: bool = True,
    ) -> NoReturn:
        self._record(
            attempt=prompt.attempt,
            status="rejected",
            code=code,
            retryable=retryable,
            **record,
        )
        raise PatchProposalRejected(code, message[:1000], retryable=retryable)
