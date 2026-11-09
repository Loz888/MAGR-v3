"""Bounded command execution for scanners: snapshot, policy, local, Docker (A02).

Repository files are copied into a disposable snapshot before any tool runs.
The source worktree is only read, never written. Every command passes a
sub-command allow-list, and the Docker path re-inspects the created container
before it starts so the isolation claims are observed rather than asserted.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mgar_v3.domain import DomainError, digest
from mgar_v3.repository_analysis.workspace import validate_manifest_path
from mgar_v3.scanners.domain import ResourceLimits, ScanContext

_FORBIDDEN_ARGUMENT_CHARACTERS = ("\n", "\r", "\0", ";", "|", "&", "`", "$", ">", "<")

_ALLOWED_COMMANDS: dict[str, tuple[tuple[str, ...], ...]] = {
    "go": (("vet",), ("build",), ("test",)),
    "ruff": (("check",),),
    "pytest": ((),),
    "python": (("-m", "pytest"),),
    "semgrep": (("scan",),),
    "pyright": (("--outputjson",), ("--project",), ("-p",)),
}


@dataclass(frozen=True)
class CommandResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool
    truncated: bool
    duration_seconds: float

    def combined(self) -> str:
        return "\n".join(part for part in (self.stdout, self.stderr) if part)


@dataclass(frozen=True)
class Snapshot:
    path: Path
    digest: str
    files: int
    bytes: int


class CommandPolicy:
    """Only the exact tool sub-commands a scanner needs are permitted."""

    def check(self, argv: Sequence[str]) -> list[str]:
        if not argv or len(argv) > 64:
            raise DomainError("SANDBOX_COMMAND_DENIED", "Command shape is not permitted", 403)
        executable = str(argv[0])
        basename = Path(executable).name
        if not basename or "/" in basename or "\\" in basename or ".." in executable:
            raise DomainError("SANDBOX_COMMAND_DENIED", "Executable path is not permitted", 403)
        name = "python" if basename.startswith("python") else basename
        prefixes = _ALLOWED_COMMANDS.get(name)
        if prefixes is None:
            raise DomainError("SANDBOX_COMMAND_DENIED", "Executable is not permitted", 403)
        arguments = [str(item) for item in argv[1:]]
        for argument in arguments:
            if len(argument) > 4_096:
                raise DomainError("SANDBOX_COMMAND_DENIED", "Argument is too long", 403)
            if any(character in argument for character in _FORBIDDEN_ARGUMENT_CHARACTERS):
                raise DomainError(
                    "SANDBOX_COMMAND_DENIED", "Shell metacharacters are not permitted", 403
                )
        if not any(tuple(arguments[: len(prefix)]) == prefix for prefix in prefixes):
            raise DomainError(
                "SANDBOX_COMMAND_DENIED", "Sub-command is outside the allow-list", 403
            )
        return [executable, *arguments]


def _git_blob_sha(content: bytes) -> str:
    return hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest()


def materialize(
    context: ScanContext, *, base: Path | None = None, limits: ResourceLimits | None = None
) -> Snapshot:
    """Copy manifest-validated blobs into a disposable directory without ``.git``."""

    bounded = limits or ResourceLimits()
    root = Path(base) if base is not None else Path(tempfile.mkdtemp(prefix="mgar-v3-scan-"))
    directory = root / "src"
    directory.mkdir(parents=True, exist_ok=True)
    entries: list[dict[str, str]] = []
    total = 0
    for entry in context.files:
        relative = validate_manifest_path(entry.path)
        target = directory.joinpath(*relative.split("/"))
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise DomainError(
                "SANDBOX_SNAPSHOT_DENIED", "Snapshot directory could not be created", 409
            ) from exc
        source = context.worktree.joinpath(*relative.split("/"))
        try:
            if source.is_symlink() or not source.is_file():
                raise DomainError(
                    "SANDBOX_SNAPSHOT_MISMATCH", "Source file is missing or not regular", 409
                )
            content = source.read_bytes()
        except OSError as exc:
            raise DomainError(
                "SANDBOX_SNAPSHOT_MISMATCH", "Source file could not be read", 409
            ) from exc
        if len(content) != entry.size or _git_blob_sha(content) != entry.blob:
            raise DomainError(
                "SANDBOX_SNAPSHOT_MISMATCH", "Source file changed after its manifest", 409
            )
        total += len(content)
        if total > bounded.memory_bytes:
            raise DomainError(
                "SANDBOX_SNAPSHOT_LIMIT", "Snapshot exceeds its memory budget", 409
            )
        target.write_bytes(content)
        entries.append({"path": relative, "blob": entry.blob})
    snapshot_digest = digest(
        {
            "manifest_hash": context.manifest_hash,
            "files": sorted(entries, key=lambda item: item["path"]),
        }
    )
    return Snapshot(path=directory, digest=snapshot_digest, files=len(entries), bytes=total)


def _apply_limits(limits: ResourceLimits) -> Callable[[], None] | None:
    if os.name != "posix":
        return None

    def apply() -> None:
        import resource

        resource.setrlimit(resource.RLIMIT_CPU, (limits.cpu_seconds, limits.cpu_seconds + 2))
        resource.setrlimit(resource.RLIMIT_AS, (limits.memory_bytes, limits.memory_bytes))
        resource.setrlimit(
            resource.RLIMIT_FSIZE,
            (limits.max_output_bytes * 4, limits.max_output_bytes * 4),
        )
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))

    return apply


def _kill_group(process: subprocess.Popen[bytes]) -> None:
    try:
        if os.name == "posix":
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        else:
            process.kill()
    except (OSError, ProcessLookupError):
        process.kill()


def _run_captured(
    argv: Sequence[str],
    *,
    timeout: float,
    limits: ResourceLimits,
    cwd: Path | None,
    env: Mapping[str, str],
) -> CommandResult:
    """Run one bounded command with output capped to a regular file."""

    scratch = Path(tempfile.mkdtemp(prefix="mgar-v3-capture-"))
    out_path = scratch / "stdout"
    err_path = scratch / "stderr"
    started = time.monotonic()
    timed_out = False
    try:
        with out_path.open("wb") as stdout, err_path.open("wb") as stderr:
            try:
                process = subprocess.Popen(
                    list(argv),
                    cwd=str(cwd) if cwd is not None else None,
                    env=dict(env),
                    stdout=stdout,
                    stderr=stderr,
                    start_new_session=True,
                    preexec_fn=_apply_limits(limits),
                )
            except OSError as exc:
                raise DomainError(
                    "SANDBOX_COMMAND_FAILED", f"Command could not be started: {exc}", 502
                ) from exc
            try:
                code = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                _kill_group(process)
                code = process.wait()
        duration = time.monotonic() - started
        raw_out = out_path.read_bytes()
        raw_err = err_path.read_bytes()
        limit = limits.max_output_bytes
        truncated = len(raw_out) > limit or len(raw_err) > limit
        return CommandResult(
            exit_code=code,
            stdout=raw_out[:limit].decode("utf-8", "replace"),
            stderr=raw_err[:limit].decode("utf-8", "replace"),
            timed_out=timed_out,
            truncated=truncated,
            duration_seconds=round(duration, 3),
        )
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


class LocalSandbox:
    """Process-level sandbox used when no language image is pinned."""

    def __init__(
        self,
        root: Path,
        *,
        limits: ResourceLimits,
        policy: CommandPolicy,
        environment: Mapping[str, str],
    ):
        self.root = Path(root)
        self.limits = limits
        self.policy = policy
        self.base_environment = dict(environment)
        self.runtime = self.root.parent
        self.home = self.runtime / "home"
        self.temporary = self.runtime / "tmp"
        self.home.mkdir(parents=True, exist_ok=True)
        self.temporary.mkdir(parents=True, exist_ok=True)
        for directory in (self.home, self.temporary):
            directory.chmod(0o700)

    def environment(self) -> dict[str, str]:
        merged = {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "HOME": str(self.home),
            "TMPDIR": str(self.temporary),
        }
        merged.update(self.base_environment)
        return merged

    def run(self, argv: Sequence[str], timeout: float) -> CommandResult:
        checked = self.policy.check(argv)
        return _run_captured(
            checked,
            timeout=timeout,
            limits=self.limits,
            cwd=self.root,
            env=self.environment(),
        )


class DockerSandbox:
    """Pinned Go image with observed network, user, capability and mount policy."""

    def __init__(
        self,
        *,
        docker: str,
        image: str,
        snapshot: Snapshot,
        limits: ResourceLimits,
        policy: CommandPolicy,
    ):
        self.docker = docker
        self.image = image
        self.snapshot = snapshot
        self.limits = limits
        self.policy = policy
        self.cache = snapshot.path.parent / "cache"
        self.cache.mkdir(parents=True, exist_ok=True)

    def _open_permissions(self) -> None:
        for directory in (self.snapshot.path, self.cache):
            for path in [directory, *directory.rglob("*")]:
                try:
                    path.chmod(0o777 if path.is_dir() else 0o666)
                except OSError:
                    continue

    def _create(self, name: str, argv: Sequence[str]) -> None:
        arguments = [
            self.docker,
            "create",
            "--name",
            name,
            "--network=none",
            "--read-only",
            "--user=65532:65532",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--pids-limit=128",
            f"--memory={self.limits.memory_bytes}",
            f"--memory-swap={self.limits.memory_bytes}",
            "--cpus=1",
            "--ipc=none",
            "--init",
            "--tmpfs",
            "/tmp:rw,exec,nosuid,nodev,size=268435456,mode=1777",
            "--ulimit",
            "nofile=128:128",
            "--ulimit",
            "core=0:0",
            "--volume",
            f"{self.snapshot.path}:/src:rw",
            "--volume",
            f"{self.cache}:/cache:rw",
            "--workdir",
            "/src",
        ]
        for key in (
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "http_proxy",
            "https_proxy",
            "all_proxy",
        ):
            arguments.extend(["--env", f"{key}="])
        for key, value in (
            ("GOPATH", "/tmp"),
            ("GOCACHE", "/cache"),
            ("GOFLAGS", "-mod=readonly"),
            ("GOPROXY", "off"),
            ("GOSUMDB", "off"),
            ("GOTOOLCHAIN", "local"),
            ("GOMAXPROCS", "1"),
            ("HOME", "/tmp"),
            ("TMPDIR", "/tmp"),
            ("LANG", "C.UTF-8"),
            ("PATH", "/usr/local/go/bin:/usr/bin:/bin"),
        ):
            arguments.extend(["--env", f"{key}={value}"])
        arguments.append(self.image)
        arguments.extend(argv)
        try:
            created = subprocess.run(
                arguments, capture_output=True, text=True, timeout=60, check=False
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise DomainError(
                "SANDBOX_CREATE_FAILED", "Container could not be created", 502
            ) from exc
        if created.returncode != 0:
            detail = short_line(created.stderr)
            raise DomainError("SANDBOX_CREATE_FAILED", detail, 502)

    def _inspect(self, name: str) -> None:
        command = [self.docker, "inspect", "--format", "{{json .HostConfig}}", name]
        try:
            inspected = subprocess.run(
                command, capture_output=True, text=True, timeout=30, check=False
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise DomainError(
                "SANDBOX_INSPECT_FAILED", "Container could not be inspected", 502
            ) from exc
        if inspected.returncode != 0:
            raise DomainError("SANDBOX_INSPECT_FAILED", "Container could not be inspected", 502)
        try:
            config: Any = json.loads(inspected.stdout)
        except ValueError as exc:
            raise DomainError(
                "SANDBOX_INSPECT_FAILED", "Container policy is unreadable", 502
            ) from exc
        violations = []
        if not isinstance(config, dict):
            raise DomainError("SANDBOX_POLICY_VIOLATION", "Container policy is unreadable", 403)
        if config.get("NetworkMode") != "none":
            violations.append("network")
        if config.get("ReadonlyRootfs") is not True:
            violations.append("readonly_rootfs")
        if config.get("Memory") != self.limits.memory_bytes:
            violations.append("memory")
        if config.get("NanoCpus") != 1_000_000_000:
            violations.append("cpus")
        if config.get("PidsLimit") != 128:
            violations.append("pids")
        if config.get("CapDrop") != ["ALL"]:
            violations.append("capabilities")
        security = config.get("SecurityOpt")
        if not isinstance(security, list) or not any(
            str(item).startswith("no-new-privileges") for item in security
        ):
            violations.append("privilege_escalation")
        if config.get("IpcMode") != "none":
            violations.append("ipc")
        if config.get("Init") is not True:
            violations.append("init")
        if violations:
            raise DomainError(
                "SANDBOX_POLICY_VIOLATION",
                "Container policy was not applied: " + ", ".join(violations),
                403,
            )

    def _exit_code(self, name: str, fallback: int) -> int:
        try:
            inspected = subprocess.run(
                [self.docker, "inspect", "--format", "{{.State.ExitCode}}", name],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return fallback
        if inspected.returncode != 0:
            return fallback
        try:
            return int(inspected.stdout.strip())
        except ValueError:
            return fallback

    def _remove(self, name: str) -> None:
        try:
            subprocess.run(
                [self.docker, "rm", "-f", name],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return

    def run(self, argv: Sequence[str], timeout: float) -> CommandResult:
        checked = self.policy.check(argv)
        name = "mgar-v3-scan-" + uuid.uuid4().hex[:16]
        self._open_permissions()
        try:
            self._create(name, checked)
            self._inspect(name)
            started = _run_captured(
                [self.docker, "start", "--attach", name],
                timeout=timeout + 5,
                limits=self.limits,
                cwd=self.snapshot.path.parent,
                env=os.environ,
            )
            if started.timed_out:
                self._remove(name)
                return CommandResult(
                    exit_code=137,
                    stdout=started.stdout,
                    stderr=started.stderr,
                    timed_out=True,
                    truncated=started.truncated,
                    duration_seconds=started.duration_seconds,
                )
            return CommandResult(
                exit_code=self._exit_code(name, started.exit_code),
                stdout=started.stdout,
                stderr=started.stderr,
                timed_out=started.timed_out,
                truncated=started.truncated,
                duration_seconds=started.duration_seconds,
            )
        finally:
            self._remove(name)


def short_line(value: str, limit: int = 300) -> str:
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    return lines[-1][:limit] if lines else "No diagnostic was reported"


@dataclass(frozen=True)
class ScanRuntime:
    snapshot: Snapshot
    docker: DockerSandbox | None
    local: LocalSandbox

    def run(
        self, argv: Sequence[str], timeout: float, *, prefer_docker: bool = False
    ) -> tuple[CommandResult, str]:
        if prefer_docker and self.docker is not None:
            return self.docker.run(argv, timeout), "docker"
        return self.local.run(argv, timeout), "local"

    def dispose(self) -> None:
        shutil.rmtree(self.snapshot.path.parent, ignore_errors=True)


def build_runtime(
    snapshot: Snapshot,
    *,
    limits: ResourceLimits,
    policy: CommandPolicy,
    docker: str | None,
    docker_image: str | None,
    environment: Mapping[str, str],
) -> ScanRuntime:
    local = LocalSandbox(
        snapshot.path,
        limits=limits,
        policy=policy,
        environment=environment,
    )
    container = None
    if docker and docker_image:
        container = DockerSandbox(
            docker=docker,
            image=docker_image,
            snapshot=snapshot,
            limits=limits,
            policy=policy,
        )
    return ScanRuntime(snapshot=snapshot, docker=container, local=local)
