"""Build only V3-owned tools/image, offline, without modifying adjacent V2 assets."""

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from mgar_v3.settings import ROOT


def main() -> None:
    directory = ROOT / ".local/r5"
    directory.mkdir(parents=True, exist_ok=True)
    candidates = [
        os.environ.get("MGAR_V3_GO"),
        "/usr/local/go/bin/go",
        str(ROOT.parent.parent / ".mgar-tools/go/bin/go"),
        shutil.which("go"),
    ]
    go = None
    version = ""
    for candidate in candidates:
        if not candidate or not Path(candidate).is_file():
            continue
        probe = subprocess.run([candidate, "version"], capture_output=True, text=True, timeout=3)
        if probe.returncode == 0 and re.fullmatch(
            r"go version go1\.(2[3-9]|[3-9][0-9])[^\n]*\n?", probe.stdout
        ):
            go = candidate
            version = probe.stdout.strip()
            break
    if go is None:
        raise SystemExit("Install Go >=1.23 or set MGAR_V3_GO to its compiler executable")
    goroot = subprocess.check_output([go, "env", "GOROOT"], text=True).strip()
    env = {
        "PATH": os.defpath,
        "GOTOOLCHAIN": "local",
        "GOPROXY": "off",
        "GOSUMDB": "off",
        "CGO_ENABLED": "0",
        "GOMAXPROCS": "2",
        "GOCACHE": str(directory / "build-cache"),
        "HOME": str(directory),
    }
    source_hash = hashlib.sha256()
    for p in [ROOT / "deploy/Dockerfile.sandbox", *sorted((ROOT / "deploy/r5").rglob("*.go"))]:
        source_hash.update(p.read_bytes())
    with tempfile.TemporaryDirectory(prefix="mgar-v3-r5-build-") as temporary:
        context = Path(temporary)
        (context / "tmp").mkdir(mode=0o777)
        (context / "tmp").chmod(0o1777)
        shutil.copytree(goroot, context / "go", symlinks=True)
        # The host toolchain may be private (0700); only its image copy is made readable.
        for path in [context / "go", *(context / "go").rglob("*")]:
            if not path.is_symlink():
                path.chmod(0o755 if path.is_dir() or path.stat().st_mode & 0o111 else 0o644)
        subprocess.run(
            [
                go,
                "build",
                "-trimpath",
                "-o",
                str(context / "entrypoint"),
                str(ROOT / "deploy/r5/entrypoint/main.go"),
            ],
            env=env,
            check=True,
        )
        subprocess.run(
            [
                go,
                "build",
                "-trimpath",
                "-o",
                str(directory / "astcheck"),
                str(ROOT / "deploy/r5/astcheck/main.go"),
            ],
            env=env,
            check=True,
        )
        shutil.copyfile(ROOT / "deploy/Dockerfile.sandbox", context / "Dockerfile")
        docker_env = {
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
        docker_env["BUILDX_CONFIG"] = str(directory / "buildx")
        subprocess.run(
            [
                "docker",
                "--host=unix:///var/run/docker.sock",
                "info",
                "--format",
                "{{.ServerVersion}}",
            ],
            env=docker_env,
            check=True,
        )
        subprocess.run(
            [
                "docker",
                "--host=unix:///var/run/docker.sock",
                "build",
                "--network=none",
                "--provenance=false",
                "--iidfile",
                str(context / "image.id"),
                "-t",
                "mgar-v3-sandbox:local",
                str(context),
            ],
            env=docker_env,
            check=True,
        )
        receipt = {
            "schema": "r5-sandbox-lock-v1",
            "image_id": (context / "image.id").read_text().strip(),
            "go_version": version,
            "build_source_sha256": source_hash.hexdigest(),
            "astcheck_sha256": hashlib.sha256((directory / "astcheck").read_bytes()).hexdigest(),
        }
        (directory / "sandbox-lock.json").write_text(json.dumps(receipt, indent=2) + "\n")
        print(json.dumps(receipt))


if __name__ == "__main__":
    main()
