import hashlib
import hmac
import json
import time

from fastapi.testclient import TestClient
from pydantic import SecretStr
from test_routing import routed as routed

from mgar_v3.api import create_app
from mgar_v3.routing.worker import FixtureRouter


def wait_for(client, path, predicate):
    deadline = time.monotonic() + 10
    while True:
        response = client.get(path)
        assert response.is_success, response.text
        value = response.json()
        if predicate(value):
            return value
        assert time.monotonic() < deadline, "Background routing did not reach expected state"
        time.sleep(0.025)


def test_web_cli_github_converge_on_tickets_and_continue_hot_conversation(routed):
    settings = routed.settings.model_copy(
        update={"github_webhook_secret": SecretStr("fixture-secret")}
    )
    with TestClient(
        create_app(settings, routed),
        base_url="http://127.0.0.1",
        headers={"X-MGAR-Token": settings.local_token.get_secret_value()},
    ) as client:
        assert client.get("/").status_code == 200
        page = client.get("/")
        assert "script-src 'self'" in page.headers["content-security-policy"]
        assert settings.local_token.get_secret_value() not in page.text
        assert client.get("/static/app.js").status_code == 200
        for source, issue in [("web", 1), ("cli", 2)]:
            response = client.post(
                "/api/v3/events",
                json={
                    "source": source,
                    "intent": "investigate",
                    "issue_number": issue,
                    "body": "Investigate documented duplicate symptoms",
                    "client_request_id": f"{source}-cold",
                },
            )
            assert response.status_code == 202 and response.json()["route"] == "unrouted"
        payload = {
            "action": "created",
            "repository": {"full_name": settings.repository},
            "issue": {"number": 3},
            "sender": {"login": "owner"},
            "comment": {"body": "@mgar Investigate documented symptoms"},
        }
        raw = json.dumps(payload).encode()
        response = client.post(
            "/api/v3/webhooks/github",
            content=raw,
            headers={
                "X-GitHub-Delivery": "r3-signed",
                "X-GitHub-Event": "issue_comment",
                "X-Hub-Signature-256": "sha256="
                + hmac.new(b"fixture-secret", raw, hashlib.sha256).hexdigest(),
            },
        )
        assert response.status_code == 202
        tickets = wait_for(
            client,
            "/api/v3/tickets",
            lambda rows: len(rows) == 3 and all(r["stage"] == "waiting_approval" for r in rows),
        )
        channels = set()
        for ticket in tickets:
            detail = client.get("/api/v3/tickets/" + ticket["ticket_id"]).json()
            channels.update(detail["channels"])
        assert channels == {"web", "cli", "github"}
        selected = next(t for t in tickets if t["external_key"].endswith(":1"))
        response = client.post(
            "/api/v3/events",
            json={
                "source": "web",
                "issue_number": 1,
                "body": "Include the limitations",
                "client_request_id": "web-hot",
            },
        )
        assert response.status_code == 202 and response.json()["route"] == "direct"
        assert response.json()["ticket_id"] == selected["ticket_id"]
        detail = wait_for(
            client,
            "/api/v3/tickets/" + selected["ticket_id"],
            lambda t: t["stage"] == "waiting_approval" and t["revision"] == 2,
        )
        assert len(detail["runs"]) == 1
        assert (
            client.post("/api/v3/tickets/" + selected["ticket_id"] + "/pause").json()["status"]
            == "paused"
        )
        assert (
            client.post("/api/v3/tickets/" + selected["ticket_id"] + "/resume").status_code == 200
        )
        wait_for(
            client,
            "/api/v3/tickets/" + selected["ticket_id"],
            lambda t: t["stage"] == "waiting_approval",
        )
        assert client.get("/api/v3/router/status").json()["physical_router_calls"] == 0
        assert client.get("/api/v3/readyz").status_code == 200
        assert (
            client.post("/api/v3/tickets/" + selected["ticket_id"] + "/cancel").json()["status"]
            == "cancelled"
        )


def test_router_exception_isolated_from_workers_and_control_plane(routed, monkeypatch):
    def fail(self, batch):
        raise RuntimeError("controlled router outage")

    monkeypatch.setattr(FixtureRouter, "propose", fail)
    settings = routed.settings
    with TestClient(
        create_app(settings, routed),
        base_url="http://127.0.0.1",
        headers={"X-MGAR-Token": settings.local_token.get_secret_value()},
    ) as client:
        cold = client.post(
            "/api/v3/events",
            json={
                "issue_number": 1,
                "body": "@mgar Investigate symptoms",
                "client_request_id": "failed-router",
            },
        )
        assert cold.status_code == 202
        direct = client.post(
            "/api/v3/runs",
            json={
                "project_id": settings.project_id,
                "issue_number": 2,
                "question": "Investigate documented symptoms",
                "client_request_id": "fast-with-router-down",
            },
        )
        assert direct.status_code == 202 and direct.json()["route"] == "created"
        run_id = direct.json()["run_id"]
        wait_for(client, "/api/v3/runs/" + run_id, lambda r: r["status"] == "waiting_approval")
        wait_for(client, "/api/v3/router/status", lambda s: s["batches"].get("failed", 0) > 0)
        assert client.get("/api/v3/readyz").status_code == 200
        message = client.post(
            "/api/v3/runs/" + run_id + "/messages",
            json={"body": "Additional facts", "client_request_id": "hot-outage"},
        )
        assert message.status_code == 202 and message.json()["route"] == "direct"
        wait_for(
            client,
            "/api/v3/runs/" + run_id,
            lambda r: r["status"] == "waiting_approval" and r["input_revision"] == 2,
        )


def test_ticket_and_event_access_enforce_local_identity(routed, trigger):
    settings = routed.settings
    receipt = routed.ingest(trigger)
    with TestClient(
        create_app(settings, routed, run_background=False), base_url="http://127.0.0.1"
    ) as client:
        assert client.get("/api/v3/tickets").status_code == 401
        assert client.post("/api/v3/events", json={}).status_code == 401
        headers = {"X-MGAR-Token": settings.local_token.get_secret_value()}
        assert client.get("/api/v3/tickets?limit=10000", headers=headers).status_code == 400
        forged = client.post(
            "/api/v3/events",
            headers=headers,
            json={
                "source": "github",
                "issue_number": 1,
                "body": "command",
                "client_request_id": "forged-source",
            },
        )
        assert forged.status_code == 422
        with routed.transaction() as conn:
            conn.execute(
                "UPDATE app.threads SET project_id=%s WHERE thread_id=%s",
                ("other-project", receipt.thread_id),
            )
        assert (
            client.get("/api/v3/tickets/" + str(receipt.ticket_id), headers=headers).status_code
            == 404
        )
