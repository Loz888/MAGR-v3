"""Prepare a cache-only image variant; retain compiler, entrypoint and runtime limits."""

import argparse
import json
import os
import subprocess
from pathlib import Path
from uuid import uuid4

DOCKER = ["docker", "--host=unix:///var/run/docker.sock"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-lock", type=Path, required=True)
    parser.add_argument("--output-lock", type=Path, required=True)
    args = parser.parse_args()
    assert not args.output_lock.exists()
    base = json.loads(args.base_lock.read_text())
    name = "mgar-host-stdlib-" + uuid4().hex[:16]
    env = {
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

    def run(*a, **kw):
        return subprocess.check_output([*DOCKER, *a], env=env, **kw).decode().strip()

    run(
        "create",
        "--name",
        name,
        "--network=none",
        "--user=0:0",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--pids-limit=256",
        "--memory=2g",
        "--entrypoint=/usr/local/go/bin/go",
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
        base["image_id"],
        "build",
        "testing/internal/testdeps",
    )
    try:
        subprocess.run([*DOCKER, "start", "-a", name], env=env, check=True, timeout=180)
        code = run("inspect", "--format", "{{.State.ExitCode}}", name)
        assert code == "0", code
        changed = run("diff", name).splitlines()
        allowed = ("/opt", "/opt/gocache", "/tmp", "/.config", "/.config/go")
        unexpected = [
            line
            for line in changed
            if line[2:] not in allowed
            and not line[2:].startswith(("/opt/gocache/", "/.config/go/telemetry"))
        ]
        assert not unexpected, unexpected

        image = run(
            "commit",
            "--change",
            "USER 65532:65532",
            "--change",
            'ENTRYPOINT ["/mgar-v3-sandbox"]',
            "--change",
            "CMD []",
            name,
        )
        assert image.startswith("sha256:") and image != base["image_id"]
        receipt = {
            **base,
            "image_id": image,
            "cache_preparation": "offline_go_build_testing/internal/testdeps",
            "base_image_id": base["image_id"],
            "compiler_and_entrypoint_changed": False,
            "cache_and_local_telemetry_only": True,
        }
        args.output_lock.parent.mkdir(parents=True, exist_ok=True)
        args.output_lock.write_text(json.dumps(receipt, indent=2) + "\n")
        print(json.dumps(receipt))
    finally:
        subprocess.run([*DOCKER, "rm", "-f", name], env=env, check=True)


if __name__ == "__main__":
    main()
