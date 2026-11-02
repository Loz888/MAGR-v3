"Trusted Docker launcher; no model-controlled command, mounts, environment or daemon."

import hashlib
import io
import json
import os
import selectors
import subprocess
import tarfile
import time
from collections.abc import Callable
from typing import Any
from uuid import uuid4

from mgar_v3.domain import DomainError
from mgar_v3.repair.workspace import workspace_hash
from mgar_v3.settings import ROOT

DOCKER = ["docker", "--host=unix:///var/run/docker.sock"]


def environment() -> dict[str, str]:
    return {
        k: v
        for k, v in os.environ.items()
        if k
        not in {
            "DOCKER_HOST",
            "DOCKER_CONTEXT",
            "DOCKER_TLS",
            "DOCKER_TLS_VERIFY",
            "DOCKER_CERT_PATH",
        }
    }


def command(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [*DOCKER, *args], env=environment(), capture_output=True, timeout=15, **kwargs
    )


def archive(files: dict[str, str]) -> bytes:
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as tar:
        for name, content in sorted(files.items()):
            from mgar_v3.repair.workspace import validate_path

            validate_path(name)
            data = content.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o600
            tar.addfile(info, io.BytesIO(data))
    if len(stream.getvalue()) > 1024 * 1024:
        raise DomainError("SANDBOX_INPUT_LIMIT", "Source archive exceeds its bound")
    return stream.getvalue()


def parse_tests(raw: bytes, code: int, expected: list[str]) -> dict[str, Any]:
    ran = set()
    passed = set()
    failed = set()
    skipped = 0
    package_done = False
    invalid = False
    for line in raw.decode(errors="replace").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            invalid = True
            continue
        name = event.get("Test")
        action = event.get("Action")
        if action == "run" and name:
            ran.add(name)
        if action == "pass" and name:
            passed.add(name)
        if action == "fail" and name:
            failed.add(name)
        if action == "skip":
            skipped += 1
        if (
            action == "pass"
            and not name
            and event.get("Package") == "example.invalid/mgar-v3/orders"
        ):
            package_done = True
    return {
        "passed": code == 0
        and not invalid
        and not skipped
        and package_done
        and set(expected) <= ran
        and ran == passed,
        "tests": sorted(ran),
        "failed_tests": sorted(failed),
        "skipped": skipped,
    }


def local_lock() -> dict[str, Any]:
    path = ROOT / ".local/r5/sandbox-lock.json"
    if not path.is_file():
        raise DomainError("SANDBOX_NOT_PREPARED", "Run make sandbox-build first")
    lock = json.loads(path.read_text())
    image = lock["image_id"]
    sources = [
        ROOT / "deploy/Dockerfile.sandbox",
        ROOT / "deploy/r5/astcheck/main.go",
        ROOT / "deploy/r5/entrypoint/main.go",
    ]
    if (
        hashlib.sha256(b"".join(p.read_bytes() for p in sources)).hexdigest()
        != lock["build_source_sha256"]
    ):
        raise DomainError(
            "SANDBOX_SOURCE_CHANGED", "Rebuild the sandbox after trusted source changes"
        )
    if not isinstance(image, str) or not image.startswith("sha256:") or len(image) != 71:
        raise DomainError("SANDBOX_IMAGE_INVALID", "Sandbox image identity is invalid")
    return dict(lock)


class Sandbox:
    def __init__(self, owner: str):
        self.owner = hashlib.sha256(owner.encode()).hexdigest()[:24]

    def lock(self) -> dict[str, Any]:
        lock = local_lock()
        probe = command(["image", "inspect", lock["image_id"]])
        if probe.returncode:
            raise DomainError("SANDBOX_NOT_PREPARED", "Pinned sandbox image is unavailable")
        return lock

    def cleanup(self) -> int:
        result = command(["ps", "-aq", "--filter", "label=mgar.v3.sandbox=" + self.owner])
        if result.returncode:
            raise DomainError("SANDBOX_UNAVAILABLE", "Docker daemon is unavailable")
        ids = result.stdout.decode().split()
        for identifier in ids:
            command(["rm", "-f", identifier])
        return len(ids)

    def run(
        self,
        files: dict[str, str],
        expected: list[str],
        timeout: float = 30,
        after_created: Callable[[str], None] | None = None,
    ) -> tuple[dict[str, Any], bytes]:
        if not 0 < timeout <= 45:
            raise DomainError("SANDBOX_TIME_LIMIT", "Sandbox duration is outside its fixed limit")
        lock = self.lock()
        receipt_id = uuid4()
        source = archive(files)
        args = [
            "create",
            "-i",
            "--name",
            "mgar-v3-r5-" + receipt_id.hex,
            "--label",
            "mgar.v3.sandbox=" + self.owner,
            "--network=none",
            "--read-only",
            "--user=65532:65532",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--pids-limit=96",
            "--memory=512m",
            "--cpus=1",
            "--ipc=none",
            "--init",
            "--tmpfs",
            "/tmp:rw,exec,nosuid,nodev,size=268435456,mode=1777",
            "--ulimit",
            "nofile=128:128",
            "--ulimit",
            "core=0:0",
            "--env",
            "HTTP_PROXY=",
            "--env",
            "HTTPS_PROXY=",
            "--env",
            "ALL_PROXY=",
            "--env",
            "http_proxy=",
            "--env",
            "https_proxy=",
            "--env",
            "all_proxy=",
            lock["image_id"],
        ]
        created = command(args)
        if created.returncode:
            raise DomainError("SANDBOX_CREATE_FAILED", "Restricted container could not be created")
        identifier = created.stdout.decode().strip()
        raw = bytearray()
        reason = None
        process = None
        try:
            info = json.loads(command(["inspect", identifier]).stdout)[0]
            host = info["HostConfig"]
            if (
                host["NetworkMode"] != "none"
                or not host["ReadonlyRootfs"]
                or host.get("Binds")
                or info.get("Mounts")
                or host["Privileged"]
                or info["Config"]["User"] != "65532:65532"
                or "ALL" not in host["CapDrop"]
                or not any("no-new-privileges" in v for v in host["SecurityOpt"])
                or host["Memory"] != 536870912
                or host["PidsLimit"] != 96
                or host["NanoCpus"] != 1000000000
                or host["IpcMode"] != "none"
                or not host["Init"]
                or host.get("Devices")
                or host.get("VolumesFrom")
            ):
                raise DomainError(
                    "SANDBOX_POLICY", "Container policy differs from the fixed sandbox"
                )
            if after_created:
                after_created(identifier)
            process = subprocess.Popen(
                [*DOCKER, "start", "-a", "-i", identifier],
                env=environment(),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            assert process.stdin is not None and process.stdout is not None
            process.stdin.write(source)
            process.stdin.close()
            deadline = time.monotonic() + timeout
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while selector.get_map():
                    if time.monotonic() >= deadline:
                        reason = "timeout"
                        break
                    if not selector.select(0.05):
                        continue
                    block = os.read(process.stdout.fileno(), 4096)
                    if not block:
                        break
                    if len(raw) + len(block) > 65536:
                        raw.extend(block[: 65536 - len(raw)])
                        reason = "output_limit"
                        break
                    raw.extend(block)
            if reason is None:
                try:
                    process.wait(timeout=max(0.01, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    reason = "timeout"
            if reason:
                command(["kill", identifier])
            info = json.loads(command(["inspect", identifier]).stdout)[0]
            code = info["State"]["ExitCode"]
            result = {
                "receipt_id": str(receipt_id),
                "image_id": lock["image_id"],
                "workspace_hash": workspace_hash(files),
                "exit_code": code,
                "stop_reason": reason,
                "policy": {
                    "network": "none",
                    "user": "65532:65532",
                    "read_only": True,
                    "mounts": [],
                    "cap_drop": ["ALL"],
                    "no_new_privileges": True,
                    "memory_bytes": 536870912,
                    "pids_limit": 96,
                },
                **parse_tests(bytes(raw), code, expected),
            }
            result["passed"] = result["passed"] and reason is None
            return result, bytes(raw)
        finally:
            command(["rm", "-f", identifier])
            if process:
                if process.poll() is None:
                    process.kill()
                process.wait()
                if process.stdout:
                    process.stdout.close()
