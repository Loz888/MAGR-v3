import json
import os
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import psycopg
import pytest
import uvicorn
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
from test_effects import FakeRemote

from mgar_v3.api import create_app
from mgar_v3.domain import DomainError, Verification
from mgar_v3.effects.dispatcher import Dispatcher
from mgar_v3.graph.runner import Runner
from mgar_v3.recovery.domain import Decision, Observation, RecoveryInput, Snapshot, detect
from mgar_v3.recovery.watchdog import Configuration
from mgar_v3.recovery.watchdog import main as watchdog_main
from mgar_v3.settings import ROOT


def stalled(engine, trigger):
    engine.settings.graph_version = "r2-mailbox-v1"
    receipt = engine.ingest(trigger)
    run = engine.begin_run(receipt.run_id)
    with engine.transaction() as conn:
        conn.execute(
            "UPDATE app.run_events SET created_at=now()-interval '2 hours' WHERE run_id=%s",
            (receipt.run_id,),
        )
    return receipt.run_id, run["execution_epoch"]


@pytest.mark.parametrize(
    "status,age,expected",
    [
        ("running", 119, None),
        ("running", 120, "no_progress"),
        ("queued", 120, "no_progress"),
        ("waiting_input", 3600, "waiting_input"),
        ("waiting_approval", 3599, None),
        ("waiting_approval", 3600, "waiting_approval"),
    ],
)
def test_frozen_deterministic_thresholds(status, age, expected):
    now = datetime.now(UTC)
    item = Observation(
        run_id=uuid4(),
        execution_epoch=1,
        status=status,
        progress_at=now - timedelta(seconds=age),
        progress_cursor=10,
        unknown_action_id=None,
    )
    found = detect(Snapshot(sampled_at=now, items=[item]))
    assert ([s.kind for s in found] == [expected]) if expected else not found


def test_concurrent_observations_dedupe_without_progress_or_business_side_effects(engine, trigger):
    run_id, epoch = stalled(engine, trigger)
    signal = detect(engine.recovery.snapshot())[0]
    with ThreadPoolExecutor(max_workers=4) as pool:
        rows = list(pool.map(lambda _: engine.recovery.submit(signal), range(8)))
    assert len({r["request_id"] for r in rows}) == 1 and sum(r["created"] for r in rows) == 1
    assert detect(engine.recovery.snapshot()) == [signal]
    assert engine.get_run(run_id)["execution_epoch"] == epoch
    assert engine.get_run(run_id)["status"] == "running"
    with engine.transaction() as conn:
        assert not conn.execute(
            "SELECT 1 FROM app.outbox UNION ALL SELECT 1 FROM app.usage_calls"
        ).fetchone()
    with pytest.raises(psycopg.errors.RaiseException, match="immutable"):
        with engine.transaction() as conn:
            conn.execute("UPDATE app.recovery_requests SET evidence='{}'")


def test_manual_pause_fences_old_worker_and_stale_observations(engine, trigger):
    run_id, old_epoch = stalled(engine, trigger)
    signal = detect(engine.recovery.snapshot())[0]
    request = engine.recovery.submit(signal)
    with pytest.raises(DomainError, match="Actor"):
        engine.recovery.decide(request["request_id"], Decision(decision="pause"), "outsider")
    result = engine.recovery.decide(request["request_id"], Decision(decision="pause"), "owner")
    assert result["status"] == "resolved" and engine.get_run(run_id)["status"] == "paused"
    assert (
        engine.recovery.decide(request["request_id"], Decision(decision="pause"), "owner") == result
    )
    engine.resume(run_id)
    current = engine.begin_run(run_id)
    assert current["execution_epoch"] > old_epoch
    with pytest.raises(DomainError) as denied:
        engine.commit_report(
            run_id, old_epoch, b"late result", Verification(passed=True, checks=[], reasons=[])
        )
    assert denied.value.code == "FENCED"
    forged = signal.model_copy(update={"observation_key": "a" * 64})
    with pytest.raises(DomainError, match="Current facts"):
        engine.recovery.submit(forged)


def test_new_progress_makes_pending_recovery_stale(engine, trigger):
    run_id, _ = stalled(engine, trigger)
    request = engine.recovery.submit(detect(engine.recovery.snapshot())[0])
    with engine.transaction() as conn:
        engine._event(conn, run_id, "tool_result_committed")
    result = engine.recovery.decide(request["request_id"], Decision(decision="cancel"), "owner")
    assert result["status"] == "stale" and engine.get_run(run_id)["status"] == "running"


def test_unknown_effects_escalate_even_after_cancel_and_never_replay(engine, trigger):
    receipt = engine.ingest(trigger)
    action_id = Runner(engine).execute(receipt.run_id)
    action = engine.actions(receipt.run_id)[0]
    engine.approve(action_id, action["payload_hash"], "owner")
    remote = FakeRemote()
    remote.timeout_after_accept = True
    dispatcher = Dispatcher(engine, remote)
    dispatcher.tick()
    engine.cancel(receipt.run_id)
    signal = detect(engine.recovery.snapshot())[0]
    assert signal.kind == "outcome_unknown"
    request = engine.recovery.submit(signal)
    with pytest.raises(DomainError, match="only permit"):
        engine.recovery.decide(request["request_id"], Decision(decision="pause"), "owner")
    assert (
        engine.recovery.decide(request["request_id"], Decision(decision="escalate"), "owner")[
            "status"
        ]
        == "needs_human"
    )
    assert dispatcher.tick() is None and len(remote.posts) == 1
    assert engine.actions(receipt.run_id)[0]["status"] == "outcome_unknown"


def test_watchdog_token_has_only_observe_and_submit_capabilities(engine, trigger):
    stalled(engine, trigger)
    engine.settings.watchdog_token = SecretStr("watchdog-only-token-with-sufficient-length")
    wd = {"X-MGAR-Watchdog-Token": engine.settings.watchdog_token.get_secret_value()}
    admin = {"X-MGAR-Token": engine.settings.local_token.get_secret_value()}
    with TestClient(
        create_app(engine.settings, engine=engine, run_background=False),
        base_url="http://127.0.0.1:18080",
    ) as client:
        response = client.get("/api/v3/watchdog/snapshot", headers=wd)
        assert response.status_code == 200
        assert "question" not in response.text and "payload" not in response.text
        signal = detect(Snapshot.model_validate(response.json()))[0]
        posted = client.post(
            "/api/v3/watchdog/recovery-requests", headers=wd, json=signal.model_dump(mode="json")
        )
        assert posted.status_code == 200
        key = posted.json()["request_id"]
        assert client.get("/api/v3/runs", headers=wd).status_code == 401
        assert (
            client.post(
                f"/api/v3/recovery-requests/{key}/decision", headers=wd, json={"decision": "cancel"}
            ).status_code
            == 401
        )
        assert client.get("/api/v3/watchdog/snapshot", headers=admin).status_code == 401
        assert (
            client.post(
                "/api/v3/watchdog/recovery-requests",
                headers=wd,
                json={**signal.model_dump(mode="json"), "action": "publish"},
            ).status_code
            == 422
        )
        assert client.get("/api/v3/recovery-requests", headers=admin).status_code == 200


def test_separate_watchdog_process_uses_real_http_without_model_or_database_credentials(
    engine, trigger, tmp_path
):
    run_id, epoch = stalled(engine, trigger)
    token = "independent-observer-private-test-token"
    engine.settings.watchdog_token = SecretStr(token)
    app = create_app(engine.settings, engine=engine, run_background=False)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
    thread.start()
    config = tmp_path / "watchdog.json"
    config.write_text(json.dumps({"api_url": f"http://127.0.0.1:{port}", "token": token}))
    config.chmod(0o600)
    try:
        limit = time.monotonic() + 5
        while not server.started and time.monotonic() < limit:
            time.sleep(0.02)
        assert server.started
        args = [
            sys.executable,
            "-m",
            "mgar_v3.recovery.watchdog",
            "--config",
            str(config),
            "--once",
        ]
        env = {"PATH": os.environ["PATH"], "LANG": "C.UTF-8"}
        first = subprocess.run(args, cwd=ROOT, env=env, capture_output=True, text=True, timeout=15)
        assert first.returncode == 0, first.stderr
        assert json.loads(first.stdout)["created"] == 1 and token not in first.stdout + first.stderr
        second = subprocess.run(args, cwd=ROOT, env=env, capture_output=True, text=True, timeout=15)
        assert second.returncode == 0 and json.loads(second.stdout)["created"] == 0
        assert len(engine.recovery.list()) == 1
        assert (
            engine.get_run(run_id)["status"] == "running"
            and engine.get_run(run_id)["execution_epoch"] == epoch
        )
        config.chmod(0o644)
        bad = subprocess.run(args, cwd=ROOT, env=env, capture_output=True, text=True, timeout=15)
        assert bad.returncode == 2 and token not in bad.stdout + bad.stderr
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        sock.close()


def test_observer_cannot_send_its_token_to_external_or_redirect_urls():
    for url in [
        "https://example.com",
        "http://127.0.0.1@evil.example",
        "http://localhost/?next=outside",
    ]:
        with pytest.raises(ValidationError):
            Configuration(api_url=url, token="limited-observer-token-only-1234")


def test_recovery_decision_and_pause_roll_back_together(engine, trigger):
    run_id, epoch = stalled(engine, trigger)
    request = engine.recovery.submit(detect(engine.recovery.snapshot())[0])
    with engine.transaction() as conn:
        conn.execute(
            "CREATE FUNCTION app.fail_recovery_decision() RETURNS trigger "
            "LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'injected recovery failure'; END $$"
        )
        conn.execute(
            "CREATE TRIGGER fail_recovery_decision BEFORE UPDATE ON app.recovery_requests "
            "FOR EACH ROW EXECUTE FUNCTION app.fail_recovery_decision()"
        )
    try:
        with pytest.raises(psycopg.errors.RaiseException, match="injected recovery"):
            engine.recovery.decide(request["request_id"], Decision(decision="pause"), "owner")
        assert engine.get_run(run_id)["status"] == "running"
        assert engine.get_run(run_id)["execution_epoch"] == epoch
        assert engine.recovery.list()[0]["status"] == "proposed"
    finally:
        with engine.transaction() as conn:
            conn.execute("DROP TRIGGER fail_recovery_decision ON app.recovery_requests")
            conn.execute("DROP FUNCTION app.fail_recovery_decision()")


def test_unavailable_engine_is_not_reported_as_saved(tmp_path):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    config = tmp_path / "unavailable.json"
    config.write_text(
        json.dumps(
            {"api_url": f"http://127.0.0.1:{port}", "token": "private-unavailable-observer-token"}
        )
    )
    config.chmod(0o600)
    reply = subprocess.run(
        [sys.executable, "-m", "mgar_v3.recovery.watchdog", "--config", str(config), "--once"],
        env={"PATH": os.environ["PATH"]},
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert reply.returncode == 1
    assert json.loads(reply.stdout) == {"status": "unavailable", "saved": False}


def test_lost_request_response_is_unknown_then_dedupes_without_controlling_run(
    engine, trigger, tmp_path, monkeypatch, capsys
):
    run_id, epoch = stalled(engine, trigger)
    config = tmp_path / "observer.json"
    config.write_text(
        json.dumps(
            {"api_url": "http://localhost:18080", "token": "response-loss-private-test-token"}
        )
    )
    config.chmod(0o600)
    calls = []

    def handle(request):
        if request.method == "GET":
            return httpx.Response(200, json=engine.recovery.snapshot().model_dump(mode="json"))
        result = engine.recovery.submit(RecoveryInput.model_validate_json(request.content))
        calls.append(result)
        if len(calls) == 1:
            raise httpx.ReadTimeout("accepted; response lost", request=request)
        return httpx.Response(200, json={"created": result["created"]})

    real_client = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handle), **kwargs),
    )
    monkeypatch.setattr(sys, "argv", ["watchdog", "--config", str(config), "--once"])
    assert watchdog_main() == 1
    lost = json.loads(capsys.readouterr().out)
    assert lost["saved"] is None and lost["submission_outcome"] == "unknown"
    assert watchdog_main() == 0
    assert json.loads(capsys.readouterr().out)["created"] == 0
    assert len(engine.recovery.list()) == 1
    assert engine.get_run(run_id)["status"] == "running"
    assert engine.get_run(run_id)["execution_epoch"] == epoch
