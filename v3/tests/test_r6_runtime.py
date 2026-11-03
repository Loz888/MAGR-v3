import sys
import time
from pathlib import Path

import pytest
from pydantic import SecretStr
from redis.exceptions import RedisError

from mgar_v3.domain import DomainError
from mgar_v3.graph.runner import Runner
from mgar_v3.mcp.client import ReadOnlyGitHubMCP
from mgar_v3.recovery.domain import Decision, detect
from mgar_v3.recovery.health import probe
from mgar_v3.recovery.heartbeat import worker_heartbeat


def running(engine, trigger):
    engine.settings.graph_version = "r2-mailbox-v1"
    receipt = engine.ingest(trigger)
    run = engine.begin_run(receipt.run_id)
    return receipt.run_id, run["execution_epoch"]


def test_heartbeats_do_not_forge_progress_and_old_epoch_cannot_renew(engine, trigger):
    run_id, epoch = running(engine, trigger)
    before = engine.recovery.snapshot(run_id).items[0].progress_cursor
    engine.recovery.heartbeat(run_id, epoch)
    assert engine.recovery.snapshot(run_id).items[0].progress_cursor == before
    engine.pause(run_id)
    with pytest.raises(DomainError):
        engine.recovery.heartbeat(run_id, epoch)


def test_expired_worker_signal_revalidates_before_manual_control(engine, trigger):
    run_id, epoch = running(engine, trigger)
    with engine.transaction() as conn:
        conn.execute("UPDATE app.worker_heartbeats SET expires_at=now()-interval '1 second'")
    signal = detect(engine.recovery.snapshot(run_id))[0]
    assert signal.kind == "heartbeat_expired"
    request = engine.recovery.submit(signal)
    engine.recovery.heartbeat(run_id, epoch)
    changed = engine.recovery.decide(request["request_id"], Decision(decision="pause"), "owner")
    assert changed["status"] == "stale" and engine.get_run(run_id)["status"] == "running"


def test_real_worker_side_heartbeat_thread_updates_lease(engine, trigger):
    run_id, epoch = running(engine, trigger)
    with worker_heartbeat(engine.recovery, run_id, lambda: epoch):
        limit = time.monotonic() + 7
        while time.monotonic() < limit:
            with engine.transaction() as conn:
                row = conn.execute(
                    "SELECT sequence FROM app.worker_heartbeats WHERE run_id=%s", (run_id,)
                ).fetchone()
            if row["sequence"] > 0:
                break
            time.sleep(0.05)
        assert row["sequence"] > 0


def test_mailbox_and_global_router_backlogs_are_scoped_and_suggestion_only(engine, trigger):
    run_id, _ = running(engine, trigger)
    for i in range(15):
        engine.follow_up(run_id, "pending-" + str(i), "Additional input")
    assert detect(engine.recovery.snapshot(run_id))[0].kind == "mailbox_backlog"
    for i in range(16):
        engine.ingest(
            trigger.model_copy(
                update={
                    "issue_number": 100 + i,
                    "external_event_id": "unrouted-" + str(i),
                    "body": "Unrelated event",
                }
            )
        )
    snapshot = engine.recovery.snapshot()
    global_signal = next(s for s in detect(snapshot) if s.kind == "router_backlog")
    assert global_signal.run_id is None
    request = engine.recovery.submit(global_signal)
    with pytest.raises(DomainError):
        engine.recovery.decide(request["request_id"], Decision(decision="cancel"), "owner")
    assert (
        engine.recovery.decide(request["request_id"], Decision(decision="escalate"), "owner")[
            "status"
        ]
        == "needs_human"
    )
    assert engine.get_run(run_id)["status"] == "running"
    other = snapshot.model_copy(update={"project_id": "different-project"})
    assert global_signal.observation_key not in {s.observation_key for s in detect(other)}


def test_real_outbox_backlog_never_dispatches_from_watchdog(engine, trigger):
    for i in range(16):
        receipt = engine.ingest(
            trigger.model_copy(
                update={"issue_number": i + 100, "external_event_id": "pending-" + str(i)}
            )
        )
        action_id = Runner(engine).execute(receipt.run_id)
        action = engine.actions(receipt.run_id)[0]
        engine.approve(action_id, action["payload_hash"], "owner")
    signal = next(s for s in detect(engine.recovery.snapshot()) if s.kind == "outbox_backlog")
    request = engine.recovery.submit(signal)
    engine.recovery.decide(request["request_id"], Decision(decision="escalate"), "owner")
    with engine.transaction() as conn:
        assert (
            conn.execute("SELECT count(*) AS n FROM app.outbox WHERE status='ready'").fetchone()[
                "n"
            ]
            == 16
        )
        assert not conn.execute("SELECT 1 FROM app.actions WHERE status='dispatching'").fetchone()


def test_real_pg_redis_and_mcp_health_and_repeated_failure_generations(engine, monkeypatch):
    engine.settings.github_read_token = SecretStr("controlled-mcp-health-token")
    fixture = Path(__file__).parent / "fixtures/mcp_server.py"
    monkeypatch.setattr(
        "mgar_v3.recovery.health.github_client",
        lambda settings, repository: ReadOnlyGitHubMCP(
            repository, command=sys.executable, args=(str(fixture),)
        ),
    )
    assert probe(engine) == {"postgres": "ready", "redis": "ready", "mcp": "ready"}
    with monkeypatch.context() as broken:
        broken.setattr(
            "mgar_v3.recovery.health.Redis.from_url",
            lambda *a, **k: (_ for _ in ()).throw(RedisError("down")),
        )
        assert probe(engine)["redis"] == "unavailable"
        first = next(
            s for s in detect(engine.recovery.snapshot()) if s.kind == "dependency_unavailable"
        )
        probe(engine)
        assert first in detect(engine.recovery.snapshot())
        engine.recovery.submit(first)
    assert probe(engine)["redis"] == "ready"
    with monkeypatch.context() as broken:
        broken.setattr(
            "mgar_v3.recovery.health.Redis.from_url",
            lambda *a, **k: (_ for _ in ()).throw(RedisError("down")),
        )
        probe(engine)
        second = next(
            s for s in detect(engine.recovery.snapshot()) if s.kind == "dependency_unavailable"
        )
    assert second.observation_key != first.observation_key


def test_actual_redis_stop_keeps_pg_work_and_recovers_without_dispatch(engine, trigger):
    from uuid import uuid4

    from mgar_v3.repair.sandbox import command

    image = (
        "redis:7.4.7-bookworm@sha256:"
        "976ab0f0939b78c06d802bcd3bea44c5142ea9a756a57b861e08819817f768c8"
    )
    import socket

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        host_port = listener.getsockname()[1]
    name = "mgar-v3-r6-redis-" + uuid4().hex
    created = command(
        [
            "run",
            "-d",
            "--name",
            name,
            "--label",
            "mgar.v3.r6.redis=1",
            "-p",
            f"127.0.0.1:{host_port}:6379",
            image,
            "redis-server",
            "--save",
            "",
            "--appendonly",
            "no",
        ]
    )
    assert created.returncode == 0, created.stderr.decode()
    try:
        port = command(["port", name, "6379"]).stdout.decode().strip().rsplit(":", 1)[1]
        engine.settings.redis_url = SecretStr("redis://127.0.0.1:" + port + "/0")
        deadline = time.monotonic() + 10
        while probe(engine)["redis"] != "ready":
            assert time.monotonic() < deadline
            time.sleep(0.1)
        run_id, _ = running(engine, trigger)
        assert command(["stop", "-t", "1", name]).returncode == 0
        assert probe(engine)["redis"] == "unavailable"
        signal = next(
            s for s in detect(engine.recovery.snapshot()) if s.kind == "dependency_unavailable"
        )
        engine.recovery.submit(signal)
        assert engine.get_run(run_id)["status"] == "running"
        assert command(["start", name]).returncode == 0
        deadline = time.monotonic() + 10
        while probe(engine)["redis"] != "ready":
            assert time.monotonic() < deadline
            time.sleep(0.1)
        assert engine.get_run(run_id)["status"] == "running"
        with engine.transaction() as conn:
            assert not conn.execute("SELECT 1 FROM app.outbox").fetchone()
    finally:
        command(["rm", "-f", name])
