import base64
from uuid import UUID, uuid4

from test_api import client_for


def test_knowledge_api_auth_job_read_and_fixed_snapshot_duplicate(settings, engine):
    settings.graph_version = "r4-experts-v1"
    with client_for(settings, engine) as client:
        body = {
            "title": "知识",
            "source_uri": "kb://api/orders",
            "source_version": "v1",
            "media_type": "text/plain",
            "content_base64": base64.b64encode("业务订单统计需要按订单号去重。".encode()).decode(),
        }
        assert (
            client.get("/api/v3/knowledge/documents", headers={"X-MGAR-Token": "wrong"}).status_code
            == 401
        )
        response = client.post("/api/v3/knowledge/documents", json=body)
        assert response.status_code == 202
        job = response.json()
        snapshot = engine.knowledge.index(UUID(job["job_id"]))
        assert client.get("/api/v3/knowledge/jobs").json()[0]["status"] == "succeeded"
        search = client.post("/api/v3/knowledge/search", json={"query": "业务订单统计"}).json()
        hit = search["hits"][0]
        read = client.get(
            "/api/v3/knowledge/chunks/" + hit["chunk_id"], params={"snapshot_id": str(snapshot)}
        )
        assert read.status_code == 200 and "去重" in read.json()["text"]
        run = {
            "project_id": "orders-demo",
            "question": "检查订单口径",
            "issue_number": 1,
            "client_request_id": "snapshot-case",
            "knowledge_snapshot_ids": [str(snapshot)],
        }
        first = client.post("/api/v3/runs", json=run)
        assert first.status_code == 202
        assert client.post("/api/v3/runs", json=run).json()["duplicate"]
        assert (
            client.post("/api/v3/runs", json=run | {"knowledge_snapshot_ids": []}).status_code
            == 409
        )
        before = len(engine.list_runs())
        assert (
            client.post(
                "/api/v3/runs",
                json=run
                | {
                    "client_request_id": "denied",
                    "issue_number": 2,
                    "knowledge_snapshot_ids": [str(uuid4())],
                },
            ).status_code
            == 404
        )
        assert len(engine.list_runs()) == before
        assert (
            client.patch(
                "/api/v3/knowledge/documents/" + hit["document_id"] + "/acl", json={"revoked": True}
            ).status_code
            == 200
        )
        assert (
            client.get(
                "/api/v3/knowledge/chunks/" + hit["chunk_id"], params={"snapshot_id": str(snapshot)}
            ).status_code
            == 404
        )


def test_r4_web_exposes_sources_resolution_and_explicit_memory_confirmation(settings, engine):
    with client_for(settings, engine) as client:
        html = client.get("/").text
        assert "knowledge-form" in html and "memory-controls" in html and "search-form" in html
        js = client.get("/static/app.js").text
        assert "textContent" in js and "innerHTML" not in js
        assert "summary_hash:preview.summary_hash" in js
