"""Reproducible allowlisted source bundle and a fresh-install, zero-paid deployment check."""

import argparse
import hashlib
import io
import json
import os
import secrets
import socket
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path
from uuid import uuid4

import httpx
import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

from mgar_v3.repair.sandbox import DOCKER, environment
from mgar_v3.settings import ROOT, Settings

ASSETS = (
    "src",
    "migrations",
    "scripts",
    "config",
    "tests",
    "deploy",
    "eval",
    "web",
    "fixtures",
    "docs",
)
FILES = ("pyproject.toml", "uv.lock", "Makefile", "README.md", ".env.example")


def bundle(output: Path) -> dict:
    paths = sorted(
        {
            p
            for folder in ASSETS
            for p in (ROOT / folder).rglob("*")
            if p.is_file() and "__pycache__" not in p.parts
        }
        | {ROOT / p for p in FILES if (ROOT / p).is_file()}
    )
    hashes = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("wb") as raw:
        # gzip header timestamps and tar metadata are fixed for byte-identical builds.
        import gzip

        with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0, filename="") as compressed:
            with tarfile.open(fileobj=compressed, mode="w") as archive:
                for path in paths:
                    data = path.read_bytes()
                    info = tarfile.TarInfo("v3/" + str(path.relative_to(ROOT)))
                    info.size, info.mode, info.mtime = len(data), 0o644, 0
                    archive.addfile(info, io.BytesIO(data))
    result = {
        "schema": "r6-source-delivery-v1",
        "files": hashes,
        "archive_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        "scope": "source and lockfile; excludes private models, credentials and evidence",
    }
    output.with_suffix(output.suffix + ".json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def smoke(output: Path, postgres_container: str) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    cfg = Settings()
    base = cfg.database_url.get_secret_value()
    names = ["mgar_v3_deploy_" + uuid4().hex for _ in range(2)]
    checks = []
    process = None
    with tempfile.TemporaryDirectory(prefix="mgar-v3-clean-") as temp:
        temp_path = Path(temp)
        archive = temp_path / "source.tar.gz"
        release = bundle(archive)
        with tarfile.open(archive) as packed:
            packed.extractall(temp_path / "install", filter="data")
        app = temp_path / "install/v3"
        install_env = {
            **os.environ,
            "UV_PROJECT_ENVIRONMENT": str(app / ".venv"),
            "UV_CACHE_DIR": os.environ.get("UV_CACHE_DIR", str(output.resolve() / "uv-cache")),
            "UV_PYTHON_DOWNLOADS": "never",
        }
        with (output / "install.log").open("wb") as log:
            subprocess.run(
                ["uv", "sync", "--frozen", "--no-dev", "--python", "3.12"],
                cwd=app,
                env=install_env,
                stdout=log,
                stderr=subprocess.STDOUT,
                timeout=180,
                check=True,
            )
        checks.append("new source directory and independent locked non-dev virtualenv")
        python = str(app / ".venv/bin/python")
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        token, watchdog = secrets.token_hex(24), secrets.token_hex(24)
        runtime = {
            "PATH": os.environ["PATH"],
            "LANG": "C.UTF-8",
            "MGAR_V3_DATABASE_URL": make_conninfo(base, dbname=names[0]),
            "MGAR_V3_REDIS_URL": cfg.redis_url.get_secret_value(),
            "MGAR_V3_LOCAL_TOKEN": token,
            "MGAR_V3_WATCHDOG_TOKEN": watchdog,
            "MGAR_V3_ARTIFACT_ROOT": str(app / ".local/artifacts"),
            "MGAR_V3_GRAPH_VERSION": "r2-mailbox-v1",
            "MGAR_V3_ROUTER_MODE": "fixture",
            "MGAR_V3_ALLOW_PAID": "0",
            "MGAR_V3_ALLOW_EXTERNAL_WRITE": "0",
        }
        api = f"http://127.0.0.1:{port}"
        docker_env = environment()
        try:
            with psycopg.connect(base, autocommit=True) as conn:
                for name in names:
                    conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
            with (output / "migrate.log").open("wb") as log:
                for _ in range(2):
                    subprocess.run(
                        [python, "-m", "mgar_v3.store.migrate"],
                        cwd=app,
                        env=runtime,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        check=True,
                        timeout=30,
                    )
            checks.append("seventeen migrations and idempotent upgrade")

            def start():
                log = (output / "api.log").open("ab")
                child = subprocess.Popen(
                    [
                        python,
                        "-m",
                        "uvicorn",
                        "mgar_v3.api:create_app",
                        "--factory",
                        "--host",
                        "127.0.0.1",
                        "--port",
                        str(port),
                    ],
                    cwd=app,
                    env=runtime,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                )
                log.close()
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    try:
                        if httpx.get(api + "/api/v3/healthz", timeout=1).is_success:
                            return child
                    except httpx.HTTPError:
                        pass
                    if child.poll() is not None:
                        raise RuntimeError("Fresh API failed; inspect private api.log")
                    time.sleep(0.1)
                child.kill()
                child.wait()
                raise RuntimeError("Fresh API readiness timed out")

            process = start()
            with httpx.Client(base_url=api, headers={"X-MGAR-Token": token}, timeout=5) as client:
                response = client.post(
                    "/api/v3/runs",
                    json={
                        "project_id": "orders-demo",
                        "question": "Summarize documented symptoms",
                        "issue_number": 1,
                        "client_request_id": "clean-install",
                        "execution_mode": "fixture",
                    },
                )
                response.raise_for_status()
                run = response.json()["run_id"]
                deadline = time.monotonic() + 20
                while client.get("/api/v3/runs/" + run).json()["status"] != "waiting_approval":
                    if time.monotonic() > deadline:
                        raise RuntimeError("Clean workflow did not reach review")
                    time.sleep(0.1)
                assert client.get("/api/v3/runs/" + run + "/report").is_success
                with httpx.Client(
                    base_url=api, headers={"X-MGAR-Watchdog-Token": watchdog}
                ) as observer:
                    assert observer.get("/api/v3/runs").status_code == 401
                    assert observer.get("/api/v3/watchdog/health").json()["redis"] == "ready"
                    assert observer.get("/api/v3/watchdog/snapshot").is_success
                checks.append("actual HTTP workflow, report and distinct limited watchdog identity")
                # Terminate only this disposable database's Engine session.
                with psycopg.connect(base, autocommit=True) as conn:
                    count = conn.execute(
                        "SELECT count(*) FROM pg_stat_activity WHERE datname=%s", (names[0],)
                    ).fetchone()[0]
                    assert count >= 1
                    conn.execute(
                        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=%s",
                        (names[0],),
                    )
                process.kill()
                process.wait()
                process = start()
                assert client.get("/api/v3/runs/" + run + "/report").is_success
                assert client.post(
                    "/api/v3/runs",
                    json={
                        "project_id": "orders-demo",
                        "question": "Summarize documented symptoms",
                        "issue_number": 1,
                        "client_request_id": "clean-install",
                        "execution_mode": "fixture",
                    },
                ).json()["duplicate"]
                checks.append("real PG session loss and SIGKILL/restart retain report and dedupe")
            process.terminate()
            process.wait(timeout=10)
            process = None
            # Server-local commands avoid putting a DSN/password on a process command line.
            with (output / "backup.sql").open("wb") as dumped:
                subprocess.run(
                    [
                        *DOCKER,
                        "exec",
                        postgres_container,
                        "pg_dump",
                        "-U",
                        "mgar_v3",
                        "--no-owner",
                        "--no-acl",
                        "-d",
                        names[0],
                    ],
                    env=docker_env,
                    stdout=dumped,
                    stderr=subprocess.PIPE,
                    timeout=30,
                    check=True,
                )
            with (output / "backup.sql").open("rb") as dumped:
                restored = subprocess.run(
                    [
                        *DOCKER,
                        "exec",
                        "-i",
                        postgres_container,
                        "psql",
                        "-U",
                        "mgar_v3",
                        "-v",
                        "ON_ERROR_STOP=1",
                        "-d",
                        names[1],
                    ],
                    env=docker_env,
                    stdin=dumped,
                    capture_output=True,
                    timeout=30,
                    check=True,
                )
                (output / "restore.log").write_bytes(restored.stdout)
            runtime["MGAR_V3_DATABASE_URL"] = make_conninfo(base, dbname=names[1])
            process = start()
            with httpx.Client(base_url=api, headers={"X-MGAR-Token": token}) as client:
                assert client.get("/api/v3/runs/" + run + "/report").is_success
                assert len(client.get("/api/v3/runs/" + run + "/actions").json()) == 1
            with psycopg.connect(runtime["MGAR_V3_DATABASE_URL"]) as conn:
                assert (
                    conn.execute("SELECT count(*) FROM app.schema_migrations").fetchone()[0] == 17
                )
                assert conn.execute("SELECT count(*) FROM app.usage_calls").fetchone()[0] == 0
                assert conn.execute("SELECT count(*) FROM app.outbox").fetchone()[0] == 0
            checks.append(
                "actual pg_dump/psql restore to a second new DB; CAS report remains readable"
            )
        finally:
            if process:
                process.terminate()
                process.wait(timeout=10)
            with psycopg.connect(base, autocommit=True) as conn:
                for name in names:
                    assert name.startswith("mgar_v3_deploy_") and len(name) == 47
                    conn.execute(
                        sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                            sql.Identifier(name)
                        )
                    )
    result = {
        "status": "PASS",
        "checks": checks,
        "source_bundle_sha256": release["archive_sha256"],
        "new_paid_calls": 0,
        "external_writes": 0,
        "limits": "fixture business workflow; no host reboot or full PG-node restart tested",
    }
    (output / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("stage", choices=["bundle", "smoke"])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--postgres-container", default="mgar-v3-postgres-1")
    args = parser.parse_args()
    result = (
        bundle(args.output)
        if args.stage == "bundle"
        else smoke(args.output, args.postgres_container)
    )
    print(json.dumps({k: v for k, v in result.items() if k != "files"}, indent=2))


if __name__ == "__main__":
    main()
