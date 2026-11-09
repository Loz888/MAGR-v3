"""Validation and comparison gates for quality evaluation records (QEV-02)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from mgar_v3.quality_eval.manifest import verify_manifest
from mgar_v3.quality_eval.record import record_key, validate_mapping


class ValidationError(ValueError):
    def __init__(self, problems: Sequence[str]):
        super().__init__("; ".join(problems))
        self.problems = list(problems)


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    """Load one JSON object per non-blank line; reject non-object lines."""
    records: list[dict[str, Any]] = []
    with Path(path).open("r", encoding="utf-8") as handle:
        for number, raw in enumerate(handle, start=1):
            if not raw.strip():
                continue
            try:
                value = json.loads(raw)
            except json.JSONDecodeError as error:
                raise ValidationError([f"line {number}: invalid JSON ({error})"]) from error
            if not isinstance(value, dict):
                raise ValidationError([f"line {number}: record must be a JSON object"])
            records.append(value)
    return records


def validate_records(records: Iterable[Mapping[str, Any]]) -> list[str]:
    problems: list[str] = []
    seen: dict[tuple[str, str, str, int], int] = {}
    for index, mapping in enumerate(records, start=1):
        for problem in validate_mapping(mapping):
            problems.append(f"record {index}: {problem}")
        if not isinstance(mapping, Mapping):
            continue
        key = record_key(mapping)
        if key in seen:
            problems.append(f"duplicate key {key} at records {seen[key]} and {index}")
        else:
            seen[key] = index
    if not records:
        problems.append("no records found")
    return problems


def require_valid(records: Iterable[Mapping[str, Any]]) -> None:
    problems = validate_records(records)
    if problems:
        raise ValidationError(problems)


def single_value(records: Sequence[Mapping[str, Any]], field: str) -> str | None:
    values = {str(record[field]) for record in records}
    return values.pop() if len(values) == 1 else None


def require_comparable(
    records: Sequence[Mapping[str, Any]], baseline: str, candidate: str
) -> dict[str, Any]:
    """Refuse comparison when arms, datasets, configs or paired keys disagree."""
    require_valid(records)
    problems: list[str] = []

    arm_case_ids: dict[str, set[str]] = {}
    arm_pair_keys: dict[str, set[tuple[str, int]]] = {}
    for record in records:
        arm = str(record["arm"])
        arm_case_ids.setdefault(arm, set()).add(str(record["case_id"]))
        arm_pair_keys.setdefault(arm, set()).add(
            (str(record["case_id"]), int(record.get("repeat_index", 0)))
        )

    for label in (baseline, candidate):
        if label not in arm_case_ids:
            problems.append(f"arm {label} has no records")
    if problems:
        raise ValidationError(problems)

    for field in ("experiment_id", "dataset_hash", "config_hash"):
        values = {str(record[field]) for record in records}
        if len(values) != 1:
            problems.append(f"records disagree on {field}: {sorted(values)}")

    if baseline != candidate and arm_case_ids[baseline] != arm_case_ids[candidate]:
        missing_candidate = sorted(arm_case_ids[baseline] - arm_case_ids[candidate])
        missing_baseline = sorted(arm_case_ids[candidate] - arm_case_ids[baseline])
        problems.append(
            f"case sets differ: missing in {candidate}={missing_candidate}, "
            f"missing in {baseline}={missing_baseline}"
        )
    if baseline != candidate and arm_pair_keys[baseline] != arm_pair_keys[candidate]:
        only_baseline = sorted(arm_pair_keys[baseline] - arm_pair_keys[candidate])
        only_candidate = sorted(arm_pair_keys[candidate] - arm_pair_keys[baseline])
        problems.append(
            f"paired keys differ: only {baseline}={only_baseline}, "
            f"only {candidate}={only_candidate}"
        )

    if problems:
        raise ValidationError(problems)

    return {
        "arms": sorted(arm_case_ids),
        "case_count": len(arm_case_ids[baseline]),
        "pairs": len(arm_pair_keys[baseline]),
        "experiment_id": single_value(records, "experiment_id"),
        "dataset_hash": single_value(records, "dataset_hash"),
        "config_hash": single_value(records, "config_hash"),
    }


def check_manifest_consistency(
    manifest: Mapping[str, Any], records: Sequence[Mapping[str, Any]]
) -> list[str]:
    """Cross-check manifest identity against every record."""
    problems = list(verify_manifest(manifest)["errors"])
    if not isinstance(manifest, Mapping) or "dataset" not in manifest:
        return problems

    dataset = manifest["dataset"]
    labels = manifest.get("arms") if isinstance(manifest.get("arms"), list) else []

    if str(manifest.get("experiment_id")) != (single_value(records, "experiment_id") or ""):
        problems.append("record experiment_id does not match manifest")
    if dataset.get("hash") != single_value(records, "dataset_hash"):
        problems.append("record dataset_hash does not match manifest dataset.hash")
    config = manifest.get("config", {})
    if config.get("hash") != single_value(records, "config_hash"):
        problems.append("record config_hash does not match manifest config.hash")

    record_cases = {str(record["case_id"]) for record in records}
    manifest_cases = set(dataset.get("case_ids", []))
    if record_cases != manifest_cases:
        missing = sorted(manifest_cases - record_cases)
        extra = sorted(record_cases - manifest_cases)
        problems.append(f"record case_ids differ from manifest: missing={missing}, extra={extra}")

    record_arms = {str(record["arm"]) for record in records}
    if sorted(record_arms) != sorted(labels):
        problems.append(f"record arms {sorted(record_arms)} do not match manifest arms {labels}")
    return problems


def require_manifest_consistency(
    manifest: Mapping[str, Any], records: Sequence[Mapping[str, Any]]
) -> None:
    problems = check_manifest_consistency(manifest, records)
    if problems:
        raise ValidationError(problems)
