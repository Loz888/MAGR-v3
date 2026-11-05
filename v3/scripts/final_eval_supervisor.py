"""One assignment per fresh process, using one explicitly authorized frozen budget."""

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

from mgar_v3.benchmark.run import resume_state, verify
from mgar_v3.domain import DomainError
from mgar_v3.settings import ROOT


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--authorization", type=Path, required=True)
    parser.add_argument("--continuation", type=Path)
    parser.add_argument("--execute-paid", action="store_true")
    args = parser.parse_args()
    if not args.execute_paid:
        parser.error("Paid continuation needs --execute-paid")
    directory = args.directory.resolve()
    manifest = verify(directory, args.continuation)
    logs = directory / "workers"
    logs.mkdir(mode=0o700, exist_ok=True)
    for _ in range(90):
        resuming = (directory / "execution.json").exists()
        if resuming:
            before, _ = resume_state(directory, manifest)
        else:
            before = json.loads((directory / "results.json").read_text())
            if before != manifest["assignments"]:
                raise DomainError(
                    "BENCHMARK_CHANGED", "Fresh trial must have untouched assignments"
                )
        remaining = sum(r["status"] == "NOT_RUN" for r in before)
        if remaining == 0:
            print(json.dumps({"status": "completed", "assignments": 90}), flush=True)
            return
        if shutil.disk_usage(ROOT).free < 1536 * 1024 * 1024:
            raise DomainError(
                "INSUFFICIENT_WORKSPACE_SPACE",
                "At least 1.5 GiB workspace headroom required before starting a paid worker",
            )
        path = logs / ("remaining-" + str(remaining) + ".log")
        if path.exists():
            raise DomainError(
                "WORKER_ALREADY_ATTEMPTED", "Inspect prior worker, never overwrite it"
            )
        command = [
            sys.executable,
            str(ROOT / "scripts/final_eval.py"),
            "run",
            "--directory",
            str(directory),
            "--authorization",
            str(args.authorization.resolve()),
            "--one-assignment",
            "--execute-paid",
        ]
        if resuming:
            command.append("--resume")
        if args.continuation:
            command.extend(["--continuation", str(args.continuation.resolve())])
        try:
            with path.open("xb") as log:
                worker = subprocess.run(
                    command,
                    cwd=ROOT,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    timeout=300,
                    check=False,
                )
        except subprocess.TimeoutExpired:
            raise DomainError(
                "WORKER_TIMEOUT", "Inspect ledger before any further continuation"
            ) from None
        if worker.returncode:
            raise DomainError("WORKER_FAILED", "Inspect worker log and usage before continuing")
        after, _ = resume_state(directory, manifest)
        if sum(r["status"] == "NOT_RUN" for r in after) >= remaining:
            raise DomainError("NO_PROGRESS", "Supervisor made no durable progress")
        print(
            json.dumps(
                {
                    "completed": 90 - sum(r["status"] == "NOT_RUN" for r in after),
                    "paid_calls": sum(r.get("model_calls", 0) for r in after),
                    "cost_micros": sum(r.get("cost_micros", 0) for r in after),
                }
            ),
            flush=True,
        )
    raise DomainError("WORKER_LIMIT", "Finite supervisor invocation limit reached")


if __name__ == "__main__":
    main()
