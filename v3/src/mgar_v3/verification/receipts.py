"""Execution receipts for A04: what ran, where, and what it proves.

A receipt is the only thing that can move a Finding into ``reproduced``. It is
bound to one commit and one environment fingerprint, and it records the exact
argv, exit status, duration and output digests so a failure can be replayed or
audited later instead of being asserted in prose.
"""

from __future__ import annotations

import hashlib
import shlex
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, ValidationError

from mgar_v3.domain import DomainError, Model, digest
from mgar_v3.scanners.sandbox import CommandResult

ExecutionClassification = Literal[
    "passed",
    "test_failure",
    "environment_error",
    "missing_dependency",
    "no_tests",
    "timeout",
    "command_error",
]

_ENVIRONMENT_MARKERS = (
    "command not found",
    "no such file or directory",
    "permission denied",
    "operation not permitted",
    "read-only file system",
    "no space left on device",
    "cannot allocate memory",
    "resource temporarily unavailable",
    "cannot connect to the docker daemon",
    "error response from daemon",
    "docker: not found",
    "os error 13",
    "os error 28",
)

_DEPENDENCY_MARKERS = (
    "modulenotfounderror",
    "no module named",
    "no required module provides package",
    "missing go.sum entry",
    "cannot find package",
    "package is not in std",
    "go: downloading",
    "goproxy",
    "proxyconnect",
    "could not find a version that satisfies",
    "externally-managed-environment",
)

_REAL_FAILURE_MARKERS = ("syntaxerror", "error collecting", "--- fail", "fail\t", "traceback")

_RECEIPT_ID = r"^[a-f0-9]{64}$"
_OWNER = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"


class ExecutionReceipt(Model):
    receipt_id: Annotated[str, Field(pattern=_RECEIPT_ID)]
    finding_id: Annotated[str, Field(pattern=_OWNER)]
    commit: Annotated[str, Field(pattern=r"^[a-f0-9]{40}$")]
    environment_id: Annotated[str, Field(pattern=_RECEIPT_ID)]
    command: Annotated[list[str], Field(min_length=1, max_length=64)]
    reproduce_command: Annotated[str, Field(min_length=1, max_length=4_096)]
    exit_code: Annotated[int, Field(ge=-1, le=255)]
    timed_out: bool
    truncated: bool
    duration_seconds: Annotated[float, Field(ge=0)]
    stdout_sha256: Annotated[str, Field(pattern=_RECEIPT_ID)]
    stderr_sha256: Annotated[str, Field(pattern=_RECEIPT_ID)]
    output_excerpt: Annotated[str, Field(max_length=2_000)]
    classification: ExecutionClassification
    sandbox: Literal["local", "docker"]


def classify_execution(
    argv: Sequence[str], result: CommandResult
) -> ExecutionClassification:
    """Name what actually happened, never what the caller hoped happened."""

    output = result.combined().lower()
    if result.timed_out:
        return "timeout"
    family = _family(argv)
    if family == "pytest" and result.exit_code == 5:
        return "no_tests"
    if result.exit_code == 0:
        return "passed"
    if any(marker in output for marker in _ENVIRONMENT_MARKERS):
        return "environment_error"
    if any(marker in output for marker in _DEPENDENCY_MARKERS):
        return "missing_dependency"
    if family == "pytest":
        if result.exit_code == 1:
            return "test_failure"
        if result.exit_code in {2, 3} and any(
            marker in output for marker in _REAL_FAILURE_MARKERS
        ):
            return "test_failure"
        return "environment_error"
    if family == "go":
        return "test_failure"
    return "command_error"


def execution_receipt(
    *,
    finding_id: str,
    command: Sequence[str],
    result: CommandResult,
    commit: str,
    environment_id: str,
    sandbox: Literal["local", "docker"],
) -> ExecutionReceipt:
    argv = [str(item) for item in command]
    stdout_sha256 = hashlib.sha256(result.stdout.encode("utf-8")).hexdigest()
    stderr_sha256 = hashlib.sha256(result.stderr.encode("utf-8")).hexdigest()
    classification = classify_execution(argv, result)
    payload = {
        "finding_id": finding_id,
        "commit": commit,
        "environment_id": environment_id,
        "command": argv,
        "exit_code": result.exit_code,
        "timed_out": result.timed_out,
        "truncated": result.truncated,
        "stdout_sha256": stdout_sha256,
        "stderr_sha256": stderr_sha256,
        "classification": classification,
        "sandbox": sandbox,
    }
    return ExecutionReceipt(
        receipt_id=digest(payload),
        finding_id=finding_id,
        commit=commit,
        environment_id=environment_id,
        command=argv,
        reproduce_command=shlex.join(argv),
        exit_code=result.exit_code,
        timed_out=result.timed_out,
        truncated=result.truncated,
        duration_seconds=round(result.duration_seconds, 3),
        stdout_sha256=stdout_sha256,
        stderr_sha256=stderr_sha256,
        output_excerpt=result.combined()[:2_000],
        classification=classification,
        sandbox=sandbox,
    )


def _family(argv: Sequence[str]) -> Literal["pytest", "go", "other"]:
    if not argv:
        return "other"
    executable = Path(str(argv[0])).name
    basename = "python" if executable.startswith("python") else executable
    arguments = [str(item) for item in argv[1:]]
    if basename == "pytest":
        return "pytest"
    if basename == "python" and arguments[:2] == ["-m", "pytest"]:
        return "pytest"
    if basename == "go" and "test" in arguments:
        return "go"
    return "other"


class ReceiptJournal:
    """Append-only JSONL journal owned by the verifier, not by the agent."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def append(self, receipt: ExecutionReceipt) -> None:
        if self.owns(receipt.receipt_id):
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(receipt.model_dump_json() + "\n")

    def owns(self, receipt_id: str) -> bool:
        return any(item.receipt_id == receipt_id for item in self.receipts())

    def receipts(self) -> list[ExecutionReceipt]:
        if not self.path.is_file():
            return []
        collected: list[ExecutionReceipt] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                collected.append(ExecutionReceipt.model_validate_json(line))
            except ValidationError as exc:
                raise DomainError(
                    "RECEIPT_JOURNAL_CORRUPT", "Receipt journal has an invalid record", 409
                ) from exc
        return collected
