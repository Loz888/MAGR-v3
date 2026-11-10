"""Run the V3.5 end-to-end evaluation harness (V35-E01/E02).

``--ablation`` dispatches to the E02 four-arm Memory/Compaction/Expert
ablation, which reuses the same tasks, orchestration, sandbox and grader.

The harness exercises the real product chain (snapshot create, authorized
orchestration, sandboxed patch/test, sealed review bundle) instead of a
one-shot prompt.  Two modes stay strictly separate:

* ``fixture`` drives a scripted structured provider and only counts towards
  ``engineering_gate``;
* ``live`` needs an explicit authorization file plus a real provider factory.
  Without both, every task is recorded as ``NOT_RUN`` and
  ``live_agent_gate = NOT_RUN``; no paid call is ever faked.

The holdout suite is never executed by default and must be requested with
``--evaluate-holdout``.
"""

from __future__ import annotations

import argparse
import importlib
import json
import shutil
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mgar_v3.quality_eval.v35 import runner, tasks
from mgar_v3.quality_eval.v35.grader import PASS

V3_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_ROOT = V3_ROOT / "evidence" / "v35" / "E01"


def _load_provider(spec: str) -> Any:
    module_name, _, attribute = spec.partition(":")
    if not module_name or not attribute:
        raise ValueError("provider must use the module:callable form")
    module = importlib.import_module(module_name)
    factory = getattr(module, attribute, None)
    if not callable(factory):
        raise ValueError(f"provider factory is not callable: {spec}")
    return factory()


def _resolve_output(args: argparse.Namespace) -> Path:
    if args.out:
        return Path(args.out).expanduser().resolve()
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return DEFAULT_OUTPUT_ROOT / f"{stamp}-{args.suite}-{args.mode}"


def _write_not_run(out: Path, tasks_: Sequence[tasks.Task], reason: str) -> dict[str, Any]:
    if out.exists():
        raise FileExistsError(f"report already exists: {out}")
    out.mkdir(parents=True)
    records = [
        {
            "schema": "mgar-v35-eval-result-v1",
            "mode": "live",
            "gate": "live_agent_gate",
            "task_id": item.task_id,
            "kind": item.kind,
            "status": "NOT_RUN",
            "reason": reason,
        }
        for item in tasks_
    ]
    summary = {
        "schema": "mgar-v35-eval-summary-v1",
        "mode": "live",
        "gate": "live_agent_gate",
        "live_agent_gate": "NOT_RUN",
        "reason": reason,
        "executed": 0,
        "passed": 0,
        "success_rate": 0.0,
        "status_counts": {"NOT_RUN": len(records)},
        "failure_reasons": {},
        "by_kind": {},
        "avg_model_calls": 0.0,
        "total_model_calls": 0,
        "avg_cost_micros": 0.0,
        "total_cost_micros": 0,
        "avg_latency_ms": 0.0,
        "score_signature": None,
        "tasks": [item.task_id for item in tasks_],
        "generated_at": datetime.now(UTC).isoformat(),
    }
    (out / "results.json").write_text(
        json.dumps(records, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (out / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return summary


def _resume_probe(task: tasks.Task, task_dir: Path) -> dict[str, Any]:
    probe = task_dir / "resume-probe"
    if probe.exists():
        shutil.rmtree(probe)
    shutil.copytree(task_dir / "ledger", probe / "ledger")
    return runner.simulate_interrupted_resume(task, run_dir=probe)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(argv) if argv is not None else sys.argv[1:]
    if "--ablation" in arguments:
        from mgar_v3.quality_eval.v35 import ablations

        return ablations.main([item for item in arguments if item != "--ablation"])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", choices=("dev", "holdout"), default="dev")
    parser.add_argument("--mode", choices=("fixture", "live"), default="fixture")
    parser.add_argument("--task", action="append", default=[], help="restrict to task ids")
    parser.add_argument("--experiment", type=Path, default=runner.EXPERIMENT)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument(
        "--evaluate-holdout",
        action="store_true",
        help="explicitly allow the holdout suite to run",
    )
    parser.add_argument(
        "--authorization",
        type=Path,
        default=None,
        help="required in live mode; must exist before any paid call",
    )
    parser.add_argument(
        "--provider",
        default=None,
        help="live only: module:callable returning a real RepairProvider",
    )
    args = parser.parse_args(argv)

    experiment = args.experiment.resolve()
    errors = tasks.validate_dataset(experiment)
    if errors:
        for error in errors:
            print(f"dataset error: {error}", file=sys.stderr)
        return 2

    if args.suite == "holdout" and not args.evaluate_holdout:
        print(
            "holdout is disabled by default; pass --evaluate-holdout to run it",
            file=sys.stderr,
        )
        return 2

    selected = tasks.select_tasks(experiment, suite=args.suite)
    if args.task:
        wanted = set(args.task)
        selected = [item for item in selected if item.task_id in wanted]
        missing = wanted - {item.task_id for item in selected}
        if missing:
            print(f"unknown task ids: {sorted(missing)}", file=sys.stderr)
            return 2
    if not selected:
        print("no tasks selected", file=sys.stderr)
        return 2

    out = _resolve_output(args)
    if out.exists():
        print(f"report already exists: {out}", file=sys.stderr)
        return 2
    if args.mode == "live":
        if args.authorization is None or not args.authorization.is_file():
            summary = _write_not_run(
                out,
                selected,
                "live authorization file is absent; no paid model call was made",
            )
            print(json.dumps({"live_agent_gate": summary["live_agent_gate"]}))
            return 0
        if not args.provider:
            summary = _write_not_run(
                out,
                selected,
                "live mode requires --provider module:callable",
            )
            print(json.dumps({"live_agent_gate": summary["live_agent_gate"]}))
            return 0
        provider_factory = args.provider
    else:
        provider_factory = None

    results = []
    for task in selected:
        task_dir = out / "tasks" / task.task_id
        if task_dir.exists():
            raise FileExistsError(f"task report already exists: {task_dir}")
        provider = _load_provider(provider_factory) if provider_factory else None
        result = runner.run_task(
            task,
            experiment=experiment,
            run_dir=task_dir,
            mode=args.mode,
            provider=provider,
        )
        if task.resume and result["status"] == PASS:
            result["resume_probe"] = _resume_probe(task, task_dir)
        results.append(result)

    summary = runner.write_report(
        results,
        out_dir=out,
        mode=args.mode,
        suite=args.suite,
        extra={"task_runs": [str(out / "tasks" / item.task_id) for item in selected]},
    )
    print(
        json.dumps({key: summary[key] for key in ("gate", "live_agent_gate", "executed", "passed")})
    )
    return 0 if summary["passed"] == summary["executed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
