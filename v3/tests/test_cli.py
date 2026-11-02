import json
import os
import socket
import subprocess
import sys
import time

import httpx


def test_real_cli_and_api_process_share_persistent_run_and_approval(settings, tmp_path):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    environment = {
        **os.environ,
        "MGAR_V3_DATABASE_URL": settings.database_url.get_secret_value(),
        "MGAR_V3_ARTIFACT_ROOT": str(settings.artifact_root),
        "MGAR_V3_REPOSITORY": settings.repository,
        "MGAR_V3_LOCAL_ACTOR": settings.local_actor,
        "MGAR_V3_ALLOWED_ACTORS": json.dumps(settings.allowed_actors),
        "MGAR_V3_GRAPH_VERSION": "r2-mailbox-v1",
        "MGAR_V3_ROUTER_MODE": "fixture",
    }
    with (tmp_path / "api.log").open("wb") as log:
        api = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "mgar_v3.api:create_app",
                "--factory",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            env=environment,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            with httpx.Client(
                base_url=base,
                headers={"X-MGAR-Token": settings.local_token.get_secret_value()},
                timeout=2,
            ) as client:
                deadline = time.monotonic() + 15
                while True:
                    try:
                        if client.get("/api/v3/healthz").is_success:
                            break
                    except httpx.HTTPError:
                        pass
                    assert time.monotonic() < deadline, "API did not start"
                    time.sleep(0.05)
                command = [sys.executable, "-m", "mgar_v3.cli", "--api", base]
                submitted = subprocess.run(
                    [
                        *command,
                        "run",
                        "--question",
                        "Documented symptoms",
                        "--issue",
                        "1",
                        "--client-request-id",
                        "process-cli",
                    ],
                    env=environment,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                assert submitted.returncode == 0, submitted.stderr
                run_id = json.loads(submitted.stdout)["run_id"]
                deadline = time.monotonic() + 10
                while client.get(f"/api/v3/runs/{run_id}").json()["status"] != "waiting_approval":
                    assert time.monotonic() < deadline
                    time.sleep(0.05)
                report = subprocess.run(
                    [*command, "report", run_id],
                    env=environment,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                assert report.returncode == 0 and "Execution mode: fixture" in report.stdout
                action = client.get(f"/api/v3/runs/{run_id}/actions").json()[0]
                approved = subprocess.run(
                    [
                        *command,
                        "approve",
                        run_id,
                        "--action",
                        action["action_id"],
                        "--payload-hash",
                        action["payload_hash"],
                    ],
                    env=environment,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                assert approved.returncode == 0
                assert json.loads(approved.stdout)["status"] == "ready"

                def cli(*arguments):
                    result = subprocess.run(
                        [*command, *arguments],
                        env=environment,
                        capture_output=True,
                        text=True,
                        timeout=10,
                    )
                    assert result.returncode == 0, result.stderr + result.stdout
                    return result.stdout

                assert json.loads(cli("pause", run_id))["status"] == "paused"
                message = (
                    "message",
                    run_id,
                    "--body",
                    "Include the limitations",
                    "--client-request-id",
                    "process-followup",
                )
                assert not json.loads(cli(*message))["duplicate"]
                assert json.loads(cli(*message))["duplicate"]
                event = json.loads(
                    cli(
                        "event",
                        "--issue",
                        "2",
                        "--body",
                        "@mgar Investigate documented symptoms",
                        "--client-request-id",
                        "cli-router-cold",
                    )
                )
                assert event["route"] == "unrouted"
                deadline = time.monotonic() + 10
                while True:
                    tickets = json.loads(cli("tickets"))
                    routed_ticket = next(
                        (t for t in tickets if t["external_key"].endswith(":2")), None
                    )
                    if routed_ticket and routed_ticket["stage"] == "waiting_approval":
                        break
                    assert time.monotonic() < deadline
                    time.sleep(0.05)
                detail = json.loads(cli("ticket", routed_ticket["ticket_id"]))
                assert detail["channels"] == ["cli"]
                assert json.loads(cli("routing"))["physical_router_calls"] == 0
                assert json.loads(cli("resume", run_id))["run_id"] == run_id
                deadline = time.monotonic() + 10
                while client.get(f"/api/v3/runs/{run_id}").json()["status"] != "waiting_approval":
                    assert time.monotonic() < deadline
                    time.sleep(0.05)
                actions = client.get(f"/api/v3/runs/{run_id}/actions").json()
                assert [a["status"] for a in actions] == ["expired", "proposed"]
                assert "input_consumed" in cli("watch", run_id)
                assert json.loads(cli("cancel", run_id))["status"] == "cancelled"
                assert json.loads(cli(*message))["duplicate"]
        finally:
            api.terminate()
            api.wait(timeout=10)
