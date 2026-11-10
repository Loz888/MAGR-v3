"""Generic isolated sandbox test executor (V3.4 R03).

The R5 sandbox could only run one fixed Go test package.  This executor keeps
the same container policy but makes the subject language-pluggable: trusted
adapters for Go and Python generate the command, environment and result parser,
so model output never becomes shell text.  A separate preparation phase refuses
to start tests when third-party dependencies would have to be installed without
a trusted, offline bundle.

Every run records the pinned image, command, test set, exit code, log hash and
the verified container policy.  Build failures, test failures, timeouts and
sandbox errors are distinct statuses.
"""

from __future__ import annotations

import ast
import hashlib
import io
import json
import os
import re
import selectors
import subprocess
import sys
import tarfile
import time
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal, Protocol, cast
from uuid import uuid4

from pydantic import Field

from mgar_v3.domain import DomainError, Model, digest
from mgar_v3.repair.workspace import tree
from mgar_v3.repository_analysis.workspace import validate_manifest_path
from mgar_v3.settings import ROOT

_PATH = re.compile(r"[A-Za-z0-9_./-]{1,200}")
_SHA256 = r"^[a-f0-9]{64}$"
_SECRET_KEY = re.compile(
    r"(TOKEN|SECRET|PASSWORD|CREDENTIAL|API_KEY|ACCESS_KEY|DATABASE_URL|DSN)", re.IGNORECASE
)
_BOOTSTRAP_ERROR = b"mgar-sandbox-error:"
_DOCKER_ENV_DROP = {
    "DOCKER_HOST",
    "DOCKER_CONTEXT",
    "DOCKER_TLS",
    "DOCKER_TLS_VERIFY",
    "DOCKER_CERT_PATH",
}
_SANDBOX_UID = "65532:65532"

SandboxStatus = Literal[
    "passed",
    "build_failed",
    "test_failed",
    "timeout",
    "sandbox_error",
    "no_tests",
    "waiting_for_dependencies",
]
Language = Literal["go", "python"]


class SandboxLimits(Model):
    """Fixed resource envelope; every value becomes a real container limit."""

    cpus: Annotated[float, Field(ge=0.25, le=4)] = 1.0
    memory_mb: Annotated[int, Field(ge=64, le=4096)] = 512
    pids: Annotated[int, Field(ge=16, le=512)] = 96
    timeout_seconds: Annotated[float, Field(gt=0, le=120)] = 30
    output_bytes: Annotated[int, Field(ge=4096, le=1_048_576)] = 65_536
    workspace_mb: Annotated[int, Field(ge=8, le=1024)] = 64
    tmp_mb: Annotated[int, Field(ge=32, le=4096)] = 512
    max_files: Annotated[int, Field(ge=1, le=256)] = 64
    max_file_bytes: Annotated[int, Field(ge=1024, le=1_048_576)] = 131_072
    max_total_bytes: Annotated[int, Field(ge=1024, le=4_194_304)] = 1_048_576


class DependencyReport(Model):
    status: Literal["ready", "waiting"]
    requirements: list[str] = Field(default_factory=list)
    message: str


class PreparationReceipt(Model):
    receipt_id: str
    language: Language
    status: Literal["ready", "waiting"]
    requirements: list[str]
    message: str
    image: str
    image_id: str
    toolchain: str


class SandboxPolicy(Model):
    network: Literal["none"]
    read_only: bool
    privileged: bool
    user: str
    cap_drop: list[str]
    no_new_privileges: bool
    memory_bytes: int
    pids_limit: int
    nano_cpus: int
    ipc: Literal["none"]
    mounts: int
    tmpfs: list[str]
    environment_keys: list[str]
    secrets_injected: Literal[False] = False


class TestReceipt(Model):
    schema_version: Literal["v34-sandbox-receipt-v1"] = "v34-sandbox-receipt-v1"
    receipt_id: str
    language: Language
    adapter: str
    status: SandboxStatus
    preparation: PreparationReceipt | None
    image: str | None
    image_id: str | None
    command: list[str]
    tests: list[str]
    passed_tests: int
    failed_tests: list[str]
    skipped_tests: int
    exit_code: int | None
    duration_seconds: float
    log_sha256: str
    log_bytes: int
    log_truncated: bool
    workspace_tree: str
    policy: SandboxPolicy | None
    error: str | None


@dataclass(frozen=True)
class ParsedTests:
    status: SandboxStatus
    tests: list[str]
    passed: int
    failed: list[str]
    skipped: int
    error: str | None


class TestAdapter(Protocol):
    language: Language
    name: str

    def prepare(self, files: Mapping[str, str]) -> DependencyReport: ...

    def command(self, files: Mapping[str, str]) -> list[str]: ...

    def environment(self, files: Mapping[str, str]) -> dict[str, str]: ...

    def parse(self, raw: bytes, exit_code: int) -> ParsedTests: ...


GO_ENVIRONMENT = {
    "PATH": "/usr/local/go/bin:/usr/local/bin:/usr/bin:/bin",
    "HOME": "/tmp",
    "LANG": "C.UTF-8",
    "TMPDIR": "/tmp",
    "GOCACHE": "/tmp/go-cache",
    "GOPATH": "/tmp/gopath",
    "GOTOOLCHAIN": "local",
    "GOPROXY": "off",
    "GOSUMDB": "off",
    "GOVCS": "*:off",
    "GOWORK": "off",
    "GOENV": "off",
    "CGO_ENABLED": "0",
    "GOMAXPROCS": "2",
}
PYTHON_ENVIRONMENT = {
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "HOME": "/tmp",
    "LANG": "C.UTF-8",
    "TMPDIR": "/tmp",
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONUNBUFFERED": "1",
    "PYTHONHASHSEED": "0",
    "PYTHONPATH": "/workspace",
}
GO_COMMAND = [
    "/usr/local/go/bin/go",
    "test",
    "-count=1",
    "-json",
    "-vet=off",
    "-buildvcs=false",
    "./...",
]
PYTHON_COMMAND = ["/usr/bin/python3", "/opt/mgar/runner.py", "--root", "/workspace"]


def _go_requirements(manifest: str) -> list[str]:
    requirements: set[str] = set()
    in_block = False
    for raw_line in manifest.splitlines():
        line = raw_line.split("//", 1)[0].strip()
        if not line:
            continue
        if line in {"require (", "require("}:
            in_block = True
            continue
        if in_block:
            if line == ")":
                in_block = False
            else:
                requirements.add(line.split()[0])
            continue
        if line.startswith("require "):
            remainder = line[len("require ") :].strip()
            if remainder.startswith("("):
                in_block = True
            elif remainder:
                requirements.add(remainder.split()[0])
    return sorted(requirements)


def _parse_go(raw: bytes, exit_code: int) -> ParsedTests:
    ran: set[str] = set()
    passed: set[str] = set()
    failed: set[str] = set()
    skipped = 0
    build_marker = False
    for line in raw.decode(errors="replace").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        action = event.get("Action")
        name = event.get("Test")
        output = event.get("Output")
        if isinstance(output, str) and ("[build failed]" in output or output.startswith("# ")):
            build_marker = True
        if action == "run" and isinstance(name, str):
            ran.add(name)
        elif action == "pass" and isinstance(name, str):
            passed.add(name)
        elif action == "fail" and isinstance(name, str):
            failed.add(name)
        elif action == "skip" and isinstance(name, str):
            skipped += 1
    tests = sorted(ran)
    if exit_code == 0:
        if not tests:
            status: SandboxStatus = "no_tests"
        elif failed:
            status = "test_failed"
        else:
            status = "passed"
    elif build_marker or not tests:
        status = "build_failed"
    else:
        status = "test_failed"
    return ParsedTests(
        status=status,
        tests=tests,
        passed=len(passed),
        failed=sorted(failed),
        skipped=skipped,
        error=None,
    )


def _parse_python(raw: bytes, exit_code: int) -> ParsedTests:
    tests: set[str] = set()
    failed: list[str] = []
    skipped = 0
    collection_errors = 0
    summary: dict[str, Any] | None = None
    for line in raw.decode(errors="replace").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get("event")
        if kind == "test":
            name = event.get("name")
            if not isinstance(name, str):
                continue
            tests.add(name)
            if event.get("status") in {"fail", "error"}:
                failed.append(name)
            elif event.get("status") == "skip":
                skipped += 1
        elif kind == "collection_error":
            collection_errors += 1
        elif kind == "summary":
            summary = event
    ordered = sorted(tests)
    if collection_errors:
        status: SandboxStatus = "build_failed"
        error: str | None = "Test collection failed"
    elif summary is None:
        status = "sandbox_error"
        error = "Python runner exited without a summary"
    elif exit_code == 0 and not ordered:
        status = "no_tests"
        error = None
    elif failed:
        status = "test_failed"
        error = None
    elif exit_code != 0:
        status = "sandbox_error"
        error = "Python runner exited non-zero without a failing test"
    else:
        status = "passed"
        error = None
    return ParsedTests(
        status=status,
        tests=ordered,
        passed=int(summary.get("passed", 0)) if summary else 0,
        failed=failed,
        skipped=skipped,
        error=error,
    )


class GoAdapter:
    language: Language = "go"
    name = "go-test-json-v1"

    def prepare(self, files: Mapping[str, str]) -> DependencyReport:
        if not any(path.endswith(".go") for path in files):
            raise DomainError("SANDBOX_SOURCE_MISSING", "No Go source files were provided", 400)
        manifest = files.get("go.mod")
        if manifest is None:
            raise DomainError("SANDBOX_MANIFEST_MISSING", "Go repairs require a go.mod", 400)
        requirements = _go_requirements(manifest)
        if requirements and "vendor/modules.txt" not in files:
            return DependencyReport(
                status="waiting",
                requirements=requirements,
                message="Go dependencies require a trusted offline vendor bundle",
            )
        return DependencyReport(
            status="ready",
            requirements=requirements,
            message="Vendored or standard-library-only Go module",
        )

    def command(self, files: Mapping[str, str]) -> list[str]:
        return list(GO_COMMAND)

    def environment(self, files: Mapping[str, str]) -> dict[str, str]:
        environment = dict(GO_ENVIRONMENT)
        environment["GOFLAGS"] = "-mod=vendor" if "vendor/modules.txt" in files else "-mod=mod"
        return environment

    def parse(self, raw: bytes, exit_code: int) -> ParsedTests:
        return _parse_go(raw, exit_code)


def _python_requirements(files: Mapping[str, str]) -> list[str]:
    locals_ = {path.split("/")[0].removesuffix(".py") for path in files}
    imported: set[str] = set()
    for path, content in files.items():
        if not path.endswith(".py"):
            continue
        try:
            parsed = ast.parse(content)
        except SyntaxError:
            continue
        for node in ast.walk(parsed):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imported.add(node.module.split(".")[0])
    standard = set(sys.stdlib_module_names) | {"__future__"}
    external = {name for name in imported if name not in standard and name not in locals_}
    requirements = set(external)
    manifest = files.get("requirements.txt")
    if manifest:
        for raw_line in manifest.splitlines():
            line = raw_line.split("#", 1)[0].strip()
            if line:
                requirements.add(line)
    pyproject = files.get("pyproject.toml")
    if pyproject:
        try:
            declared = tomllib.loads(pyproject).get("project", {}).get("dependencies", [])
        except (tomllib.TOMLDecodeError, AttributeError):
            declared = []
        if isinstance(declared, list):
            requirements.update(item for item in declared if isinstance(item, str))
    return sorted(requirements)


class PythonAdapter:
    language: Language = "python"
    name = "python-unittest-json-v1"

    def prepare(self, files: Mapping[str, str]) -> DependencyReport:
        if not any(path.endswith(".py") for path in files):
            raise DomainError(
                "SANDBOX_SOURCE_MISSING", "No Python source files were provided", 400
            )
        requirements = _python_requirements(files)
        if requirements:
            return DependencyReport(
                status="waiting",
                requirements=requirements,
                message="Python dependencies require a trusted offline bundle",
            )
        return DependencyReport(
            status="ready",
            requirements=[],
            message="Standard-library-only Python workspace",
        )

    def command(self, files: Mapping[str, str]) -> list[str]:
        return list(PYTHON_COMMAND)

    def environment(self, files: Mapping[str, str]) -> dict[str, str]:
        return dict(PYTHON_ENVIRONMENT)

    def parse(self, raw: bytes, exit_code: int) -> ParsedTests:
        return _parse_python(raw, exit_code)


class _SandboxRuntimeError(Exception):
    """A failure inside the trusted container launcher, not inside the subject."""


@dataclass(frozen=True)
class _Execution:
    raw: bytes
    exit_code: int
    timed_out: bool
    truncated: bool
    duration_seconds: float
    policy: SandboxPolicy


def _safe_path(value: str) -> str:
    try:
        validate_manifest_path(value)
    except DomainError as exc:
        raise DomainError(
            "SANDBOX_PATH_DENIED", "Path is outside the sandbox workspace", 403
        ) from exc
    if not _PATH.fullmatch(value) or any(
        segment.startswith(".") for segment in value.split("/")
    ):
        raise DomainError("SANDBOX_PATH_DENIED", "Path contains unsupported characters", 403)
    return value


def _archive(files: Mapping[str, str], limits: SandboxLimits) -> bytes:
    if not files:
        raise DomainError("SANDBOX_SOURCE_MISSING", "Sandbox input is empty", 400)
    if len(files) > limits.max_files:
        raise DomainError("SANDBOX_INPUT_LIMIT", "Sandbox input has too many files", 413)
    stream = io.BytesIO()
    total = 0
    with tarfile.open(fileobj=stream, mode="w") as archive:
        for name, content in sorted(files.items()):
            safe = _safe_path(name)
            if not isinstance(content, str):
                raise DomainError("SANDBOX_INPUT_LIMIT", "Sandbox input must be text", 415)
            data = content.encode()
            if len(data) > limits.max_file_bytes:
                raise DomainError("SANDBOX_INPUT_LIMIT", "Sandbox file exceeds its bound", 413)
            total += len(data)
            if total > limits.max_total_bytes:
                raise DomainError("SANDBOX_INPUT_LIMIT", "Sandbox input exceeds its bound", 413)
            info = tarfile.TarInfo(safe)
            info.size = len(data)
            info.mode = 0o600
            archive.addfile(info, io.BytesIO(data))
    payload = stream.getvalue()
    if len(payload) > 2 * 1024 * 1024:
        raise DomainError("SANDBOX_INPUT_LIMIT", "Source archive exceeds its bound", 413)
    return payload


def _workspace_tree(files: Mapping[str, str]) -> str:
    try:
        return tree(dict(files))
    except DomainError as exc:
        raise DomainError("SANDBOX_PATH_DENIED", "Workspace contains an invalid path", 403) from exc


def _docker_environment() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if key not in _DOCKER_ENV_DROP}


class SandboxExecutor:
    """Creates one fixed-policy container per test phase."""

    def __init__(
        self,
        *,
        docker: str = "docker",
        limits: SandboxLimits | None = None,
        lock_path: Path | None = None,
    ) -> None:
        self.docker = docker
        self.limits = limits or SandboxLimits()
        self.lock_path = lock_path or ROOT / ".local/r03/sandbox-lock.json"
        self.owner = uuid4().hex[:16]

    # -- setup ---------------------------------------------------------

    def lock(self) -> dict[str, Any]:
        if not self.lock_path.is_file():
            raise DomainError("SANDBOX_NOT_PREPARED", "Run make sandbox-build first", 503)
        try:
            data = json.loads(self.lock_path.read_text())
        except ValueError as exc:
            raise DomainError("SANDBOX_IMAGE_INVALID", "Sandbox lock is unreadable", 500) from exc
        if not isinstance(data, dict):
            raise DomainError("SANDBOX_IMAGE_INVALID", "Sandbox lock is not an object", 500)
        image_id = data.get("image_id")
        if (
            not isinstance(image_id, str)
            or not re.fullmatch(r"sha256:[a-f0-9]{64}", image_id)
            or not isinstance(data.get("image"), str)
        ):
            raise DomainError("SANDBOX_IMAGE_INVALID", "Sandbox image identity is invalid", 500)
        for key in ("source_sha256", "bootstrap_sha256", "runner_sha256"):
            value = data.get(key)
            if not isinstance(value, str) or not re.fullmatch(_SHA256, value):
                raise DomainError("SANDBOX_IMAGE_INVALID", "Sandbox lock digest is invalid", 500)
        if self._docker(["image", "inspect", image_id]).returncode:
            raise DomainError("SANDBOX_NOT_PREPARED", "Pinned sandbox image is unavailable", 503)
        return cast(dict[str, Any], data)

    def prepare(self, files: Mapping[str, str], adapter: TestAdapter) -> PreparationReceipt:
        lock = self.lock()
        report = adapter.prepare(files)
        toolchain = lock.get(f"{adapter.language}_version")
        if not isinstance(toolchain, str):
            raise DomainError("SANDBOX_IMAGE_INVALID", "Sandbox toolchain is not pinned", 500)
        return PreparationReceipt(
            receipt_id=digest(
                {
                    "language": adapter.language,
                    "requirements": report.requirements,
                    "status": report.status,
                }
            ),
            language=adapter.language,
            status=report.status,
            requirements=report.requirements,
            message=report.message,
            image=cast(str, lock["image"]),
            image_id=cast(str, lock["image_id"]),
            toolchain=toolchain,
        )

    def cleanup(self) -> int:
        listed = self._docker(
            ["ps", "-aq", "--filter", "label=mgar.v3.r03.sandbox=" + self.owner]
        )
        if listed.returncode:
            raise DomainError("SANDBOX_UNAVAILABLE", "Docker daemon is unavailable", 503)
        identifiers = listed.stdout.decode().split()
        for identifier in identifiers:
            self._docker(["rm", "-f", identifier])
        return len(identifiers)

    def __enter__(self) -> SandboxExecutor:
        return self

    def __exit__(self, *_exc: object) -> None:
        try:
            self.cleanup()
        except (DomainError, OSError):
            pass

    def reclaim_stale(self, *, keep_owner: str | None = None) -> int:
        """Remove containers left behind by interrupted or crashed owners.

        Every container carries the ``mgar.v3.r03.sandbox`` owner label; a new
        executor only preserves its own owner and reaps the rest.
        """

        keep = self.owner if keep_owner is None else keep_owner
        listed = self._docker(["ps", "-aq", "--filter", "label=mgar.v3.r03.sandbox"])
        if listed.returncode:
            raise DomainError("SANDBOX_UNAVAILABLE", "Docker daemon is unavailable", 503)
        removed = 0
        for identifier in listed.stdout.decode().split():
            probe = self._docker(
                [
                    "inspect",
                    "--format",
                    '{{ index .Config.Labels "mgar.v3.r03.sandbox" }}',
                    identifier,
                ]
            )
            if probe.returncode:
                continue
            owner = probe.stdout.decode().strip()
            if owner and owner != keep:
                self._docker(["rm", "-f", identifier])
                removed += 1
        return removed

    # -- execution -----------------------------------------------------

    def run(
        self,
        files: Mapping[str, str],
        adapter: TestAdapter,
        *,
        timeout: float | None = None,
    ) -> tuple[TestReceipt, bytes]:
        limits = self._limits(timeout)
        workspace = _workspace_tree(files)
        try:
            prepared = self.prepare(files, adapter)
        except OSError as exc:
            message = f"docker executable is unavailable: {exc}"
            return self._error_receipt(adapter, limits, workspace, message), b""
        if prepared.status == "waiting":
            return (
                self._waiting_receipt(adapter, limits, workspace, prepared),
                b"",
            )
        payload = _archive(files, limits)
        command = adapter.command(files)
        environment = adapter.environment(files)
        try:
            execution = self._execute(
                lock=self.lock(),
                payload=payload,
                command=command,
                environment=environment,
                limits=limits,
            )
        except (OSError, _SandboxRuntimeError) as exc:
            return self._error_receipt(adapter, limits, workspace, str(exc)), b""
        parsed = self._classify(adapter, execution)
        receipt = self._receipt(
            adapter=adapter,
            limits=limits,
            prepared=prepared,
            execution=execution,
            parsed=parsed,
            workspace=workspace,
            command=command,
        )
        return receipt, execution.raw

    def _limits(self, timeout: float | None) -> SandboxLimits:
        if timeout is None:
            return self.limits
        if not 0 < timeout <= 120:
            raise DomainError("SANDBOX_TIME_LIMIT", "Sandbox duration is outside its limit", 400)
        return self.limits.model_copy(update={"timeout_seconds": float(timeout)})

    def _classify(self, adapter: TestAdapter, execution: _Execution) -> ParsedTests:
        if execution.timed_out:
            return ParsedTests(
                status="timeout",
                tests=[],
                passed=0,
                failed=[],
                skipped=0,
                error="Sandbox exceeded its time budget",
            )
        if execution.truncated:
            return ParsedTests(
                status="sandbox_error",
                tests=[],
                passed=0,
                failed=[],
                skipped=0,
                error="Sandbox output exceeded its bound",
            )
        parsed = adapter.parse(execution.raw, execution.exit_code)
        if _BOOTSTRAP_ERROR in execution.raw:
            detail = execution.raw.rsplit(_BOOTSTRAP_ERROR, 1)[-1].decode(errors="replace")
            return ParsedTests(
                status="sandbox_error",
                tests=parsed.tests,
                passed=0,
                failed=[],
                skipped=0,
                error=detail.splitlines()[0].strip()[:300],
            )
        return parsed

    def _receipt(
        self,
        *,
        adapter: TestAdapter,
        limits: SandboxLimits,
        prepared: PreparationReceipt,
        execution: _Execution,
        parsed: ParsedTests,
        workspace: str,
        command: Sequence[str],
    ) -> TestReceipt:
        raw = execution.raw
        return TestReceipt(
            receipt_id=digest(
                {
                    "language": adapter.language,
                    "status": parsed.status,
                    "tests": parsed.tests,
                    "log_sha256": hashlib.sha256(raw).hexdigest(),
                    "image_id": prepared.image_id,
                    "exit_code": execution.exit_code,
                }
            ),
            language=adapter.language,
            adapter=adapter.name,
            status=parsed.status,
            preparation=prepared,
            image=prepared.image,
            image_id=prepared.image_id,
            command=list(command),
            tests=parsed.tests,
            passed_tests=parsed.passed,
            failed_tests=parsed.failed,
            skipped_tests=parsed.skipped,
            exit_code=execution.exit_code,
            duration_seconds=round(execution.duration_seconds, 3),
            log_sha256=hashlib.sha256(raw).hexdigest(),
            log_bytes=len(raw),
            log_truncated=execution.truncated,
            workspace_tree=workspace,
            policy=execution.policy,
            error=parsed.error,
        )

    def _waiting_receipt(
        self,
        adapter: TestAdapter,
        limits: SandboxLimits,
        workspace: str,
        prepared: PreparationReceipt,
    ) -> TestReceipt:
        empty = hashlib.sha256(b"").hexdigest()
        return TestReceipt(
            receipt_id=digest(
                {
                    "language": adapter.language,
                    "status": "waiting_for_dependencies",
                    "requirements": prepared.requirements,
                }
            ),
            language=adapter.language,
            adapter=adapter.name,
            status="waiting_for_dependencies",
            preparation=prepared,
            image=prepared.image,
            image_id=prepared.image_id,
            command=[],
            tests=[],
            passed_tests=0,
            failed_tests=[],
            skipped_tests=0,
            exit_code=None,
            duration_seconds=0.0,
            log_sha256=empty,
            log_bytes=0,
            log_truncated=False,
            workspace_tree=workspace,
            policy=None,
            error=prepared.message,
        )

    def _error_receipt(
        self,
        adapter: TestAdapter,
        limits: SandboxLimits,
        workspace: str,
        message: str,
    ) -> TestReceipt:
        empty = hashlib.sha256(b"").hexdigest()
        return TestReceipt(
            receipt_id=digest(
                {
                    "language": adapter.language,
                    "status": "sandbox_error",
                    "message": message,
                    "workspace_tree": workspace,
                }
            ),
            language=adapter.language,
            adapter=adapter.name,
            status="sandbox_error",
            preparation=None,
            image=None,
            image_id=None,
            command=[],
            tests=[],
            passed_tests=0,
            failed_tests=[],
            skipped_tests=0,
            exit_code=None,
            duration_seconds=0.0,
            log_sha256=empty,
            log_bytes=0,
            log_truncated=False,
            workspace_tree=workspace,
            policy=None,
            error=message,
        )

    def _execute(
        self,
        *,
        lock: Mapping[str, Any],
        payload: bytes,
        command: Sequence[str],
        environment: Mapping[str, str],
        limits: SandboxLimits,
    ) -> _Execution:
        name = "mgar-v3-r03-" + uuid4().hex[:16]
        memory = limits.memory_mb * 1024 * 1024
        args = [
            "create",
            "-i",
            "--name",
            name,
            "--label",
            "mgar.v3.r03.sandbox=" + self.owner,
            "--network=none",
            "--read-only",
            "--user=" + _SANDBOX_UID,
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--pids-limit=" + str(limits.pids),
            "--memory=" + str(memory),
            "--memory-swap=" + str(memory),
            "--cpus=" + f"{limits.cpus:g}",
            "--ipc=none",
            "--init",
            "--tmpfs",
            f"/workspace:rw,exec,nosuid,nodev,size={limits.workspace_mb * 1048576},mode=1777",
            "--tmpfs",
            f"/tmp:rw,exec,nosuid,nodev,size={limits.tmp_mb * 1048576},mode=1777",
            "--ulimit",
            "nofile=128:128",
            "--ulimit",
            "core=0:0",
        ]
        for key, value in sorted(environment.items()):
            args.extend(["--env", f"{key}={value}"])
        args.append(cast(str, lock["image_id"]))
        args.extend(command)
        created = self._docker(args)
        if created.returncode:
            raise _SandboxRuntimeError("Restricted container could not be created")
        identifier = created.stdout.decode().strip() or name
        started = time.monotonic()
        process: subprocess.Popen[bytes] | None = None
        raw = bytearray()
        reason: str | None = None
        try:
            policy = self._verify_policy(identifier, limits, environment)
            process = subprocess.Popen(
                [self.docker, "start", "-a", "-i", identifier],
                env=_docker_environment(),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            if process.stdin is None or process.stdout is None:
                raise _SandboxRuntimeError("Sandbox stream could not be opened")
            process.stdin.write(payload)
            process.stdin.close()
            deadline = started + limits.timeout_seconds
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout.fileno(), selectors.EVENT_READ)
                while selector.get_map():
                    if time.monotonic() >= deadline:
                        reason = "timeout"
                        break
                    if not selector.select(0.05):
                        continue
                    block = os.read(process.stdout.fileno(), 4096)
                    if not block:
                        break
                    if len(raw) + len(block) > limits.output_bytes:
                        raw.extend(block[: limits.output_bytes - len(raw)])
                        reason = "output_limit"
                        break
                    raw.extend(block)
            if reason is None:
                try:
                    process.wait(timeout=max(0.01, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    reason = "timeout"
            if reason is not None:
                self._docker(["kill", identifier])
            exit_code = int(self._inspect(identifier)["State"]["ExitCode"])
            return _Execution(
                raw=bytes(raw),
                exit_code=exit_code,
                timed_out=reason == "timeout",
                truncated=reason == "output_limit",
                duration_seconds=time.monotonic() - started,
                policy=policy,
            )
        finally:
            self._docker(["rm", "-f", identifier])
            if process is not None:
                if process.poll() is None:
                    process.kill()
                process.wait()
                if process.stdout is not None:
                    process.stdout.close()

    def _verify_policy(
        self,
        identifier: str,
        limits: SandboxLimits,
        environment: Mapping[str, str],
    ) -> SandboxPolicy:
        info = self._inspect(identifier)
        host = info.get("HostConfig")
        config = info.get("Config")
        if not isinstance(host, dict) or not isinstance(config, dict):
            raise _SandboxRuntimeError("Container inspection was not understood")
        mounts = info.get("Mounts") or []
        tmpfs = host.get("Tmpfs") or {}
        actual_env: dict[str, str] = {}
        for item in config.get("Env") or []:
            key, _, value = str(item).partition("=")
            actual_env[key] = value
        memory = limits.memory_mb * 1024 * 1024
        nano = int(round(limits.cpus * 1_000_000_000))
        violations = [
            host.get("NetworkMode") != "none",
            host.get("ReadonlyRootfs") is not True,
            bool(host.get("Binds")),
            bool(mounts),
            bool(host.get("Privileged")),
            config.get("User") != _SANDBOX_UID,
            set(host.get("CapDrop") or []) != {"ALL"},
            not any(
                "no-new-privileges" in str(item) for item in host.get("SecurityOpt") or []
            ),
            int(host.get("Memory") or 0) != memory,
            int(host.get("MemorySwap") or 0) != memory,
            int(host.get("PidsLimit") or 0) != limits.pids,
            int(host.get("NanoCpus") or 0) != nano,
            host.get("IpcMode") != "none",
            host.get("Init") is not True,
            bool(host.get("Devices")),
            bool(host.get("VolumesFrom")),
            set(tmpfs) != {"/workspace", "/tmp"},
            any(actual_env.get(key) != value for key, value in environment.items()),
            any(_SECRET_KEY.search(key) for key in actual_env),
        ]
        if any(violations):
            raise DomainError(
                "SANDBOX_POLICY", "Container policy differs from the fixed sandbox", 500
            )
        return SandboxPolicy(
            network="none",
            read_only=cast(bool, host.get("ReadonlyRootfs")),
            privileged=cast(bool, host.get("Privileged")),
            user=_SANDBOX_UID,
            cap_drop=["ALL"],
            no_new_privileges=True,
            memory_bytes=memory,
            pids_limit=limits.pids,
            nano_cpus=nano,
            ipc="none",
            mounts=0,
            tmpfs=sorted(str(key) for key in tmpfs),
            environment_keys=sorted(actual_env),
        )

    def _inspect(self, identifier: str) -> dict[str, Any]:
        probe = self._docker(["inspect", identifier])
        if probe.returncode:
            raise _SandboxRuntimeError("Container inspection failed")
        try:
            data = json.loads(probe.stdout)
            info = data[0]
        except (ValueError, IndexError, TypeError) as exc:
            raise _SandboxRuntimeError("Container inspection was not understood") from exc
        if not isinstance(info, dict):
            raise _SandboxRuntimeError("Container inspection was not understood")
        return cast(dict[str, Any], info)

    def _docker(
        self, args: Sequence[str], *, timeout: float = 15
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            [self.docker, *args],
            env=_docker_environment(),
            capture_output=True,
            timeout=timeout,
        )
