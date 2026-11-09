"""Canonical per-case record for quality evaluation experiments (QEV-02).

Pure standard library: this module must stay importable in an offline
environment where ``pydantic`` / ``psycopg`` are unavailable. The canonical
digest mirrors :func:`mgar_v3.domain.digest` without importing it.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

SCHEMA = "mgar-quality-eval-record-v1"

MODES = (
    "offline_replay",
    "deterministic_test",
    "fixture",
    "historical_import",
    "real_model_live",
)
STATUSES = ("PASS", "FAIL", "NOT_RUN", "UNKNOWN")
FAILURE_KINDS = ("business", "infrastructure", "validation_rejected", "unknown")
USAGE_SOURCES = ("actual_provider_usage", "fixture", "estimated", "not_applicable")
CACHE_STATES = ("cold", "warm", "none")


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def canonical_digest(value: Any) -> str:
    """Same serialization as ``mgar_v3.domain.digest`` (kept dependency-free)."""
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    value = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            value.update(chunk)
    return value.hexdigest()


@dataclass(frozen=True)
class CaseRecord:
    experiment_id: str
    baseline_commit: str
    candidate_commit: str
    dataset_hash: str
    config_hash: str
    case_id: str
    arm: str
    mode: str
    status: str
    failure_kind: str | None
    latency_ms: float | None
    model_calls: int
    usage_source: str
    evidence_refs: tuple[str, ...]
    repeat_index: int = 0
    order_index: int = 0
    seed: int | None = None
    cache_state: str = "none"
    card: str = ""
    run_id: str | None = None
    not_run_reason: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_micros: int = 0
    input_sha256: str | None = None
    output_sha256: str | None = None
    input_ref: str | None = None
    output_ref: str | None = None
    note: str | None = None

    @classmethod
    def new_run_id(cls) -> str:
        return str(uuid4())

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, Any]) -> CaseRecord:
        problems = validate_mapping(mapping)
        if problems:
            raise RecordError(problems)
        refs = tuple(str(item) for item in mapping["evidence_refs"])
        return cls(
            experiment_id=mapping["experiment_id"],
            baseline_commit=mapping["baseline_commit"],
            candidate_commit=mapping["candidate_commit"],
            dataset_hash=mapping["dataset_hash"],
            config_hash=mapping["config_hash"],
            case_id=mapping["case_id"],
            arm=mapping["arm"],
            mode=mapping["mode"],
            status=mapping["status"],
            failure_kind=mapping["failure_kind"],
            latency_ms=mapping["latency_ms"],
            model_calls=mapping["model_calls"],
            usage_source=mapping["usage_source"],
            evidence_refs=refs,
            repeat_index=mapping.get("repeat_index", 0),
            order_index=mapping.get("order_index", 0),
            seed=mapping.get("seed"),
            cache_state=mapping.get("cache_state", "none"),
            card=mapping.get("card", ""),
            run_id=mapping.get("run_id"),
            not_run_reason=mapping.get("not_run_reason"),
            prompt_tokens=mapping.get("prompt_tokens", 0),
            completion_tokens=mapping.get("completion_tokens", 0),
            cost_micros=mapping.get("cost_micros", 0),
            input_sha256=mapping.get("input_sha256"),
            output_sha256=mapping.get("output_sha256"),
            input_ref=mapping.get("input_ref"),
            output_ref=mapping.get("output_ref"),
            note=mapping.get("note"),
        )

    def to_dict(self) -> dict[str, Any]:
        data = {field.name: getattr(self, field.name) for field in fields(self)}
        data["evidence_refs"] = list(self.evidence_refs)
        data["schema"] = SCHEMA
        return data

    def key(self) -> tuple[str, str, str, int]:
        return (self.experiment_id, self.case_id, self.arm, self.repeat_index)


class RecordError(ValueError):
    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


REQUIRED_FIELDS = (
    "experiment_id",
    "baseline_commit",
    "candidate_commit",
    "dataset_hash",
    "config_hash",
    "case_id",
    "arm",
    "mode",
    "status",
    "failure_kind",
    "latency_ms",
    "model_calls",
    "usage_source",
    "evidence_refs",
)

OPTIONAL_FIELDS = (
    "repeat_index",
    "order_index",
    "seed",
    "cache_state",
    "card",
    "run_id",
    "not_run_reason",
    "prompt_tokens",
    "completion_tokens",
    "cost_micros",
    "input_sha256",
    "output_sha256",
    "input_ref",
    "output_ref",
    "note",
)

KNOWN_FIELDS = REQUIRED_FIELDS + OPTIONAL_FIELDS + ("schema",)
HASH_FIELDS = ("dataset_hash", "config_hash")
OPTIONAL_HASH_FIELDS = ("input_sha256", "output_sha256")


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_hash(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def record_key(mapping: Mapping[str, Any]) -> tuple[str, str, str, int]:
    return (
        str(mapping["experiment_id"]),
        str(mapping["case_id"]),
        str(mapping["arm"]),
        int(mapping.get("repeat_index", 0)),
    )


def validate_mapping(mapping: Any) -> list[str]:
    """Return a list of human-readable schema errors; empty means valid."""
    if not isinstance(mapping, Mapping):
        return ["record must be a JSON object"]

    problems: list[str] = []
    unknown = sorted(set(mapping) - set(KNOWN_FIELDS))
    if unknown:
        problems.append(f"unknown fields: {unknown}")
    missing = [name for name in REQUIRED_FIELDS if name not in mapping]
    if missing:
        problems.append(f"missing required fields: {missing}")

    if missing:
        return problems

    for name in ("experiment_id", "case_id", "arm"):
        if not isinstance(mapping[name], str) or not mapping[name].strip():
            problems.append(f"{name} must be a non-empty string")
    for name in ("baseline_commit", "candidate_commit"):
        value = mapping[name]
        if not isinstance(value, str) or not value.strip() or len(value) > 200:
            problems.append(f"{name} must be a non-empty string of at most 200 characters")

    if mapping["mode"] not in MODES:
        problems.append(f"mode must be one of {MODES}")
    if mapping["status"] not in STATUSES:
        problems.append(f"status must be one of {STATUSES}")
    if mapping["usage_source"] not in USAGE_SOURCES:
        problems.append(f"usage_source must be one of {USAGE_SOURCES}")

    for name in HASH_FIELDS:
        if not _is_hash(mapping[name]):
            problems.append(f"{name} must be a lowercase sha256 hex digest")
    for name in OPTIONAL_HASH_FIELDS:
        if mapping.get(name) is not None and not _is_hash(mapping[name]):
            problems.append(f"{name} must be null or a lowercase sha256 hex digest")

    latency = mapping["latency_ms"]
    if latency is not None and not (_is_number(latency) and latency >= 0):
        problems.append("latency_ms must be null or a non-negative number")

    for name in ("model_calls", "prompt_tokens", "completion_tokens", "cost_micros"):
        value = mapping.get(name, 0)
        if not (_is_int(value) and value >= 0):
            problems.append(f"{name} must be a non-negative integer")

    repeat_index = mapping.get("repeat_index", 0)
    if not (_is_int(repeat_index) and repeat_index >= 0):
        problems.append("repeat_index must be a non-negative integer")
    order_index = mapping.get("order_index", 0)
    if not (_is_int(order_index) and order_index >= 0):
        problems.append("order_index must be a non-negative integer")
    seed = mapping.get("seed")
    if seed is not None and not (_is_int(seed) and seed >= 0):
        problems.append("seed must be null or a non-negative integer")

    if mapping.get("cache_state", "none") not in CACHE_STATES:
        problems.append(f"cache_state must be one of {CACHE_STATES}")

    refs = mapping["evidence_refs"]
    if not isinstance(refs, (list, tuple)) or any(
        not isinstance(item, str) or not item.strip() for item in refs
    ):
        problems.append("evidence_refs must be a list of non-empty strings")

    reason = mapping.get("not_run_reason")
    if reason is not None and (not isinstance(reason, str) or not reason.strip()):
        problems.append("not_run_reason must be null or a non-empty string")

    problems.extend(_coherence_errors(mapping))
    return problems


def _coherence_errors(mapping: Mapping[str, Any]) -> list[str]:
    problems: list[str] = []
    status = mapping["status"]
    failure_kind = mapping["failure_kind"]
    reason = mapping.get("not_run_reason")

    if failure_kind is not None and failure_kind not in FAILURE_KINDS:
        problems.append(f"failure_kind must be null or one of {FAILURE_KINDS}")

    if status == "PASS":
        if failure_kind is not None:
            problems.append("PASS records must have failure_kind=null")
        if reason is not None:
            problems.append("PASS records must have not_run_reason=null")
    elif status == "FAIL":
        if failure_kind not in {"business", "infrastructure", "validation_rejected"}:
            problems.append(
                "FAIL records require failure_kind in business|infrastructure|validation_rejected"
            )
    elif status == "UNKNOWN":
        if failure_kind != "unknown":
            problems.append("UNKNOWN records require failure_kind=unknown")
    elif status == "NOT_RUN":
        if failure_kind is not None:
            problems.append("NOT_RUN records must have failure_kind=null")
        if not reason:
            problems.append("NOT_RUN records require not_run_reason")

    if mapping["usage_source"] == "not_applicable" and mapping.get("model_calls", 0) != 0:
        problems.append("usage_source=not_applicable requires model_calls=0")
    return problems


def serialize_record(mapping: Mapping[str, Any]) -> str:
    """Deterministic JSONL line (UTF-8, no BOM, sorted keys)."""
    return canonical_json(dict(mapping))
