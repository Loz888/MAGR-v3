"""Build the V3.4 R03 Go/Python sandbox from a fixed, locally available base.

The base tag is resolved to an image ID and recorded in the lock; the build
runs with ``--network=none`` and copies only V3-owned bootstrap/runner files.
A matching lock and image make the target idempotent, so repeated acceptance
runs do not rebuild or pull anything.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from mgar_v3.settings import ROOT

BASE_IMAGE = "golang:1.27.1-bookworm"
IMAGE_TAG = "mgar-v3-sandbox-r03:local"
DOCKER = ["docker", "--host=unix:///var/run/docker.sock"]
DOCKER_ENV_DROP = {
    "DOCKER_HOST",
    "DOCKER_CONTEXT",
    "DOCKER_TLS",
    "DOCKER_TLS_VERIFY",
    "DOCKER_CERT_PATH",
}


def environment() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if key not in DOCKER_ENV_DROP}


def docker(args: list[str]) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run([*DOCKER, *args], env=environment(), capture_output=True)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    directory = ROOT / ".local/r03"
    directory.mkdir(parents=True, exist_ok=True)
    lock_path = directory / "sandbox-lock.json"
    sources = [
        ROOT / "deploy/Dockerfile.sandbox-r03",
        ROOT / "deploy/r03/bootstrap.py",
        ROOT / "deploy/r03/runner.py",
    ]
    source_hash = hashlib.sha256(b"".join(path.read_bytes() for path in sources)).hexdigest()

    base = docker(["image", "inspect", "--format", "{{.Id}}", BASE_IMAGE])
    if base.returncode:
        raise SystemExit(
            f"Base image {BASE_IMAGE} is not available locally; obtain it before building"
        )
    base_id = base.stdout.decode().strip()
    if not base_id.startswith("sha256:"):
        raise SystemExit("Base image identity is invalid")

    if lock_path.is_file():
        existing = json.loads(lock_path.read_text())
        if (
            existing.get("source_sha256") == source_hash
            and existing.get("base_image_id") == base_id
            and isinstance(existing.get("image_id"), str)
            and not docker(["image", "inspect", existing["image_id"]]).returncode
        ):
            print(json.dumps(existing))
            return

    (directory / "buildx").mkdir(exist_ok=True)
    build_env = environment()
    build_env["BUILDX_CONFIG"] = str(directory / "buildx")
    context = directory / "context"
    context.mkdir(exist_ok=True)
    (context / "image.id").unlink(missing_ok=True)
    built = subprocess.run(
        [
            *DOCKER,
            "build",
            "--network=none",
            "--provenance=false",
            "--iidfile",
            str(context / "image.id"),
            "-t",
            IMAGE_TAG,
            "-f",
            str(ROOT / "deploy/Dockerfile.sandbox-r03"),
            str(ROOT / "deploy"),
        ],
        env=build_env,
        capture_output=True,
    )
    if built.returncode:
        raise SystemExit(built.stderr.decode(errors="replace")[-4000:])
    image_id = (context / "image.id").read_text().strip()
    if not image_id.startswith("sha256:"):
        raise SystemExit("Built image identity is invalid")

    bootstrap_hash = digest(ROOT / "deploy/r03/bootstrap.py")
    runner_hash = digest(ROOT / "deploy/r03/runner.py")
    for remote, expected in (
        ("/opt/mgar/bootstrap.py", bootstrap_hash),
        ("/opt/mgar/runner.py", runner_hash),
    ):
        probe = docker(
            ["run", "--rm", "--network=none", "--entrypoint", "sha256sum", image_id, remote]
        )
        if probe.returncode or probe.stdout.decode().split()[0] != expected:
            raise SystemExit(f"Trusted file {remote} does not match its source")
    python = docker(
        ["run", "--rm", "--network=none", "--entrypoint", "/usr/bin/python3", image_id, "-V"]
    )
    go = docker(
        [
            "run",
            "--rm",
            "--network=none",
            "--entrypoint",
            "/usr/local/go/bin/go",
            image_id,
            "version",
        ]
    )
    if python.returncode or go.returncode:
        raise SystemExit("Sandbox toolchains could not be probed")
    receipt = {
        "schema": "r03-sandbox-lock-v1",
        "image": IMAGE_TAG,
        "image_id": image_id,
        "base_image": BASE_IMAGE,
        "base_image_id": base_id,
        "source_sha256": source_hash,
        "bootstrap_sha256": bootstrap_hash,
        "runner_sha256": runner_hash,
        "go_version": go.stdout.decode().strip(),
        "python_version": python.stdout.decode().strip(),
        "built_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    lock_path.write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps(receipt))


if __name__ == "__main__":
    main()
