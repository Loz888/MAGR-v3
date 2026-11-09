"""Frozen experiment manifest for quality evaluation experiments (QEV-02)."""

from __future__ import annotations

import platform
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from mgar_v3.quality_eval.record import canonical_digest, sha256_file

SCHEMA = "mgar-quality-eval-manifest-v1"
PAIRING_KEY = ("case_id", "repeat_index")
REQUIRED_KEYS = (
    "schema",
    "experiment_id",
    "baseline_commit",
    "candidate_commit",
    "dataset",
    "config",
    "arms",
    "pairing_key",
    "authorization",
    "environment",
    "manifest_sha256",
)


def default_environment() -> dict[str, Any]:
    return {
        "python_version": sys.version.split()[0],
        "python_requires": ">=3.12,<3.13",
        "python_requires_deviation": (
            None
            if sys.version_info[:2] == (3, 12)
            else "runtime outside declared pyproject constraint; offline stdlib-only run"
        ),
        "platform": platform.platform(),
        "offline": True,
        "paid_model_calls": 0,
    }


def default_authorization() -> dict[str, Any]:
    return {
        "paid_models": False,
        "external_writes": False,
        "production_actions": False,
    }


def build_manifest(
    *,
    experiment_id: str,
    baseline_commit: str,
    candidate_commit: str,
    dataset_file: str,
    config_file: str,
    case_ids: Sequence[str],
    arms: Sequence[str],
    root: Path,
    mode: str,
    card: str = "QEV-02",
    seed: int | None = None,
    pairing_key: Sequence[str] = PAIRING_KEY,
    authorization: Mapping[str, Any] | None = None,
    environment: Mapping[str, Any] | None = None,
    provenance: Mapping[str, Any] | None = None,
    notes: str | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    root = Path(root)
    dataset_path = root / dataset_file
    config_path = root / config_file
    if not dataset_path.is_file():
        raise FileNotFoundError(f"dataset file not found: {dataset_path}")
    if not config_path.is_file():
        raise FileNotFoundError(f"config file not found: {config_path}")

    dataset_hash = sha256_file(dataset_path)
    config_hash = sha256_file(config_path)
    ordered_cases = sorted(str(item) for item in case_ids)
    if len(ordered_cases) != len(set(ordered_cases)):
        raise ValueError("case_ids contains duplicates")

    payload: dict[str, Any] = {
        "schema": SCHEMA,
        "experiment_id": experiment_id,
        "card": card,
        "created_at": created_at or datetime.now(UTC).isoformat(),
        "baseline_commit": baseline_commit,
        "candidate_commit": candidate_commit,
        "mode": mode,
        "dataset": {
            "file": dataset_file,
            "file_sha256": dataset_hash,
            "hash": dataset_hash,
            "case_ids": ordered_cases,
            "case_ids_sha256": canonical_digest(ordered_cases),
            "count": len(ordered_cases),
        },
        "config": {
            "file": config_file,
            "file_sha256": config_hash,
            "hash": config_hash,
        },
        "arms": list(arms),
        "pairing_key": list(pairing_key),
        "seed": seed,
        "authorization": dict(authorization or default_authorization()),
        "environment": dict(environment or default_environment()),
        "provenance": dict(provenance or {}),
        "notes": notes,
    }
    payload["manifest_sha256"] = canonical_digest(payload)
    return payload


def verify_manifest(
    manifest: Any, *, root: Path | None = None, check_files: bool = False
) -> dict[str, Any]:
    """Return ``{"errors": [...], "file_checks": {...}}`` for a manifest."""
    errors: list[str] = []
    file_checks: dict[str, str] = {}
    if not isinstance(manifest, Mapping):
        return {"errors": ["manifest must be a JSON object"], "file_checks": file_checks}

    missing = [name for name in REQUIRED_KEYS if name not in manifest]
    if missing:
        return {"errors": [f"manifest missing keys: {missing}"], "file_checks": file_checks}
    if manifest["schema"] != SCHEMA:
        errors.append(f"manifest schema must be {SCHEMA}")

    body = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    if canonical_digest(body) != manifest["manifest_sha256"]:
        errors.append("manifest_sha256 does not match canonical payload digest")

    dataset = manifest["dataset"]
    if not isinstance(dataset, Mapping):
        errors.append("dataset must be an object")
    else:
        for name in ("file", "file_sha256", "hash", "case_ids", "case_ids_sha256", "count"):
            if name not in dataset:
                errors.append(f"dataset missing {name}")
        if "case_ids" in dataset and isinstance(dataset["case_ids"], list):
            case_ids = dataset["case_ids"]
            if len(case_ids) != len(set(case_ids)):
                errors.append("dataset case_ids contains duplicates")
            if "count" in dataset and dataset["count"] != len(case_ids):
                errors.append("dataset count does not match case_ids length")
            if "case_ids_sha256" in dataset:
                if canonical_digest(list(case_ids)) != dataset["case_ids_sha256"]:
                    errors.append("dataset case_ids_sha256 does not match case_ids")
        for name in ("file_sha256", "hash"):
            if name in dataset and not (
                isinstance(dataset[name], str)
                and len(dataset[name]) == 64
                and all(c in "0123456789abcdef" for c in dataset[name])
            ):
                errors.append(f"dataset {name} must be a sha256 hex digest")

    config = manifest["config"]
    if not isinstance(config, Mapping) or "file_sha256" not in config:
        errors.append("config must be an object with file_sha256")

    if not isinstance(manifest["arms"], list) or not manifest["arms"]:
        errors.append("arms must be a non-empty list")
    if list(manifest["pairing_key"]) != list(PAIRING_KEY):
        errors.append(f"pairing_key must be {list(PAIRING_KEY)}")
    if not isinstance(manifest["authorization"], Mapping):
        errors.append("authorization must be an object")
    if not isinstance(manifest["environment"], Mapping):
        errors.append("environment must be an object")

    if check_files and root is not None and isinstance(dataset, Mapping):
        root = Path(root)
        for label, entry in (("dataset", dataset), ("config", manifest.get("config"))):
            if not isinstance(entry, Mapping) or "file" not in entry:
                continue
            path = root / str(entry["file"])
            if not path.is_file():
                file_checks[label] = "MISSING"
                errors.append(f"{label} file not found: {path}")
            elif sha256_file(path) != entry.get("file_sha256"):
                file_checks[label] = "MISMATCH"
                errors.append(f"{label} file hash mismatch: {path}")
            else:
                file_checks[label] = "MATCH"

    return {"errors": errors, "file_checks": file_checks}


def manifest_sha256(manifest: Mapping[str, Any]) -> str:
    return manifest["manifest_sha256"]


def unique_arms(records: Iterable[Mapping[str, Any]]) -> list[str]:
    return sorted({str(record["arm"]) for record in records})
