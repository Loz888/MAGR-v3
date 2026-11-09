"""Read-only migration rehearsal for QEV-02 (no model calls, no new scores).

Repackages the existing ``eval/reports/final-v6-answers.jsonl`` records into the
QEV-02 case-record protocol so the validation/summary/comparison commands can be
exercised on real historical data. PASS/FAIL and all numbers are copied verbatim;
nothing is re-adjudicated. This produces a format rehearsal, not a new evaluation.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from mgar_v3.quality_eval import manifest as manifest_module
from mgar_v3.quality_eval import record

EXPERIMENT_ID = "qev-02-rehearsal"
SNAPSHOT_COMMIT = "5edad21e42bab401efbf36b484aae47dc0a0622b"
SOURCE_COMMIT = "c5ca75cd59fc2206bda1d6486955774350227e04"
DATASET_FILE = "eval/final-v3/cases.json"
CONFIG_FILE = "eval/reports/final-v6-manifest.json"
ANSWERS_FILE = "eval/reports/final-v6-answers.jsonl"
FREEZE_FILE = "eval/reports/final-v6-freeze-receipt.json"
INFRASTRUCTURE_CODES = {
    "MODEL_INVALID_RESPONSE",
    "MODEL_PROVIDER_ERROR",
    "MODEL_OUTPUT_LIMIT",
    "MODEL_TIMEOUT",
    "USAGE_UNKNOWN",
    "NETWORK_ERROR",
}


def failure_kind(row: dict[str, Any]) -> str | None:
    status = row["status"]
    if status == "PASS":
        return None
    if status == "UNKNOWN":
        return "unknown"
    if status == "NOT_RUN":
        return None
    reasons = {str(item) for item in row.get("reasons") or []}
    if reasons & INFRASTRUCTURE_CODES:
        return "infrastructure"
    return "business"


def to_record(
    row: dict[str, Any], *, line: int, order: int, dataset_hash: str, config_hash: str, raw: bytes
) -> dict[str, Any]:
    answer = row.get("answer")
    status = row["status"]
    model_calls = int(row.get("model_calls", 0))
    mapping = {
        "experiment_id": EXPERIMENT_ID,
        "baseline_commit": SNAPSHOT_COMMIT,
        "candidate_commit": SNAPSHOT_COMMIT,
        "dataset_hash": dataset_hash,
        "config_hash": config_hash,
        "case_id": row["case_id"],
        "arm": row["arm"],
        "mode": "historical_import",
        "status": status,
        "failure_kind": failure_kind(row),
        "latency_ms": (
            round(float(row["seconds"]) * 1000, 1) if row.get("seconds") is not None else None
        ),
        "model_calls": model_calls,
        "usage_source": "actual_provider_usage" if model_calls else "not_applicable",
        "evidence_refs": [f"{ANSWERS_FILE}#L{line}", FREEZE_FILE],
        "repeat_index": 0,
        "order_index": order,
        "seed": None,
        "cache_state": "none",
        "card": "QEV-02",
        "run_id": None,
        "not_run_reason": None if status != "NOT_RUN" else "source status NOT_RUN",
        "prompt_tokens": int(row.get("prompt_tokens", 0)),
        "completion_tokens": int(row.get("completion_tokens", 0)),
        "cost_micros": int(row.get("cost_micros", 0)),
        "input_sha256": record.sha256_bytes(raw),
        "output_sha256": record.canonical_digest(answer) if answer is not None else None,
        "input_ref": f"{ANSWERS_FILE}#L{line}",
        "output_ref": None,
        "note": (
            "格式迁移演练：PASS/FAIL 与数值原样复制自 final-v6；"
            "不重新判定、不产生新成绩、不代表 QEV-03/04 结论"
        ),
    }
    return mapping


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument(
        "--out", type=Path, default=None, help="default: experiments/quality-upgrade/qev-02-rehearsal"
    )
    args = parser.parse_args(argv)

    root = args.root
    answers_path = root / ANSWERS_FILE
    dataset_path = root / DATASET_FILE
    out = args.out or root / "experiments" / "quality-upgrade" / "qev-02-rehearsal"

    cases = json.loads(dataset_path.read_text(encoding="utf-8"))
    case_ids = [str(item["id"]) for item in cases]
    dataset_hash = record.sha256_file(dataset_path)
    config_hash = record.sha256_file(root / CONFIG_FILE)

    records: list[dict[str, Any]] = []
    with answers_path.open("r", encoding="utf-8") as handle:
        for line, raw in enumerate(handle, start=1):
            if not raw.strip():
                continue
            row = json.loads(raw)
            mapping = to_record(
                row,
                line=line,
                order=len(records),
                dataset_hash=dataset_hash,
                config_hash=config_hash,
                raw=raw.encode("utf-8"),
            )
            problems = record.validate_mapping(mapping)
            if problems:
                raise SystemExit(f"line {line}: migrated record invalid: {problems}")
            records.append(mapping)

    answer_cases = {item["case_id"] for item in records}
    if answer_cases != set(case_ids):
        raise SystemExit("migrated case_ids do not match eval/final-v3/cases.json")
    if len(records) != len(case_ids) * len({item["arm"] for item in records}):
        raise SystemExit("unbalanced arms in final-v6 answers")

    out.mkdir(parents=True, exist_ok=True)
    out.joinpath("cases.jsonl").write_text(
        "".join(record.serialize_record(item) + "\n" for item in records), encoding="utf-8"
    )

    experiment_manifest = manifest_module.build_manifest(
        experiment_id=EXPERIMENT_ID,
        baseline_commit=SNAPSHOT_COMMIT,
        candidate_commit=SNAPSHOT_COMMIT,
        dataset_file=DATASET_FILE,
        config_file=CONFIG_FILE,
        case_ids=case_ids,
        arms=sorted({item["arm"] for item in records}),
        root=root,
        mode="historical_import",
        card="QEV-02",
        created_at="2026-11-09T00:00:00+00:00",
        provenance={
            "source_commit": SOURCE_COMMIT,
            "source_file": ANSWERS_FILE,
            "source_file_sha256": record.sha256_file(answers_path),
            "freeze_receipt": FREEZE_FILE,
            "freeze_receipt_sha256": record.sha256_file(root / FREEZE_FILE),
            "migration": "format only; PASS/FAIL and metrics copied verbatim",
        },
        notes=(
            "格式迁移演练，不是新实验成绩；原 final-v6 报告保持冻结。"
            "不调用模型、不产生费用、不重新判定。"
        ),
    )
    out.joinpath("manifest.json").write_text(
        json.dumps(experiment_manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    result = {
        "status": "PASS",
        "experiment_id": EXPERIMENT_ID,
        "records": len(records),
        "arms": sorted({item["arm"] for item in records}),
        "dataset_hash": dataset_hash,
        "config_hash": config_hash,
        "manifest_sha256": experiment_manifest["manifest_sha256"],
        "paid_model_calls": 0,
        "output": str(out),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
