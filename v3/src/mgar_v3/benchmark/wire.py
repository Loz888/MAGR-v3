"""Explicit model transport contracts; resolve only predeclared exact quote spans."""

import json
from typing import Any, Literal

from pydantic import Field

from mgar_v3.benchmark.domain import Answer, Case, Citation
from mgar_v3.domain import DomainError, Model
from mgar_v3.repair.body import body_diff
from mgar_v3.repair.domain import Snapshot


def require_json_mode(system: str, payload: dict[str, Any]) -> None:
    # DeepSeek validates this before inference. Do it before recording/reserving a call.
    if "json" not in (system + json.dumps(payload, ensure_ascii=False)).lower():
        raise DomainError(
            "MODEL_JSON_MODE_INVALID", "JSON-object mode requires JSON in the supplied messages"
        )


class QuoteChoice(Model):
    quote_id: str = Field(pattern=r"^s[0-9]+q[0-9]+$")


class WireAnswer(Model):
    status: Literal["supported", "insufficient", "denied"]
    values: dict[str, str] = Field(default_factory=dict, max_length=12)
    citations: list[QuoteChoice] = Field(default_factory=list, max_length=6)
    explanation: str = Field(default="", max_length=800)


class WirePatch(Model):
    status: Literal["supported", "insufficient", "denied"]
    function_body: str | None = Field(default=None, max_length=16384)
    citations: list[QuoteChoice] = Field(default_factory=list, max_length=6)
    explanation: str = Field(default="", max_length=800)


def response_schema(case: Case) -> type[Answer] | type[WireAnswer] | type[WirePatch]:
    if case.output_contract is None:
        return Answer
    return WirePatch if case.category == "repair" else WireAnswer


def quote_sources(
    sources: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Citation]]:
    result = []
    catalog = {}
    for index, source in enumerate(sources):
        text = source["text"]
        segments = []
        for ordinal, offset in enumerate(range(0, len(text), 1200)):
            quote = text[offset : offset + 1200]
            if not quote:
                continue
            key = f"s{index}q{ordinal}"
            catalog[key] = Citation(source_id=source["id"], quote=quote)
            segments.append({"quote_id": key, "text": quote})
        result.append(
            {k: v for k, v in source.items() if k != "text"}
            | {
                "quote_segments": segments,
            }
        )
    return result, catalog


def decode(
    value: WireAnswer | WirePatch,
    catalog: dict[str, Citation],
    snapshot: Snapshot | None,
) -> Answer:
    citations = []
    for choice in value.citations:
        if choice.quote_id not in catalog:
            raise DomainError("MODEL_QUOTE_ID_INVALID", "Choose an exact supplied quote_id")
        citations.append(catalog[choice.quote_id])
    patch = None
    if isinstance(value, WirePatch) and value.function_body is not None:
        if value.status != "supported" or snapshot is None:
            raise DomainError(
                "PATCH_BODY_INVALID", "Only supported local repairs may include a body"
            )
        patch = body_diff(snapshot, value.function_body)
    return Answer(
        status=value.status,
        values=value.values if isinstance(value, WireAnswer) else {},
        citations=citations,
        explanation=value.explanation,
        diff=patch,
    )
