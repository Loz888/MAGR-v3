"""V3.5 R05 acceptance: one Web / CLI / API product contract.

The tests pin the user-visible contract of the unified workbench:

* every task exposes the fixed commit, scan scope, observed coverage
  (read/unread files), risk-ranked findings, evidence, receipts, usage/cost and
  the next action;
* the five stage labels never upgrade a suggestion to a fix;
* pause/resume/cancel/follow-up/history and failure reasons stay observable;
* the page reviews the full diff and approves the reviewed ``payload_hash``;
* missing dependencies surface as ``waiting_for_dependencies`` and missing
  GitHub write authorization as ``waiting_approval`` instead of "failed";
* negative paths are enforced by the API, not by the page.

No live model, GitHub write or agent gate is opened here; live dependencies are
simulated at the database boundary and stay NOT_RUN in the acceptance record.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from mgar_v3 import cli
from mgar_v3.coordination.engine import Engine
from mgar_v3.domain import DomainError
from mgar_v3.repair.pr_delivery import seal_bundle
from mgar_v3.settings import Settings
from mgar_v3.workbench.domain import STAGE_LABELS, CreateWorkbenchTask, WorkbenchFinding
from mgar_v3.workbench.service import WorkbenchService

REPO = "octo/example"
DISPATCHER_RULE = "WB-DISPATCH-SILENT-UNKNOWN"
WEB = Path(__file__).resolve().parents[1] / "web"


def _client(settings: Settings, engine: Engine) -> TestClient:
    from mgar_v3.api import create_app

    return TestClient(
        create_app(settings, engine, run_background=False),
        base_url="http://127.0.0.1",
        headers={"X-MGAR-Token": settings.local_token.get_secret_value()},
    )


def _create(
    client: TestClient,
    *,
    task: str = "检查未知请求处理路径",
    request_id: str | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "repository_url": f"https://github.com/{REPO}",
        "mode": "audit",
        "task": task,
        "execution_mode": "fixture",
        "auto_start": True,
    }
    if request_id is not None:
        body["client_request_id"] = request_id
    response = client.post("/api/v3/workbench/tasks", json=body)
    assert response.status_code == 202, response.text
    payload: dict[str, Any] = response.json()
    return payload


def _finding(payload: dict[str, Any], rule_id: str = DISPATCHER_RULE) -> dict[str, Any]:
    item: dict[str, Any] = next(
        finding for finding in payload["findings"] if finding["rule_id"] == rule_id
    )
    return item


def _waiting_approval(client: TestClient) -> tuple[UUID, str, str]:
    created = _create(client)
    task_id = UUID(created["task_id"])
    finding_id = str(_finding(created)["finding_id"])
    reproduced = client.post(
        f"/api/v3/workbench/tasks/{task_id}/findings/{finding_id}/reproduce"
    )
    assert reproduced.status_code == 200, reproduced.text
    repaired = client.post(f"/api/v3/workbench/tasks/{task_id}/findings/{finding_id}/repair")
    assert repaired.status_code == 200, repaired.text
    assert repaired.json()["stage"] == "waiting_approval"
    review = client.get(f"/api/v3/workbench/tasks/{task_id}/findings/{finding_id}/diff")
    assert review.status_code == 200, review.text
    return task_id, finding_id, str(review.json()["payload_hash"])


def _live_clone(engine: Engine, task_id: UUID, *, status: str = "waiting_approval") -> UUID:
    """Clone a fixture task into an equivalent live task row."""

    live_id = uuid4()
    phase = "waiting_approval" if status == "waiting_approval" else status
    stage = "waiting_approval" if status == "waiting_approval" else "suspected"
    with engine.transaction() as conn:
        conn.execute(
            "INSERT INTO app.workbench_tasks("
            "task_id,thread_id,request_key,request_digest,mode,repository_url,repository,"
            "resolved_commit,task,issue_number,execution_mode,coverage,observed_coverage,"
            "limits,status,stage,phase,resume_phase,revision) "
            "SELECT %s,thread_id,%s,request_digest,mode,repository_url,repository,"
            "resolved_commit,task,issue_number,'live',coverage,observed_coverage,"
            "limits,%s,%s,%s,%s,revision "
            "FROM app.workbench_tasks WHERE task_id=%s",
            (live_id, f"r05-live-{live_id.hex}", status, stage, phase, phase, task_id),
        )
        if status == "waiting_approval":
            conn.execute(
                "INSERT INTO app.workbench_findings(task_id,finding_id,stage,risk_score,payload) "
                "SELECT %s,finding_id,stage,risk_score,payload "
                "FROM app.workbench_findings WHERE task_id=%s",
                (live_id, task_id),
            )
    return live_id


# -- positive contract ---------------------------------------------------


def test_view_exposes_commit_scope_coverage_findings_usage_and_next_action(
    settings: Settings, engine: Engine
) -> None:
    with _client(settings, engine) as client:
        created = _create(client)
        task_id = created["task_id"]
        view = client.get(f"/api/v3/workbench/tasks/{task_id}").json()

    assert len(view["commit"]) == 40
    assert all(char in "0123456789abcdef" for char in view["commit"])
    assert view["coverage"]["request"]["scope"] == "repository"
    observed = view["coverage"]["observed"]
    assert observed["files_considered"] > 0
    assert observed["files_scanned"] == observed["files_considered"]
    assert observed["unscanned_files"] == []
    assert observed["complete"] is True

    ranks = [finding["risk_rank"] for finding in view["findings"]]
    assert ranks == list(range(1, len(ranks) + 1))
    for finding in view["findings"]:
        assert finding["label"] in STAGE_LABELS.values()
        assert finding["source"]
        assert finding["risk_score"] >= 0
        assert finding["stage"] in STAGE_LABELS

    assert view["usage"]["runs"]["total"] >= 1
    assert view["usage"]["runs"]["audit"] == 1
    assert view["usage"]["retries"]["total"] == 0
    assert view["usage"]["cost"]["recorded"] is True
    assert view["usage"]["cost"]["currency"] == "USD"
    assert view["usage"]["cost"]["amount"] == "0.000000"
    assert view["usage"]["cost"]["calls"] >= 1
    assert view["next_action"] == "reproduce"
    assert view["pending"] == {"state": None, "reason_code": "", "reason": ""}
    assert view["failure"] is None
    assert view["labels"] == dict(STAGE_LABELS)


def test_suggested_patch_is_never_presented_as_fixed(settings: Settings, engine: Engine) -> None:
    with _client(settings, engine) as client:
        created = _create(client)
        view = client.get(f"/api/v3/workbench/tasks/{created['task_id']}").json()

    for finding in view["findings"]:
        if finding["suggested_fix"] and finding["fix"] is None:
            assert finding["suggested_fix_is_not_applied"] is True
            assert finding["label"] != "Patch Verified"
            assert finding["label"] in {"Suspected", "Reproduced"}
    assert set(view["labels"].values()) == {
        "Suspected",
        "Reproduced",
        "Patch Verified",
        "Waiting Approval",
        "PR Created",
    }

    scripts = (WEB / "workbench.js").read_text(encoding="utf-8") + (
        WEB / "workbench.html"
    ).read_text(encoding="utf-8")
    assert 'label: "Fixed"' not in scripts
    assert "finding.label" in scripts


def test_web_and_cli_use_the_same_api_contract(
    settings: Settings, engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    scripts = (WEB / "workbench.js").read_text(encoding="utf-8")
    assert "/api/v3/workbench/tasks" in scripts
    assert 'taskAction("run")' in scripts
    for control in ("pause", "resume", "cancel"):
        assert f'taskAction("{control}")' in scripts
    for action in ("reproduce", "repair", "approve"):
        assert f'findingAction("{action}")' in scripts
    assert "/diff" in scripts
    assert "/messages" in scripts
    assert "payload_hash" in scripts
    assert "pull_request_link" in scripts
    assert "pull_request_url" not in scripts

    calls: list[tuple[str, str, Any]] = []

    class FakeResponse:
        status_code = 200
        text = '{"ok": true}'
        is_success = True

    class FakeClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def post(self, path: str, json: Any = None) -> FakeResponse:
            calls.append(("POST", path, json))
            return FakeResponse()

    monkeypatch.setattr(httpx, "Client", FakeClient)
    task_id = str(uuid4())
    finding_id = "a" * 64
    base = ["workbench", "--task-id", task_id, "--finding-id", finding_id, "--action"]
    assert cli.main([*base, "publish"]) == 0
    assert calls[-1][0] == "POST" and calls[-1][1].endswith("/publish")
    assert cli.main([*base, "reconcile"]) == 0
    assert calls[-1][0] == "POST" and calls[-1][1].endswith("/reconcile")


def test_missing_live_dependencies_wait_instead_of_failing(
    settings: Settings, engine: Engine
) -> None:
    with _client(settings, engine) as client:
        source = _create(client)
        live_id = _live_clone(engine, UUID(source["task_id"]), status="queued")
        failed = client.post(f"/api/v3/workbench/tasks/{live_id}/run")
        assert failed.status_code == 403, failed.text
        reason_code = failed.json()["code"]
        assert reason_code in {
            "REPO_ANALYSIS_PROFILE_REQUIRED",
            "REPO_ANALYSIS_PROFILE_INVALID",
        }
        view = client.get(f"/api/v3/workbench/tasks/{live_id}").json()

    assert view["status"] == "ready"
    assert view["phase"] == "waiting_input"
    assert view["next_action"] == "wait_for_dependencies"
    assert view["pending"]["state"] == "waiting_for_dependencies"
    assert view["pending"]["reason_code"] == reason_code
    assert view["failure"] is None
    assert not [event for event in view["timeline"] if event["status"] == "failed"]
    assert [event for event in view["timeline"] if event["kind"] == "dependencies.waiting"]


def test_missing_github_authorization_reports_waiting_approval(
    settings: Settings, engine: Engine
) -> None:
    with _client(settings, engine) as client:
        task_id, finding_id, payload_hash = _waiting_approval(client)
        live_id = _live_clone(engine, task_id)
        approved = client.post(
            f"/api/v3/workbench/tasks/{live_id}/findings/{finding_id}/approve",
            json={"payload_hash": payload_hash},
        )
        assert approved.status_code == 200, approved.text
        view = client.get(f"/api/v3/workbench/tasks/{live_id}").json()

    assert view["status"] == "waiting_approval"
    assert view["pending"]["state"] == "waiting_approval"
    assert view["pending"]["reason_code"] == "WRITE_PERMISSION_REQUIRED"
    assert view["failure"] is None
    publication = _finding(view)["publication"]
    assert publication["status"] == "exported"
    assert publication["pull_request_link"] is None


# -- negative paths ------------------------------------------------------


def test_unauthenticated_requests_are_rejected(settings: Settings, engine: Engine) -> None:
    from mgar_v3.api import create_app

    client = TestClient(
        create_app(settings, engine, run_background=False), base_url="http://127.0.0.1"
    )
    task_id = uuid4()
    assert client.get("/api/v3/workbench/tasks").status_code == 401
    assert (
        client.post(
            "/api/v3/workbench/tasks",
            json={
                "repository_url": f"https://github.com/{REPO}",
                "mode": "audit",
                "task": "未登录不应创建任务",
                "execution_mode": "fixture",
            },
        ).status_code
        == 401
    )
    assert (
        client.post(
            f"/api/v3/workbench/tasks/{task_id}/findings/{'a' * 64}/approve",
            json={"payload_hash": "b" * 64},
        ).status_code
        == 401
    )


def test_direct_approve_api_cannot_bypass_the_reviewed_hash(
    settings: Settings, engine: Engine
) -> None:
    with _client(settings, engine) as client:
        created = _create(client)
        task_id = created["task_id"]
        finding_id = str(_finding(created)["finding_id"])
        too_early = client.post(
            f"/api/v3/workbench/tasks/{task_id}/findings/{finding_id}/approve",
            json={"payload_hash": "0" * 64},
        )
        assert too_early.status_code == 409
        assert too_early.json()["code"] == "APPROVAL_NOT_PENDING"

        task_id, finding_id, payload_hash = _waiting_approval(client)
        stale = client.post(
            f"/api/v3/workbench/tasks/{task_id}/findings/{finding_id}/approve",
            json={"payload_hash": "0" * 64},
        )
        assert stale.status_code == 409
        assert stale.json()["code"] == "APPROVAL_STALE"
        unapproved = client.post(
            f"/api/v3/workbench/tasks/{task_id}/findings/{finding_id}/publish"
        )
        assert unapproved.status_code == 409
        assert unapproved.json()["code"] == "APPROVAL_REQUIRED"
        view = client.get(f"/api/v3/workbench/tasks/{task_id}").json()

    finding = _finding(view)
    assert finding["stage"] == "waiting_approval"
    assert finding["approval"]["payload_hash"] == payload_hash
    assert finding["publication"] is None


def test_stale_diff_hash_is_rejected_after_a_newer_review(
    settings: Settings, engine: Engine
) -> None:
    with _client(settings, engine) as client:
        task_id, finding_id, old_hash = _waiting_approval(client)
        service = WorkbenchService(engine, settings)
        finding = service.store.finding(task_id, finding_id)
        assert finding.fix is not None and finding.approval is not None
        changed = seal_bundle(
            repository=finding.fix.bundle.repository,
            base_branch=finding.fix.bundle.base_branch,
            base_sha=finding.fix.bundle.base_sha,
            tree_hash=finding.fix.bundle.tree_hash,
            proposal_id=finding.fix.bundle.proposal_id,
            branch=finding.fix.bundle.branch,
            title=finding.fix.bundle.title,
            body=finding.fix.bundle.body + "\n\nreviewer note: tightened validation",
            diff=finding.fix.bundle.diff,
            files=finding.fix.bundle.files,
        )
        assert changed.payload_hash != old_hash
        review = finding.approval.model_copy(
            update={
                "status": "pending",
                "payload_hash": changed.payload_hash,
                "approval_id": None,
                "actor": "",
                "approved_at": None,
            }
        )
        payload = finding.model_dump(mode="json")
        payload["fix"]["bundle"] = changed.model_dump(mode="json")
        payload["approval"] = review.model_dump(mode="json")
        service.store.upsert_findings(task_id, [WorkbenchFinding.model_validate(payload)])

        stale = client.post(
            f"/api/v3/workbench/tasks/{task_id}/findings/{finding_id}/approve",
            json={"payload_hash": old_hash},
        )
        assert stale.status_code == 409
        assert stale.json()["code"] == "APPROVAL_STALE"
        current = client.get(f"/api/v3/workbench/tasks/{task_id}").json()
        assert _finding(current)["stage"] == "waiting_approval"

        fresh = client.post(
            f"/api/v3/workbench/tasks/{task_id}/findings/{finding_id}/approve",
            json={"payload_hash": changed.payload_hash},
        )
        assert fresh.status_code == 200, fresh.text
        assert fresh.json()["stage"] == "pr_created"


def test_refresh_resubmission_is_idempotent(settings: Settings, engine: Engine) -> None:
    request_id = f"web-{uuid4()}"
    with _client(settings, engine) as client:
        first = _create(client, request_id=request_id)
        second = _create(client, request_id=request_id)
        assert second["task_id"] == first["task_id"]
        conflicting = client.post(
            "/api/v3/workbench/tasks",
            json={
                "repository_url": f"https://github.com/{REPO}",
                "mode": "audit",
                "task": "刷新后提交了不同的任务内容",
                "execution_mode": "fixture",
                "auto_start": True,
                "client_request_id": request_id,
            },
        )
        assert conflicting.status_code == 409
        assert conflicting.json()["code"] == "REQUEST_ID_REUSED"
        history = client.get("/api/v3/workbench/tasks").json()

    assert len([task for task in history if task["task_id"] == first["task_id"]]) == 1


def test_cancelled_task_cannot_become_a_publication(
    settings: Settings, engine: Engine
) -> None:
    with _client(settings, engine) as client:
        task_id, finding_id, payload_hash = _waiting_approval(client)
        cancelled = client.post(f"/api/v3/workbench/tasks/{task_id}/cancel")
        assert cancelled.status_code == 200, cancelled.text
        assert cancelled.json()["status"] == "cancelled"
        approve = client.post(
            f"/api/v3/workbench/tasks/{task_id}/findings/{finding_id}/approve",
            json={"payload_hash": payload_hash},
        )
        assert approve.status_code == 409
        view = client.get(f"/api/v3/workbench/tasks/{task_id}").json()

    assert view["status"] == "cancelled"
    assert view["pending"] == {"state": None, "reason_code": "", "reason": ""}
    assert _finding(view)["publication"] is None
    service = WorkbenchService(engine, settings)
    assert service.publications.latest(task_id, finding_id) is None


def test_fixture_publication_is_never_a_github_link(settings: Settings, engine: Engine) -> None:
    with _client(settings, engine) as client:
        task_id, finding_id, payload_hash = _waiting_approval(client)
        approved = client.post(
            f"/api/v3/workbench/tasks/{task_id}/findings/{finding_id}/approve",
            json={"payload_hash": payload_hash},
        )
        assert approved.status_code == 200, approved.text
        view = client.get(f"/api/v3/workbench/tasks/{task_id}").json()

    publication = _finding(view)["publication"]
    assert publication["remote"] == "in_memory_fixture"
    assert publication["pull_request_url"].startswith("fixture://")
    assert publication["pull_request_link"] is None
    assert _finding(view)["pull_request_link"] is None
    assert _finding(view)["stage"] == "pr_created"


def test_failure_reason_is_visible_in_view_and_history(
    settings: Settings, engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = WorkbenchService(engine, settings)
    queued = service.create(
        CreateWorkbenchTask(
            repository_url=f"https://github.com/{REPO}",
            mode="audit",
            task="检查未知请求处理路径",
            execution_mode="fixture",
            auto_start=False,
        )
    )
    task_id = UUID(str(queued["task_id"]))

    def boom(analysis_input: Any, *, actor: str | None = None) -> dict[str, Any]:
        raise DomainError("AUDIT_EXECUTION_FAILED", "fixture audit exploded", 400)

    monkeypatch.setattr(service.repo, "create", boom)
    with pytest.raises(DomainError):
        service.run(task_id)
    view = service.view(task_id)
    history = service.history()

    assert view["status"] == "failed"
    assert view["failure"]["reason_code"] == "AUDIT_EXECUTION_FAILED"
    assert view["failure"]["reason"] == "fixture audit exploded"
    assert view["failure"]["stage"] == "audit"
    assert str(task_id) in {str(task["task_id"]) for task in history}
