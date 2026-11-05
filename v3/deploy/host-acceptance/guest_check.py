"""Assertions inside the dedicated network-isolated systemd acceptance guest.

The private install fork is the only database touched. No provider client is used.
"""

import hashlib
import json
import os
import pwd
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

BASE = Path("/var/lib/mgar-acceptance")
APP = Path("/opt/mgar/v3")
PYTHON = APP / ".venv/bin/python"


def save(path, data):
    encoded = (json.dumps(data, sort_keys=True, default=str) + "\n").encode()
    temp = path.with_suffix(".pending")
    with temp.open("wb") as f:
        os.fchmod(f.fileno(), 0o600)
        f.write(encoded)
        f.flush()
        os.fsync(f.fileno())
    temp.replace(path)
    fd = os.open(path.parent, os.O_DIRECTORY)
    os.fsync(fd)
    os.close(fd)


def command(args, **kwargs):
    return subprocess.run(args, check=True, capture_output=True, text=True, **kwargs).stdout.strip()


def environment():
    for line in Path("/etc/mgar/server.env").read_text().splitlines():
        k, v = line.split("=", 1)
        os.environ[k] = v
    os.environ["PATH"] = "/opt/mgar/v3/.venv/bin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    assert os.environ["MGAR_V3_ALLOW_PAID"] == "0"
    assert os.environ["MGAR_V3_ALLOW_EXTERNAL_WRITE"] == "0"
    assert not any(
        os.environ.get(k) for k in ("MGAR_V3_DEEPSEEK_API_KEY", "GH_TOKEN", "GITHUB_TOKEN")
    )
    return os.environ


def db(name=None):
    import psycopg
    from psycopg.conninfo import make_conninfo
    from psycopg.rows import dict_row

    dsn = os.environ["MGAR_V3_DATABASE_URL"]
    if name:
        dsn = make_conninfo(dsn, dbname=name)
    return psycopg.connect(dsn, autocommit=True, row_factory=dict_row)


def admin(sql=None, database="postgres", file=None):
    args = [
        "runuser",
        "-u",
        "postgres",
        "--",
        "psql",
        "-X",
        "-v",
        "ON_ERROR_STOP=1",
        "-d",
        database,
    ]
    if file:
        sql = file.read_text()  # Root reads private backup; postgres receives stdin only.
    return command(args, input=sql)


def boot_id():
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def original_finance(conn):
    from mgar_v3.benchmark.store import Benchmarks

    return {k: int(v) for k, v in Benchmarks.cost(conn, "loz888/magr-v3").items()}


def cas_digest(root=None):
    result = []
    for f in sorted((root or APP / ".local/artifacts").glob("*/*")):
        if not f.is_file():
            continue
        h = hashlib.sha256(f.read_bytes()).hexdigest()
        assert h == f.name, "CAS content differs from immutable address"
        result.append(h)
    return hashlib.sha256("\n".join(result).encode()).hexdigest(), len(result)


def api():
    import httpx

    return httpx.Client(
        base_url="http://127.0.0.1:18080",
        headers={"X-MGAR-Token": os.environ["MGAR_V3_LOCAL_TOKEN"]},
        timeout=5,
        trust_env=False,
    )


def ready():
    import httpx

    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        try:
            if httpx.get(
                "http://127.0.0.1:18080/api/v3/healthz", timeout=1, trust_env=False
            ).is_success:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    raise AssertionError("Actual systemd API readiness timed out")


def checks():
    ready()
    assert Path("/proc/1/comm").read_text().strip() == "systemd"
    assert command(["systemd-detect-virt"]) == "qemu"
    for service, user in [("mgar-v3", "mgar"), ("mgar-v3-watchdog", "mgar-watchdog")]:
        assert command(["systemctl", "is-active", service]) == "active"
        assert command(["systemctl", "show", service, "-p", "User", "--value"]) == user
        pid = int(command(["systemctl", "show", service, "-p", "MainPID", "--value"]))
        assert os.stat(f"/proc/{pid}").st_uid == pwd.getpwnam(user).pw_uid
        assert command(["systemctl", "show", service, "-p", "NoNewPrivileges", "--value"]) == "yes"
    assert (
        subprocess.run(
            ["runuser", "-u", "mgar-watchdog", "--", "test", "-r", "/etc/mgar/server.env"]
        ).returncode
        != 0
    )
    assert (
        subprocess.run(["runuser", "-u", "mgar", "--", "test", "-w", str(APP / "src")]).returncode
        != 0
    )
    interfaces = json.loads(command(["ip", "-j", "link"]))
    assert {i["ifname"] for i in interfaces} == {"lo"}
    ports = command(["ss", "-lntH"])
    for port in (18080, 5432, 6379):
        matching = [r for r in ports.splitlines() if f":{port} " in r]
        assert matching and all("127.0.0.1:" in r or "[::1]:" in r for r in matching)
    import httpx

    with httpx.Client(
        base_url="http://127.0.0.1:18080",
        headers={"X-MGAR-Watchdog-Token": os.environ["MGAR_V3_WATCHDOG_TOKEN"]},
        trust_env=False,
    ) as c:
        assert c.get("/api/v3/watchdog/health").is_success
        assert c.get("/api/v3/runs").status_code == 401
    with db() as conn:
        assert original_finance(conn) == {"calls": 633, "cost_micros": 299317, "unknown": 0}
        assert conn.execute("SELECT count(*) AS n FROM app.schema_migrations").fetchone()["n"] == 17
        for key in ("fsync", "synchronous_commit", "full_page_writes"):
            assert conn.execute("SHOW " + key).fetchone()[key] == "on"
    return {
        "boot_id": boot_id(),
        "pid1": "systemd",
        "virtualization": "qemu",
        "network": "loopback_only_no_nic",
        "service_users": ["mgar", "mgar-watchdog"],
        "original_finance": {"calls": 633, "cost_micros": 299317, "unknown": 0},
    }


def setup_docker():
    import shutil

    marker = BASE / "sandbox-loaded.json"
    lock = json.loads((BASE / "install-private/sandbox-lock.json").read_text())
    if not marker.exists() or json.loads(marker.read_text())["image_id"] != lock["image_id"]:
        command(
            [
                "docker",
                "--host=unix:///var/run/docker.sock",
                "load",
                "-i",
                str(BASE / "sandbox-image.tar.gz"),
            ]
        )
        target = APP / ".local/r5"
        target.mkdir(mode=0o700, exist_ok=True)
        shutil.copyfile(BASE / "install-private/sandbox-lock.json", target / "sandbox-lock.json")
        command(["chown", "-R", "mgar:mgar", str(target)])
        save(marker, {"image_id": lock["image_id"]})
    # Trusted host parser is required as well as the untrusted execution image.
    parser = BASE / "install-private/astcheck"
    assert hashlib.sha256(parser.read_bytes()).hexdigest() == lock["astcheck_sha256"]
    target = APP / ".local/r5/astcheck"
    shutil.copyfile(parser, target)
    target.chmod(0o700)
    command(["chown", "mgar:mgar", str(target)])
    assert (
        command(
            [
                "docker",
                "--host=unix:///var/run/docker.sock",
                "image",
                "inspect",
                "--format",
                "{{.Id}}",
                lock["image_id"],
            ]
        )
        == lock["image_id"]
    )


def initialize():
    setup_docker()
    marker = BASE / "initialized.json"
    if marker.exists():
        return {"initialized": "already_present"}
    # Original snapshots stay unchanged; filter only the pre-existing guest admin role.
    roles = (BASE / "install-private/roles.sql").read_text()
    roles = "\n".join(line for line in roles.splitlines() if line != "CREATE ROLE postgres;")
    local_roles = BASE / "roles-for-guest.sql"
    local_roles.write_text(roles)
    local_roles.chmod(0o600)
    if not (BASE / "roles-restored.json").exists():
        admin(file=local_roles)
        save(BASE / "roles-restored.json", {"roles_restored": True})
    from psycopg.conninfo import conninfo_to_dict

    password = conninfo_to_dict(os.environ["MGAR_V3_DATABASE_URL"])["password"]
    assert all(c in "0123456789abcdef" for c in password)
    if not (BASE / "database-restored.json").exists():
        admin("ALTER ROLE mgar_v3 PASSWORD '" + password + "';")
        admin(
            "CREATE DATABASE mgar_v3 OWNER mgar_v3 TEMPLATE template0 "
            "ENCODING 'UTF8' LC_COLLATE 'C.UTF-8' LC_CTYPE 'C.UTF-8';"
        )
        admin(database="mgar_v3", file=BASE / "install-private/database.sql")
        save(BASE / "database-restored.json", {"database_restored": True, "encoding": "UTF8"})
    import shutil

    shutil.copytree(
        BASE / "install-private/artifacts", APP / ".local/artifacts", dirs_exist_ok=True
    )
    command(["chown", "-R", "mgar:mgar", str(APP / ".local")])
    command(["chmod", "-R", "go-rwx", str(APP / ".local")])
    for _ in range(2):
        command([str(PYTHON), "-m", "mgar_v3.store.migrate"], env=dict(os.environ), cwd=APP)
    with db() as conn:
        assert original_finance(conn) == {"calls": 633, "cost_micros": 299317, "unknown": 0}
    save(marker, {"restored_original_finance": True, "migrations": 17, "CAS": cas_digest()})
    return {"initialized": True, "original_ledger_restored": True}


def sandbox_canary():
    from mgar_v3.repair.sandbox import Sandbox

    files = {
        "go.mod": "module example.invalid/mgar-v3/orders\n\ngo 1.27.3\n",
        "orders.go": (
            "package orders\ntype Order struct {ID string; Status string; Amount int}\n"
            "func PaidRevenueCents(orders []Order) int {total:=0;for _,o:=range orders {"
            'if o.Status=="paid" {total+=o.Amount}};return total}\n'
        ),
        "orders_test.go": (
            'package orders\nimport "testing"\nfunc TestPaidRevenueCents(t *testing.T){'
            'if got:=PaidRevenueCents([]Order{{"a","paid",2},{"b","cancelled",9}});'
            'got!=2{t.Fatalf("got %d",got)}}\n'
        ),
    }
    sandbox = Sandbox("isolated-real-host-canary")
    proof, raw = sandbox.run(files, ["TestPaidRevenueCents"], timeout=45)
    if not proof["passed"]:
        print(
            json.dumps(
                {"failed_sandbox_receipt": proof, "sandbox_output": raw.decode(errors="replace")}
            ),
            flush=True,
        )
    assert proof["passed"] and proof["policy"]["network"] == "none"
    assert proof["policy"]["user"] == "65532:65532" and proof["policy"]["read_only"]
    return {
        "actual_docker_go_sandbox": True,
        "proof": proof,
        "log_sha256": hashlib.sha256(raw).hexdigest(),
    }


def before():
    report = checks()
    canary = command(["runuser", "-u", "mgar", "--", str(PYTHON), __file__, "sandbox-canary"])
    report["sandbox"] = json.loads(canary)["checks"]
    body = {
        "project_id": "orders-demo",
        "question": "Summarize documented symptoms",
        "issue_number": 98001,
        "client_request_id": "host-node-persistent-fixture",
        "execution_mode": "fixture",
    }
    with api() as c:
        r = c.post("/api/v3/runs", json=body)
        r.raise_for_status()
        run = r.json()["run_id"]
        deadline = time.monotonic() + 60
        while c.get("/api/v3/runs/" + run).json()["status"] != "waiting_approval":
            assert time.monotonic() < deadline, "Fixture workflow failed to reach review"
            time.sleep(0.1)
        response = c.get("/api/v3/runs/" + run + "/report")
        response.raise_for_status()
        report_hash = hashlib.sha256(response.content).hexdigest()
    # This table is only a fault probe in the isolated fork, never Engine business state.
    with db() as conn:
        conn.execute("CREATE SCHEMA host_acceptance")
        conn.execute("CREATE TABLE host_acceptance.probe(value text PRIMARY KEY)")
        conn.execute("INSERT INTO host_acceptance.probe VALUES ('committed')")
    state = {"first_boot_id": boot_id(), "body": body, "run_id": run, "report_sha256": report_hash}
    save(BASE / "state.json", state)
    report.update(
        {
            "fixture_run": run,
            "report_sha256": report_hash,
            "committed_probe": True,
            "cas": cas_digest(),
        }
    )
    return report


def verify_work():
    state = json.loads((BASE / "state.json").read_text())
    with api() as c:
        r = c.get("/api/v3/runs/" + state["run_id"] + "/report")
        r.raise_for_status()
        assert hashlib.sha256(r.content).hexdigest() == state["report_sha256"]
        duplicate = c.post("/api/v3/runs", json=state["body"]).json()
        assert duplicate["duplicate"] and duplicate["run_id"] == state["run_id"]
    with db() as conn:
        values = conn.execute("SELECT value FROM host_acceptance.probe").fetchall()
        assert values == [{"value": "committed"}]
    return {
        "report_unchanged": True,
        "dedupe_unchanged": True,
        "committed_probe_present": True,
        "uncommitted_probe_absent": True,
        "cas": cas_digest(),
    }


def pending_tx():
    import psycopg

    with psycopg.connect(
        os.environ["MGAR_V3_DATABASE_URL"], application_name="mgar_acceptance_uncommitted"
    ) as conn:
        conn.execute("INSERT INTO host_acceptance.probe VALUES ('uncommitted')")
        save(BASE / "transaction-ready.json", {"uncommitted_insert_executed": True})
        while True:
            signal.pause()


def prepare_cut():
    with (BASE / "pending-transaction.log").open("wb") as log:
        subprocess.Popen(
            [str(PYTHON), __file__, "pending-tx"],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
    deadline = time.monotonic() + 10
    while not (BASE / "transaction-ready.json").exists():
        assert time.monotonic() < deadline
        time.sleep(0.1)
    with db() as conn:
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM pg_stat_activity "
                "WHERE application_name='mgar_acceptance_uncommitted' "
                "AND state='idle in transaction'"
            ).fetchone()["n"]
            == 1
        )
    save(BASE / "before-cut.json", {"boot_id": boot_id(), "cas": cas_digest()})
    return {
        "boot_id": boot_id(),
        "active_uncommitted_backend": True,
        "engine_service_active": command(["systemctl", "is-active", "mgar-v3"]) == "active",
    }


def after_cut():
    report = checks()
    prior = json.loads((BASE / "before-cut.json").read_text())
    assert boot_id() != prior["boot_id"]
    report.update(verify_work())
    assert list(cas_digest())[:1] == prior["cas"][:1]
    log = Path("/var/lib/postgresql/mgar/server.log").read_text()
    assert "database system was interrupted" in log and "redo starts at" in log
    report.update({"postgres_crash_recovery": True, "new_boot_id": True})
    return report


def after_normal():
    report = checks()
    report.update(verify_work())
    assert boot_id() != json.loads((BASE / "before-cut.json").read_text())["boot_id"]
    return report


def seed_fault():
    from pydantic import SecretStr

    from mgar_v3.coordination.engine import COMMAND, Engine
    from mgar_v3.domain import TriggerInput
    from mgar_v3.evidence.artifacts import ArtifactStore
    from mgar_v3.live import Authorization, LivePermit, LiveProfile
    from mgar_v3.settings import Settings

    cfg = Settings(
        allow_paid=True,
        deepseek_api_key=SecretStr("controlled-no-provider-call"),
        graph_version="r2-mailbox-v1",
    )
    p = LiveProfile(
        case_id="controlled-host-fault",
        project_id="orders-demo",
        repository=cfg.repository,
        issue_number=98002,
        actor="loz888",
        question="Controlled reservation recovery, no remote request",
        source_ref="a" * 40,
        model_id="controlled-offline-host",
        price_version="controlled-not-billing",
        input_micros_per_million=1000000,
        output_micros_per_million=1000000,
        context_tokens=4096,
        max_output_tokens=256,
        max_cost_micros=5000,
    )
    auth = Authorization(
        profile_hash=p.profile_hash,
        approved_by="loz888",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        allow_paid=True,
        allow_external_write=False,
    )
    permit = LivePermit(p, auth, cfg)
    engine = Engine(cfg, ArtifactStore(cfg.artifact_root))
    receipt = engine.ingest(
        TriggerInput(
            source="cli",
            external_event_id="controlled-node-fault-reservation",
            project_id="orders-demo",
            repository=cfg.repository,
            issue_number=98002,
            actor="loz888",
            body=COMMAND + p.question,
            execution_mode="live",
        ),
        permit,
    )
    run = engine.begin_run(receipt.run_id)
    call = engine.reserve_call(
        receipt.run_id, run["execution_epoch"], "never-sent-controlled", permit
    )
    result = {
        "run_id": str(receipt.run_id),
        "call_id": str(call),
        "old_epoch": run["execution_epoch"],
        "profile_hash": p.profile_hash,
        "reservation_micros": p.reservation_micros,
        "boot_id": boot_id(),
        "physical_model_requests": 0,
    }
    save(BASE / "controlled-inflight.json", result)
    while True:
        signal.pause()  # Engine and PG connection remain alive until the entire VM is cut.


def prepare_inflight():
    command(["systemctl", "stop", "mgar-v3-watchdog", "mgar-v3"])
    with (BASE / "controlled-inflight.log").open("wb") as log:
        subprocess.Popen(
            [str(PYTHON), __file__, "seed-fault"],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
    deadline = time.monotonic() + 15
    while not (BASE / "controlled-inflight.json").exists():
        assert time.monotonic() < deadline, "Controlled Engine reservation did not start"
        time.sleep(0.1)
    result = json.loads((BASE / "controlled-inflight.json").read_text())
    with db() as conn:
        row = conn.execute(
            "SELECT status FROM app.usage_calls WHERE call_id=%s", (result["call_id"],)
        ).fetchone()
        assert row["status"] == "reserved"
    return result


def after_inflight():
    from mgar_v3.coordination.engine import Engine
    from mgar_v3.domain import DomainError
    from mgar_v3.evidence.artifacts import ArtifactStore
    from mgar_v3.settings import Settings

    result = checks()
    s = json.loads((BASE / "controlled-inflight.json").read_text())
    assert boot_id() != s["boot_id"]
    with db() as conn:
        call = conn.execute(
            "SELECT * FROM app.usage_calls WHERE call_id=%s", (s["call_id"],)
        ).fetchone()
        run = conn.execute(
            "SELECT status,execution_epoch,reason_code FROM app.runs WHERE run_id=%s",
            (s["run_id"],),
        ).fetchone()
        budget = conn.execute(
            "SELECT used_calls,reserved_micros,settled_micros FROM app.authorization_budgets "
            "WHERE profile_hash=%s",
            (s["profile_hash"],),
        ).fetchone()
        assert call["status"] == "unknown" and call["settled_micros"] is None
        assert run["status"] == "paused" and run["execution_epoch"] == s["old_epoch"] + 1
        assert budget == {
            "used_calls": 1,
            "reserved_micros": s["reservation_micros"],
            "settled_micros": 0,
        }
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM app.usage_calls WHERE run_id=%s", (s["run_id"],)
            ).fetchone()["n"]
            == 1
        )
    command(["systemctl", "stop", "mgar-v3-watchdog", "mgar-v3"])
    cfg = Settings()
    engine = Engine(cfg, ArtifactStore(cfg.artifact_root))
    try:
        try:
            engine.recovery.heartbeat(UUID(s["run_id"]), s["old_epoch"])
            raise AssertionError("Old epoch accepted")
        except DomainError as e:
            assert e.code == "FENCED"
        try:
            engine.resume(UUID(s["run_id"]))
            raise AssertionError("Unknown request resumed")
        except DomainError as e:
            assert e.code == "RECOVERY_BLOCKED"
    finally:
        engine.close()
    command(["systemctl", "start", "mgar-v3", "mgar-v3-watchdog"])
    ready()
    result.update(
        {
            "controlled_reserved_became_unknown": True,
            "reservation_kept_micros": s["reservation_micros"],
            "old_epoch_fenced": True,
            "unknown_resume_blocked": True,
            "no_resend": True,
            "physical_model_requests": 0,
        }
    )
    return result


def backup_restore():
    import shutil

    import psycopg
    from psycopg.conninfo import make_conninfo

    command(["systemctl", "stop", "mgar-v3-watchdog", "mgar-v3"])
    backup = BASE / "verified-backup.sql"
    with backup.open("wb") as out:
        subprocess.run(
            ["runuser", "-u", "postgres", "--", "pg_dump", "--no-owner", "-d", "mgar_v3"],
            stdout=out,
            check=True,
        )
    backup.chmod(0o600)
    admin(
        "CREATE DATABASE mgar_v3_restored OWNER mgar_v3 TEMPLATE template0 "
        "ENCODING 'UTF8' LC_COLLATE 'C.UTF-8' LC_CTYPE 'C.UTF-8';"
    )
    admin(database="mgar_v3_restored", file=backup)
    with db("mgar_v3_restored") as conn:
        assert original_finance(conn) == {"calls": 633, "cost_micros": 299317, "unknown": 0}
        s = json.loads((BASE / "controlled-inflight.json").read_text())
        assert (
            conn.execute(
                "SELECT status FROM app.usage_calls WHERE call_id=%s", (s["call_id"],)
            ).fetchone()["status"]
            == "unknown"
        )
        assert (
            conn.execute(
                "SELECT reserved_micros FROM app.authorization_budgets WHERE profile_hash=%s",
                (s["profile_hash"],),
            ).fetchone()["reserved_micros"]
            == s["reservation_micros"]
        )
    reader = json.loads((BASE / "install-private/reader.json").read_text())
    # Explicit reader configuration is consumed privately; no credential is printed.
    reader_dsn = reader.get("dsn") or reader.get("database_url")
    if not reader_dsn:
        raise AssertionError("Reader profile has no configured DSN")
    rd = make_conninfo(reader_dsn, host="127.0.0.1", port=5432, dbname="mgar_v3_restored")
    from mgar_v3.agents.data import DataReader

    restored_reader = DataReader({**reader, "database_url": rd})
    with psycopg.connect(**restored_reader.connect) as conn:
        assert conn.execute("SHOW default_transaction_read_only").fetchone()[0] == "on"
        schema = reader["schema"]
        from psycopg import sql

        assert (
            conn.execute(
                sql.SQL("SELECT count(*) FROM {}.orders").format(sql.Identifier(schema))
            ).fetchone()[0]
            >= 0
        )
        assert not conn.execute(
            "SELECT has_table_privilege(current_user,%s,%s)", (schema + ".orders", "INSERT")
        ).fetchone()[0]
    # Exercise the actual production reader, including fixture hash and ACL rechecks.
    restored_reader.query("SELECT count(*) AS n FROM " + reader["schema"] + ".orders", [])
    with psycopg.connect(rd) as conn:
        try:
            conn.execute(
                sql.SQL("INSERT INTO {}.orders SELECT * FROM {}.orders WHERE FALSE").format(
                    sql.Identifier(reader["schema"]), sql.Identifier(reader["schema"])
                )
            )
        except psycopg.errors.InsufficientPrivilege:
            conn.rollback()
        else:
            conn.rollback()
            raise AssertionError("Restored Reader could write with session read-only disabled")
    old_cas = cas_digest()
    target = APP / ".local/restored-artifacts"
    shutil.copytree(APP / ".local/artifacts", target)
    command(["chown", "-R", "mgar:mgar", str(target)])
    config = Path("/etc/mgar/server.env")
    raw = (
        config.read_text()
        .replace("/mgar_v3\n", "/mgar_v3_restored\n")
        .replace(
            "MGAR_V3_ARTIFACT_ROOT=/opt/mgar/v3/.local/artifacts",
            "MGAR_V3_ARTIFACT_ROOT=/opt/mgar/v3/.local/restored-artifacts",
        )
    )
    save(
        BASE / "backup-proof.json",
        {"backup_sha256": hashlib.sha256(backup.read_bytes()).hexdigest(), "cas": old_cas},
    )
    config.write_text(raw)
    config.chmod(0o600)
    environment()
    assert cas_digest(target) == old_cas
    command(["systemctl", "start", "mgar-v3", "mgar-v3-watchdog"])
    ready()
    result = verify_work()
    result.update(
        {
            "pg_dump_restore": True,
            "reader_role_restored_readonly": True,
            "actual_production_DataReader_query": True,
            "write_denied_with_session_readonly_disabled": True,
            "restored_CAS_bytes_verified": True,
            "unknown_budget_restored": True,
            "http_from_restored_database": True,
            "backup_sha256": hashlib.sha256(backup.read_bytes()).hexdigest(),
        }
    )
    return result



def runtime_assets():
    # Supplemental check after all seven fault stages, against the restored service.
    setup_docker()
    result = checks()
    result.update(verify_work())
    with db() as conn:
        assert conn.execute("SELECT current_database() AS name").fetchone()["name"] == (
            "mgar_v3_restored"
        )
    target = Path(os.environ["MGAR_V3_ARTIFACT_ROOT"])
    assert target == APP / ".local/restored-artifacts"
    hashes = []
    for f in sorted(target.glob("*/*")):
        if f.is_file():
            h = hashlib.sha256(f.read_bytes()).hexdigest()
            assert h == f.name
            hashes.append(h)
    restored = (hashlib.sha256("\n".join(hashes).encode()).hexdigest(), len(hashes))
    assert restored == cas_digest()
    code = (
        "from mgar_v3.repair.workspace import ast_check\n"
        "from mgar_v3.domain import DomainError\n"
        "source='package orders\\ntype Order struct {ID string; Status string; Amount int}"
        "\\nfunc PaidRevenueCents(orders []Order) int {return 0}\\n'\n"
        "ast_check(source, source.replace('return 0','return 1'))\n"
        "try:\n"
        "    ast_check(source, source.replace('package orders',"
        "'package orders\\nimport \"os\"'))\n"
        "except DomainError as exc:\n"
        "    assert exc.code == 'PATCH_AST_DENIED'\n"
        "else:\n"
        "    raise AssertionError('Import change accepted')\n"
        "print('trusted_AST_accept_and_deny_PASS')\n"
    )
    assert command(["runuser", "-u", "mgar", "--", str(PYTHON), "-c", code]) == (
        "trusted_AST_accept_and_deny_PASS"
    )
    result.update(
        {
            "trusted_ast_helper_present_hash_verified": True,
            "ast_valid_body_accepted_import_change_denied_as_service_user": True,
            "actual_restored_database_probes_verified": True,
            "restored_CAS_hashes_equal_original": True,
            "restored_CAS": restored,
            "physical_model_requests": 0,
        }
    )
    return result


def main():
    stage = sys.argv[1]
    if stage != "sandbox-canary":
        environment()
    handlers = {
        "initialize": initialize,
        "runtime-assets": runtime_assets,
        "sandbox-canary": sandbox_canary,
        "before": before,
        "pending-tx": pending_tx,
        "prepare-cut": prepare_cut,
        "after-cut": after_cut,
        "after-normal": after_normal,
        "seed-fault": seed_fault,
        "prepare-inflight": prepare_inflight,
        "after-inflight": after_inflight,
        "backup-restore": backup_restore,
    }
    result = handlers[stage]()
    print(json.dumps({"stage": stage, "status": "PASS", "checks": result}, default=str), flush=True)


if __name__ == "__main__":
    main()
