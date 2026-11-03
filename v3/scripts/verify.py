"""Execute registered tests with raw logs; zero tests and skipped tests never pass."""

import argparse
import hashlib
import importlib.util
import json
import os
import signal
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4


def source_fingerprint(root: Path) -> str:
    fingerprint = hashlib.sha256()
    for subtree in ["src", "migrations", "config", "scripts", "tests", "eval/final-v1", "deploy"]:
        for path in sorted((root / subtree).rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts:
                fingerprint.update(str(path.relative_to(root)).encode() + b"\0")
                fingerprint.update(path.read_bytes() + b"\0")
    for name in ["pyproject.toml", "uv.lock"]:
        if (root / name).is_file():
            fingerprint.update(name.encode() + b"\0" + (root / name).read_bytes())
    return fingerprint.hexdigest()


def execute(root: Path, task: str, registry: dict, evidence_root: Path | None = None) -> dict:
    if task not in registry:
        return {"task": task, "status": "NOT_RUN", "reason": "unknown_task", "exit_code": 2}
    if importlib.util.find_spec("pytest") is None:
        return {"task": task, "status": "NOT_RUN", "reason": "missing_pytest", "exit_code": 2}
    entry = registry[task]
    targets = entry.get("tests", [])
    if not targets or any(
        not isinstance(p, str)
        or not (root / p).is_file()
        or not (root / p).resolve().is_relative_to(root.resolve())
        for p in targets
    ):
        return {"task": task, "status": "NOT_RUN", "reason": "missing_test_targets", "exit_code": 2}
    attempt = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ-") + uuid4().hex[:8]
    directory = (evidence_root or root / "evidence") / task / attempt
    directory.mkdir(parents=True)
    junit = directory / "junit.xml"
    command = [sys.executable, "-m", "pytest", "-q", *targets, f"--junitxml={junit}"]
    started = datetime.now(UTC).isoformat()
    frozen_source = source_fingerprint(root)
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=False
    )
    before = time.monotonic()
    try:
        with (directory / "command.log").open("wb") as log:
            process = subprocess.Popen(
                command, cwd=root, stdout=log, stderr=subprocess.STDOUT, start_new_session=True
            )
            try:
                exit_code = process.wait(timeout=entry.get("timeout_seconds", 90))
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
                raise
        reason = "test_failure"
    except subprocess.TimeoutExpired:
        exit_code, reason = 124, "timeout"
    counts = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    if junit.exists():
        try:
            for suite in ET.parse(junit).getroot().iter("testsuite"):
                for key in counts:
                    counts[key] += int(suite.get(key, "0"))
        except (ET.ParseError, ValueError):
            exit_code, reason = 1, "invalid_junit"
    passed = (
        exit_code == 0
        and counts["tests"] > 0
        and not any(counts[key] for key in ["failures", "errors", "skipped"])
    )
    if not counts["tests"] and exit_code != 124:
        reason = "no_collected_tests"
    elif counts["skipped"]:
        reason = "skipped_tests"
    final_source = source_fingerprint(root)
    if final_source != frozen_source:
        passed, reason = False, "source_changed_during_verification"
    result = {
        "task": task,
        "status": "PASS" if passed else "FAIL",
        "engineering_gate": "PASS" if passed else "FAIL",
        "live_gate": "NOT_APPLICABLE" if task.startswith("V31-R0") else "NOT_RUN",
        "reason": None if passed else reason,
        "argv": command,
        "cwd": str(root),
        "started_at": started,
        "finished_at": datetime.now(UTC).isoformat(),
        "duration_seconds": round(time.monotonic() - before, 3),
        "command_exit_code": exit_code,
        "exit_code": 0 if passed else (exit_code or 1),
        "counts": counts,
        "components": entry.get("components", {}),
        "git_commit": revision.stdout.strip() if revision.returncode == 0 else None,
        "source_sha256": frozen_source,
        "finished_source_sha256": final_source,
        "log_path": str((directory / "command.log").relative_to(root)),
        "attempt_path": str(directory.relative_to(root)),
    }
    (directory / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    (directory.parent / "acceptance.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    registry = json.loads((root / "config/verification.json").read_text())
    result = execute(root, args.task, registry)
    print(json.dumps(result, indent=2))
    return result["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
