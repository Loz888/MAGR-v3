"""Single-page entry and the live API -> background agent -> report path.

PostgreSQL, checkpoints and the model client are real. Only the remote GitHub
and DeepSeek responses are controlled; no paid calls are made by this suite.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from mgar_v3.api import create_app
from mgar_v3.repository_analysis.domain import AnalysisAuthorization
from mgar_v3.repository_analysis.model import DeepSeekAnalysisModel
from mgar_v3.repository_analysis.tools import RepositoryAnalysisTools

REPOSITORY = "octo/real-path"
COMMIT = "c" * 40
SOURCE = b"def start():\n    return 'ready'\n"


def configuration(settings, tmp_path, *, expired=False, model_key=True):
    path = tmp_path / "analysis-authorization.json"
    authorization = AnalysisAuthorization(
        approved_by=settings.local_actor,
        expires_at=datetime.now(UTC) + timedelta(days=-1 if expired else 1),
        allow_paid=True,
        allowed_repositories=[REPOSITORY],
    )
    path.write_text(authorization.model_dump_json())
    return settings.model_copy(update={
        "allow_paid": True,
        "repo_analysis_authorization_path": path,
        "deepseek_api_key": SecretStr("controlled-model-key") if model_key else None,
        "github_read_token": SecretStr("controlled-github-key"),
    })


def client_for(settings, engine, *, background=False):
    return TestClient(
        create_app(settings, engine, run_background=background),
        base_url="http://127.0.0.1:18080",
        headers={"X-MGAR-Token": settings.local_token.get_secret_value()},
    )


def test_all_entry_urls_serve_one_page_without_credentials(settings, engine):
    with client_for(settings, engine) as client:
        pages = [client.get(path) for path in ("/", "/repo", "/workbench")]
        assert all(page.status_code == 200 for page in pages)
        assert len({page.text for page in pages}) == 1
        assert 'id="repository-url"' in pages[0].text
        assert 'id="question"' in pages[0].text
        assert settings.local_token.get_secret_value() not in pages[0].text
        assert "script-src 'self'" in pages[0].headers["content-security-policy"]
        assert client.get("/legacy").status_code == 200
        assert "finding-source" in client.get("/legacy/workbench").text
        assert client.get("/api/v3/repository-analyses/profile",
                          headers={"X-MGAR-Token": "wrong"}).status_code == 401


def test_preflight_lists_missing_prerequisites_without_leaking_values(settings, engine):
    config = settings.model_copy(update={
        "allow_paid": False, "deepseek_api_key": None,
        "github_read_token": None, "github_mcp_binary": None,
        "repo_analysis_authorization_path": None,
    })
    with client_for(config, engine) as client:
        profile = client.get("/api/v3/repository-analyses/profile").json()
    assert profile["ready"] is False
    assert profile["available"] is False
    assert all(value is False for value in profile["checks"].values())
    assert {issue["code"] for issue in profile["issues"]} == {
        "PAID_DISABLED", "MODEL_CREDENTIAL_REQUIRED",
        "GITHUB_READ_CREDENTIAL_REQUIRED", "REPO_ANALYSIS_PROFILE_REQUIRED",
    }


def test_expired_authorization_is_not_presented_as_ready(settings, engine, tmp_path):
    config = configuration(settings, tmp_path, expired=True)
    with client_for(config, engine) as client:
        profile = client.get("/api/v3/repository-analyses/profile").json()
    assert profile["ready"] is False
    assert profile["checks"]["authorization_valid"] is False
    assert profile["issues"][0]["code"] == "REPO_ANALYSIS_AUTHORIZATION_EXPIRED"
    assert "controlled-model-key" not in json.dumps(profile)
    assert "controlled-github-key" not in json.dumps(profile)


@pytest.mark.parametrize("empty_key", [False, True])
def test_missing_model_key_fails_before_github_or_queued_work(
    settings, engine, tmp_path, monkeypatch, empty_key,
):
    config = configuration(settings, tmp_path, model_key=False)
    if empty_key:
        config = config.model_copy(update={"deepseek_api_key": SecretStr("")})

    async def unexpected_network(*args):
        raise AssertionError("Missing credentials must fail before accessing GitHub")

    monkeypatch.setattr(RepositoryAnalysisTools, "resolve_commit", unexpected_network)
    with client_for(config, engine) as client:
        assert client.get("/api/v3/repository-analyses/profile").json()["ready"] is False
        response = client.post("/api/v3/repository-analyses", json={
            "repository_url": "https://github.com/" + REPOSITORY,
            "task": "Trace startup", "execution_mode": "live", "client_request_id": "missing-key",
        })
        assert response.status_code == 403
        assert response.json()["code"] == "MODEL_CREDENTIAL_REQUIRED"
        assert client.get("/api/v3/repository-analyses").json() == []


def test_live_submission_background_loop_citations_and_followup(
    settings, engine, tmp_path, monkeypatch,
):
    config = configuration(settings, tmp_path)
    requests = []
    github_calls = []

    async def github(self, name, arguments):
        github_calls.append((name, arguments))
        if name == "search_repositories":
            payload = {"items": [{
                "full_name": REPOSITORY, "default_branch": "main", "private": False,
            }]}
        elif name == "list_branches":
            payload = [{"name": "main", "commit": {"sha": COMMIT}}]
        elif name == "get_file_contents":
            assert arguments["sha"] == COMMIT
            assert arguments["path"] == "src/bootstrap.py"
            payload = {
                "encoding": "base64", "content": base64.b64encode(SOURCE).decode(),
                "sha": hashlib.sha1(
                    b"blob " + str(len(SOURCE)).encode() + b"\0" + SOURCE
                ).hexdigest(),
            }
        else:
            raise AssertionError(name)
        return [], payload, []

    def completion(request):
        body = json.loads(request.content)
        payload = json.loads(body["messages"][1]["content"])
        requests.append(payload)
        if payload["round"] == 1:
            decision = {
                "action": "tool", "thought": "Read the startup implementation.",
                "tool_calls": [{"tool": "read_file", "path": "src/bootstrap.py"}],
                "report": None, "insufficient_reason": None,
            }
        else:
            evidence = next(item for item in payload["history"] if item.get("evidence_id"))
            decision = {
                "action": "final", "thought": "The source establishes the startup result.",
                "tool_calls": [], "insufficient_reason": None,
                "report": {
                    "conclusion": "启动函数返回 ready。",
                    "structure": "入口位于 src/bootstrap.py。",
                    "key_files": [{
                        "statement": "start 返回 ready", "evidence_id": evidence["evidence_id"],
                        "quote": "    return 'ready'",
                    }],
                    "call_chains": [],
                    "findings": [{
                        "statement": "返回值是常量", "evidence_id": evidence["evidence_id"],
                        "quote": "    return 'ready'",
                    }],
                    "uncertainties": [], "next_steps": [],
                },
            }
        return httpx.Response(200, json={
            "model": body["model"], "usage": {"prompt_tokens": 300, "completion_tokens": 100},
            "choices": [{"finish_reason": "stop",
                         "message": {"content": json.dumps(decision, ensure_ascii=False)}}],
        })

    transport = httpx.MockTransport(completion)
    monkeypatch.setattr(RepositoryAnalysisTools, "_call", github)
    with httpx.Client(transport=transport) as remote:
        monkeypatch.setattr(
            "mgar_v3.repository_analysis.service.DeepSeekAnalysisModel",
            lambda settings, model: DeepSeekAnalysisModel(settings, model, client=remote),
        )
        with client_for(config, engine, background=True) as client:
            profile = client.get("/api/v3/repository-analyses/profile").json()
            assert profile["ready"] is True
            body = {
                "repository_url": "https://github.com/" + REPOSITORY,
                "task": "说明项目如何启动", "execution_mode": "live",
                "limits": profile["limits"], "client_request_id": "live-question",
            }
            response = client.post("/api/v3/repository-analyses", json=body)
            assert response.status_code == 202
            first = response.json()
            assert client.post("/api/v3/repository-analyses", json=body).json()["run_id"] == (
                first["run_id"]
            )

            def completed(run_id):
                deadline = time.monotonic() + 20
                while True:
                    detail = client.get("/api/v3/repository-analyses/" + run_id).json()
                    if detail["run"]["run_status"] not in {"queued", "running"}:
                        assert detail["run"]["run_status"] == "succeeded", json.dumps(detail)
                        return detail
                    assert time.monotonic() < deadline, detail
                    time.sleep(0.05)

            detail = completed(first["run_id"])
            assert detail["run"]["execution_mode"] == "live"
            assert detail["run"]["commit"] == COMMIT
            assert detail["run"]["model_calls_used"] == 2
            assert detail["tool_calls"][0]["tool"] == "read_file"
            assert detail["claims"][0]["quote"] == "    return 'ready'"
            assert "src/bootstrap.py#L2-L2" in detail["report"]
            followup = client.post(
                "/api/v3/repository-analyses/" + first["run_id"] + "/messages",
                json={"body": "进一步解释返回值", "client_request_id": "live-followup"},
            )
            assert followup.status_code == 202
            second = followup.json()
            assert second["thread_id"] == first["thread_id"]
            assert second["parent_run_id"] == first["run_id"]
            assert completed(second["run_id"])["run"]["model_calls_used"] == 2
    assert len(requests) == 4
    assert requests[-1]["prior_report"]
    assert len([name for name, _ in github_calls if name == "get_file_contents"]) == 2
