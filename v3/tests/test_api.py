import hashlib
import hmac
import json

from fastapi.testclient import TestClient

from mgar_v3.api import create_app
from mgar_v3.graph.runner import Runner


def client_for(settings, engine):
    return TestClient(
        create_app(settings, engine, run_background=False),
        base_url="http://127.0.0.1",
        headers={"X-MGAR-Token": settings.local_token.get_secret_value()},
    )


def test_auth_host_origin_and_actual_body_limit(settings, engine):
    with client_for(settings, engine) as client:
        assert client.get("/api/v3/runs", headers={"X-MGAR-Token": "wrong"}).status_code == 401
        assert client.get("/api/v3/runs", headers={"Host": "evil.example"}).status_code == 400
        assert (
            client.post(
                "/api/v3/runs", json={}, headers={"Origin": "https://evil.example"}
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/api/v3/runs", content=b"x" * 70000, headers={"Content-Length": "1"}
            ).status_code
            == 413
        )


def test_api_create_report_approve_and_sse_resume_share_authority(settings, engine):
    body = {
        "project_id": "orders-demo",
        "question": "Summarize documented symptoms",
        "issue_number": 1,
        "client_request_id": "cli-1",
        "execution_mode": "fixture",
    }
    with client_for(settings, engine) as client:
        response = client.post("/api/v3/runs", json=body)
        assert response.status_code == 202
        run_id = response.json()["run_id"]
        assert client.post("/api/v3/runs", json=body).json()["duplicate"]
        from uuid import UUID

        Runner(engine).execute(UUID(run_id))
        assert client.get(f"/api/v3/runs/{run_id}/report").status_code == 200
        action = client.get(f"/api/v3/runs/{run_id}/actions").json()[0]
        approval = client.post(
            f"/api/v3/runs/{run_id}/approvals",
            json={
                "action_id": action["action_id"],
                "payload_hash": action["payload_hash"],
            },
        )
        assert approval.status_code == 200 and approval.json()["status"] == "ready"
        events = client.get(f"/api/v3/runs/{run_id}/events").text
        ids = [int(line[4:]) for line in events.splitlines() if line.startswith("id: ")]
        assert ids == sorted(set(ids))
        rest = client.get(
            f"/api/v3/runs/{run_id}/events", headers={"Last-Event-ID": str(ids[-2])}
        ).text
        assert f"id: {ids[-1]}\n" in rest and f"id: {ids[-2]}\n" not in rest
        assert (
            client.get(f"/api/v3/runs/{run_id}/events", headers={"Last-Event-ID": "-1"}).status_code
            == 400
        )
        live = client.post("/api/v3/runs", json={**body, "execution_mode": "live"})
        assert live.status_code == 403


def test_signed_github_webhook_and_deduplication(settings, engine):
    from pydantic import SecretStr

    configuration = settings.model_copy(
        update={"github_webhook_secret": SecretStr("fixture-secret")}
    )
    payload = {
        "action": "created",
        "repository": {"full_name": "example/mgar-test"},
        "issue": {"number": 1},
        "sender": {"login": "owner"},
        "comment": {"body": "/mgar investigate documented symptoms"},
    }
    raw = json.dumps(payload).encode()
    headers = {
        "X-GitHub-Delivery": "signed-1",
        "X-GitHub-Event": "issue_comment",
        "X-Hub-Signature-256": "sha256="
        + hmac.new(b"fixture-secret", raw, hashlib.sha256).hexdigest(),
    }
    with client_for(configuration, engine) as client:
        assert client.post("/api/v3/webhooks/github", content=raw).status_code == 401
        first = client.post("/api/v3/webhooks/github", content=raw, headers=headers)
        assert first.status_code == 202
        second = client.post("/api/v3/webhooks/github", content=raw, headers=headers)
        assert second.json()["duplicate"]
        assert len(engine.list_runs()) == 1


def test_pg_failure_never_acknowledges_new_work(settings, engine):
    with client_for(settings, engine) as client:
        engine.close()
        response = client.post(
            "/api/v3/runs",
            json={
                "project_id": "orders-demo",
                "question": "Investigate",
                "issue_number": 1,
                "client_request_id": "not-saved",
            },
        )
        assert response.status_code == 503


def test_evidence_download_requires_project_reference(settings, engine, trigger):
    receipt = engine.ingest(trigger)
    Runner(engine).execute(receipt.run_id)
    with engine.transaction() as conn:
        evidence = conn.execute("SELECT * FROM app.evidence").fetchone()
    unreferenced = engine.artifacts.put(b"not authorized by a project reference")
    with client_for(settings, engine) as client:
        assert client.get(f"/api/v3/evidence/{evidence['evidence_id']}").status_code == 200
        assert client.get(f"/api/v3/artifacts/{evidence['content_hash']}").status_code == 200
        assert client.get(f"/api/v3/artifacts/{unreferenced}").status_code == 404
