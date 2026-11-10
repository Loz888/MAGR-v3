"""Real PostgreSQL account/session/configuration tests, with no external model calls."""

from __future__ import annotations

import json
import stat
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from mgar_v3.api import create_app
from mgar_v3.repository_analysis.tools import RepositoryAnalysisTools
from mgar_v3.web_access import SESSION_COOKIE, WebAnalysisSettings

ORIGIN = "http://127.0.0.1:18080"
PASSWORD = "testing-password-123"


def browser(settings, engine, *, host="127.0.0.1"):
    return TestClient(
        create_app(settings, engine, run_background=False),
        base_url=ORIGIN, headers={"Origin": ORIGIN}, client=(host, 54321),
    )


def signup(client):
    response = client.post("/api/v3/auth/setup", json={
        "username": "test-owner", "password": PASSWORD,
    })
    assert response.status_code == 201, response.text
    client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
    return response


def test_first_owner_cookie_login_logout_and_persistent_account(settings, engine):
    with browser(settings, engine) as client:
        assert client.get("/api/v3/auth/session").json()["setup_required"] is True
        response = signup(client)
        cookie_header = response.headers["set-cookie"].lower()
        assert "httponly" in cookie_header and "samesite=strict" in cookie_header
        assert SESSION_COOKIE not in response.json()
        assert client.get("/api/v3/repository-analyses").status_code == 200
        assert client.post("/api/v3/auth/logout").status_code == 204
        assert client.get("/api/v3/repository-analyses").status_code == 401
    with browser(settings, engine) as restarted:
        assert restarted.get("/api/v3/auth/session").json()["setup_required"] is False
        assert restarted.post("/api/v3/auth/login", json={
            "username": "test-owner", "password": PASSWORD,
        }).status_code == 200
        assert restarted.get("/api/v3/repository-analyses").status_code == 200
    with engine.transaction() as conn:
        account = conn.execute("SELECT password_hash FROM app.web_accounts").fetchone()
        assert account["password_hash"].startswith("scrypt$")
        assert PASSWORD not in account["password_hash"]


def test_setup_is_local_once_and_race_has_single_winner(settings, engine):
    with browser(settings, engine, host="203.0.113.1") as remote:
        assert remote.post("/api/v3/auth/setup", json={
            "username": "outsider", "password": PASSWORD,
        }).status_code == 403
    with browser(settings, engine) as client:
        def create(_):
            return client.post("/api/v3/auth/setup", json={
                "username": "test-owner", "password": PASSWORD,
            }).status_code
        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses = list(pool.map(create, range(2)))
        assert sorted(statuses) == [201, 409]
    with engine.transaction() as conn:
        assert conn.execute("SELECT count(*) AS n FROM app.web_accounts").fetchone()["n"] == 1


def test_password_validation_and_login_throttling(settings, engine):
    with browser(settings, engine) as client:
        assert client.post("/api/v3/auth/setup", json={
            "username": "test-owner", "password": "short",
        }).status_code == 400
        signup(client)
        client.post("/api/v3/auth/logout")
        for index in range(11):
            response = client.post("/api/v3/auth/login", json={
                "username": "test-owner" if index % 2 else "unknown-user",
                "password": "incorrect",
            })
            assert response.status_code == (401 if index < 10 else 429)


def test_cookie_writes_require_origin_and_csrf_and_expired_sessions_fail(settings, engine):
    with browser(settings, engine) as client:
        signup(client)
        client.headers.pop("X-CSRF-Token")
        assert client.put("/api/v3/settings/analysis", json={}).status_code == 403
        session = client.get("/api/v3/auth/session").json()
        client.headers["X-CSRF-Token"] = session["csrf_token"]
        assert client.put("/api/v3/settings/analysis", json={},
                          headers={"Origin": "https://evil.example"}).status_code == 403
        client.headers.pop("Origin")
        assert client.put("/api/v3/settings/analysis", json={}).status_code == 403
        client.headers["Origin"] = ORIGIN
        with engine.transaction() as conn:
            conn.execute("UPDATE app.web_sessions SET expires_at=now()-interval '1 second'")
        assert client.get("/api/v3/repository-analyses").status_code == 401
        assert client.get("/api/v3/auth/session").json()["authenticated"] is False


def test_settings_are_encrypted_preserved_and_effective_without_restart(settings, engine):
    with browser(settings, engine) as client:
        signup(client)
        response = client.put("/api/v3/settings/analysis", json={
            "deepseek_api_key": "private-model-credential",
            "github_read_token": "private-github-credential",
            "allow_paid": True, "allowed_repositories": ["octo/example"],
        })
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["profile"]["ready"] is True
        assert data["profile"]["allowed_repositories"] == ["octo/example"]
        assert "private-model-credential" not in response.text
        assert "private-github-credential" not in response.text
        assert client.put("/api/v3/settings/analysis", json={
            "allow_paid": True, "expected_revision": data["revision"],
        }).status_code == 200
        refreshed = client.get("/api/v3/settings/analysis").json()
        assert refreshed["profile"]["ready"] is True
        assert client.put("/api/v3/settings/analysis", json={
            "expected_revision": 0,
        }).status_code == 409
    # New service instances read the same persisted credentials and permission.
    store = WebAnalysisSettings(engine, settings)
    _, payload = store.snapshot()
    effective = store.effective(payload)
    assert effective.allow_paid is True
    assert effective.deepseek_api_key.get_secret_value() == "private-model-credential"
    assert effective.github_read_token.get_secret_value() == "private-github-credential"
    assert stat.S_IMODE(store.key_path.stat().st_mode) == 0o600
    with engine.transaction() as conn:
        rows = conn.execute("SELECT encrypted_payload FROM app.web_analysis_settings").fetchall()
        assert all("private-model-credential" not in row["encrypted_payload"] for row in rows)
        assert all("private-github-credential" not in row["encrypted_payload"] for row in rows)
    with browser(settings, engine) as restarted:
        restarted.post("/api/v3/auth/login", json={"username": "test-owner", "password": PASSWORD})
        assert restarted.get("/api/v3/settings/analysis").json()["profile"]["ready"] is True


def test_permission_is_explicit_readonly_and_bounded(settings, engine):
    with browser(settings, engine) as client:
        signup(client)
        response = client.put("/api/v3/settings/analysis", json={
            "deepseek_api_key": "private-key", "github_read_token": "read-key",
        })
        assert response.status_code == 200
        assert response.json()["profile"]["ready"] is False
        for invalid in (
            {"authorization_days": 0}, {"allowed_repositories": ["../../elsewhere"]},
            {"allow_paid": True, "limits": {"max_cost_micros": 10000001}},
            {"allow_paid": True, "allow_external_write": True},
        ):
            assert client.put("/api/v3/settings/analysis", json=invalid).status_code == 422
    store = WebAnalysisSettings(engine, settings)
    _, payload = store.snapshot()
    assert store.effective(payload).allow_external_write == settings.allow_external_write
    assert json.loads(json.dumps(payload))["allow_paid"] is False


@pytest.mark.parametrize("narrow_scope", [False, True])
def test_configuration_update_fences_existing_live_runs(
    settings, engine, monkeypatch, narrow_scope,
):
    async def commit(*args):
        return "c" * 40

    monkeypatch.setattr(RepositoryAnalysisTools, "resolve_commit", commit)
    with browser(settings, engine) as client:
        signup(client)
        data = client.put("/api/v3/settings/analysis", json={
            "deepseek_api_key": "private-key", "github_read_token": "read-key",
            "allow_paid": True,
        }).json()
        created = client.post("/api/v3/repository-analyses", json={
            "repository_url": "https://github.com/octo/example", "task": "Trace startup",
            "execution_mode": "live", "client_request_id": "before-config-update",
        })
        assert created.status_code == 202
        run_id = created.json()["run_id"]
        update = {
            "expected_revision": data["revision"], "allow_paid": True,
            "allowed_repositories": ["other/repo"] if narrow_scope else ["*"],
        }
        blocked = client.put("/api/v3/settings/analysis", json=update)
        assert blocked.status_code == 409
        assert blocked.json()["code"] == "ANALYSIS_BUSY"
        assert client.post(f"/api/v3/repository-analyses/{run_id}/pause").status_code == 200
        assert client.put("/api/v3/settings/analysis", json=update).status_code == 200
        for suffix, body in (
            ("/resume", None),
            ("/messages", {"body": "Continue", "client_request_id": "after-update"}),
        ):
            denied = client.post(f"/api/v3/repository-analyses/{run_id}{suffix}", json=body)
            assert denied.status_code == (403 if narrow_scope else 409)
        detail = client.get(f"/api/v3/repository-analyses/{run_id}").json()
        assert detail["run"]["model_calls_used"] == 0
        assert len(client.get("/api/v3/repository-analyses").json()) == 1
