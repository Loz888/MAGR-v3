"""Apply a reviewed provider bill to two stopped calls, with no model request or retry."""

import argparse
import json
from pathlib import Path
from uuid import UUID

from mgar_v3.benchmark.prepare import configuration, write
from mgar_v3.benchmark.reconcile import HourlyReceipt, reconcile
from mgar_v3.coordination.engine import Engine
from mgar_v3.domain import digest
from mgar_v3.evidence.artifacts import ArtifactStore


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--call", action="append", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if not args.apply:
        parser.error("Applying a provider bill needs --apply")
    receipt = HourlyReceipt.model_validate_json(args.receipt.read_bytes())
    ids = [UUID(c) for c in args.call]
    directory = args.directory.resolve()
    manifest = json.loads((directory / "manifest.json").read_text())
    record_file = directory / "results.json"
    state_file = directory / "execution.json"
    records = json.loads(record_file.read_text())
    state = json.loads(state_file.read_text())
    record = next(r for r in records if r["status"] == "UNKNOWN")
    profile = next(
        p["profile"]
        for p in manifest["profiles"]
        if p["case_id"] == record["case_id"] and p["arm"] == record["arm"]
    )
    cfg = configuration(directory, profile["project_id"])
    engine = Engine(cfg, ArtifactStore(cfg.artifact_root))
    try:
        expected_run = state["runs"][record["case_id"] + ":" + record["arm"]]
        with engine.transaction() as conn:
            actual = conn.execute(
                "SELECT DISTINCT run_id FROM app.usage_calls WHERE call_id=ANY(%s)", (ids,)
            ).fetchall()
            if len(actual) != 1 or str(actual[0]["run_id"]) != expected_run:
                raise ValueError("Receipt calls do not belong to the selected assignment")
        result = reconcile(engine, receipt, ids)
    finally:
        engine.close()
    before = dict(record)
    record.update(
        status="FAIL",
        prior_status="UNKNOWN",
        reason="BILLING_RECONCILED_RESPONSE_LOST",
        infrastructure_failure=True,
        reserved_micros=0,
        cost_micros=result["group_upper_micros"],
        prompt_tokens=result["group_prompt_tokens"],
        completion_tokens=result["group_completion_tokens"],
        reconciliation_hash=result["receipt_hash"],
    )
    key = record["case_id"] + ":" + record["arm"]
    state.setdefault("sealed_results", {})[key] = digest(record)
    write(
        directory / "aggregate-reconciliation.json",
        {"settlement": result, "before": before, "after": record},
    )
    write(record_file, records)
    write(state_file, state)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
