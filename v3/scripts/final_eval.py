"""Prepare or inspect 90 assignments for free. Paid execution is a separate exact grant."""

import argparse
import json
from pathlib import Path

from mgar_v3.benchmark.metrics import summarize
from mgar_v3.benchmark.prepare import prepare, write
from mgar_v3.benchmark.run import execute, verify
from mgar_v3.domain import digest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("stage", choices=["prepare", "plan", "preflight", "run", "report"])
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--source-ref")
    parser.add_argument("--dataset", choices=["final-v1", "final-v3"], default="final-v1")
    parser.add_argument("--authorization", type=Path)
    parser.add_argument("--execute-paid", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--one-assignment", action="store_true")
    parser.add_argument("--continuation", type=Path)
    parser.add_argument(
        "--ablation",
        choices=["none", "no_reranker", "no_evidence_repair", "no_verifier"],
        default="none",
    )
    args = parser.parse_args()
    if args.stage == "prepare":
        if not args.source_ref:
            parser.error("prepare needs a fixed GitHub --source-ref")
        manifest = prepare(args.directory, args.source_ref, args.ablation, args.dataset)
    else:
        manifest = verify(args.directory, args.continuation)
    if args.stage in {"prepare", "plan"}:
        result = {
            "status": "PREPARED_NOT_AUTHORIZED",
            "manifest_hash": digest(manifest),
            "assignments": 90,
            "max_new_calls": manifest["max_new_calls"],
            "max_new_cost_micros": manifest["max_new_cost_micros"],
            "prior_cost_micros": manifest["prior_cost_micros"],
            "cumulative_upper_bound_micros": manifest["prior_cost_micros"]
            + manifest["max_new_cost_micros"],
            "new_paid_calls": 0,
            "external_writes": False,
            "ablation": manifest["ablation"],
        }
        write(args.directory / "plan.json", result)
    elif args.stage == "preflight":
        from mgar_v3.benchmark.preflight import preflight

        result = preflight(args.directory)
    elif args.stage == "run":
        if not args.authorization or not args.execute_paid:
            parser.error(
                "run needs --authorization and --execute-paid; neither is implied by prepare"
            )
        result = execute(
            args.directory,
            args.authorization,
            execute_paid=True,
            resume=args.resume,
            one_assignment=args.one_assignment,
            continuation=args.continuation,
        )
    else:
        result = summarize(json.loads((args.directory / "results.json").read_text()))
        write(args.directory / "summary.json", result)
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
