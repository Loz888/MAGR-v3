"""V3.4 U01 unified repository workbench tests.

U01 unifies Audit, Issue Repair and Audit & Repair behind one Web/CLI/API
surface.  These tests assert the five review labels, the evidence gates that
keep a suggested fix from being displayed as a fix, the real reproduction and
patch receipts, the human approval bound to the reviewed payload hash, the
Draft-PR-only publication, and the pause/resume/cancel plus follow-up paths.
"""

from __future__ import annotations

import hashlib
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from mgar_v3.api import create_app
from mgar_v3.domain import DomainError
from mgar_v3.repair.pr_delivery import (
    DraftPullRequest,
    PublicationFile,
    PublicationStep,
    ReviewBundle,
    seal_bundle,
)
from mgar_v3.workbench.domain import (
    ALLOWED_FINDING_STAGES,
    STAGE_LABELS,
    ApprovalRecord,
    Classification,
    FindingSource,
    FindingStage,
    FixRecord,
    FixRisk,
    PublicationRecord,
    ReproductionPlan,
    ReproductionRecord,
    Severity,
    TestReceipt,
    WorkbenchFinding,
    advance_finding,
    derive_stage,
    rank_findings,
    score_finding,
)

REPO = "octo/example"
BASE_SHA = "a" * 40
COMMAND = "python -m pytest -q tests/test_dispatcher.py"
DIFF = (
    "diff --git a/src/dispatcher.py b/src/dispatcher.py\n"
    "--- a/src/dispatcher.py\n"
    "+++ b/src/dispatcher.py\n"
    "@@ -1,4 +1,2 @@\n"
    " def dispatch(request: str) -> str:\n"
    "-    if request == 'startup':\n"
    "-        return execute(request)\n"
    "-    return 'unknown'\n"
    "+    return execute(request)\n"
)


# -- helpers ------------------------------------------------------------
def _receipt(*, passed: bool) -> TestReceipt:
    return TestReceipt(
        receipt_id="receipt-pass" if passed else "receipt-fail",
        command=COMMAND,
        exit_code=0 if passed else 1,
        passed=passed,
        tests=["tests/test_dispatcher.py::test_unknown_request_is_handled"],
        skipped=0,
        duration_ms=5,
        log_sha256=("b" if passed else "c") * 64,
        output_excerpt="1 passed" if passed else "1 failed",
    )


def _bundle() -> ReviewBundle:
    return seal_bundle(
        repository=REPO,
        base_branch="main",
        base_sha=BASE_SHA,
        tree_hash="d" * 64,
        proposal_id="e" * 64,
        branch="mgar/u01-test",
        title="Fix the silent unknown dispatch",
        body="Verified repair proposal",
        diff=DIFF,
        files=[PublicationFile(path="src/dispatcher.py", content="fixed\n")],
    )


def _fix(bundle: ReviewBundle) -> FixRecord:
    return FixRecord(
        suggested_fix="Route every request through execute().",
        diff=DIFF,
        diff_sha256=hashlib.sha256(DIFF.encode()).hexdigest(),
        files=["src/dispatcher.py"],
        tree_hash_before="f" * 64,
        tree_hash_after="d" * 64,
        before_receipt=_receipt(passed=False),
        after_receipt=_receipt(passed=True),
        bundle=bundle,
    )


def _publication() -> PublicationRecord:
    return PublicationRecord(
        status="published",
        steps=[
            PublicationStep(ordinal=0, kind="branch", status="succeeded"),
            PublicationStep(ordinal=1, kind="commit", status="succeeded"),
            PublicationStep(ordinal=2, kind="pull_request", status="succeeded"),
        ],
        pull_request=DraftPullRequest(
            number=1,
            draft=True,
            title="Fix the silent unknown dispatch",
            body="Draft PR proposal",
            head_ref="mgar/u01-test",
            head_sha="3" * 40,
            base_ref="main",
            base_sha=BASE_SHA,
            repository=REPO,
        ),
        pull_request_url="fixture://github/octo/example/pull/1",
        published_at="2026-11-09T00:00:00+00:00",
    )


def _finding(
    *,
    task_id: UUID,
    finding_id: str,
    severity: Severity = "warning",
    classification: Classification = "defect",
    confidence: float = 0.6,
    fix_risk: FixRisk = "low",
    stage: FindingStage = "suspected",
    reproduction: ReproductionRecord | None = None,
    suggested_fix: str = "",
    plan: ReproductionPlan | None = None,
) -> WorkbenchFinding:
    return WorkbenchFinding(
        finding_id=finding_id,
        task_id=task_id,
        title="Unknown requests are silently degraded",
        summary="dispatch() returns the string 'unknown' instead of executing the request.",
        path="src/dispatcher.py",
        severity=severity,
        classification=classification,
        fix_risk=fix_risk,
        confidence=confidence,
        source=FindingSource(
            uri=f"github://{REPO}@{BASE_SHA}/src/dispatcher.py",
            evidence_id="evidence-1",
            quote="return 'unknown'",
            content_hash="2" * 64,
        ),
        stage=stage,
        reproduction=reproduction,
        suggested_fix=suggested_fix,
        reproduction_plan=plan,
        risk_score=score_finding(
            severity=severity,
            classification=classification,
            confidence=confidence,
            fix_risk=fix_risk,
        ),
    )


def _client(settings, engine) -> TestClient:
    return TestClient(
        create_app(settings, engine, run_background=False),
        base_url="http://127.0.0.1",
        headers={"X-MGAR-Token": settings.local_token.get_secret_value()},
    )


def _create(client: TestClient, **overrides):
    body = {
        "repository_url": f"https://github.com/{REPO}",
        "mode": "audit",
        "task": "检查未知请求处理路径",
        "execution_mode": "fixture",
        "auto_start": True,
    }
    body.update(overrides)
    return client.post("/api/v3/workbench/tasks", json=body)


def _dispatcher_finding(payload: dict) -> dict:
    return next(
        item for item in payload["findings"] if item["rule_id"] == "WB-DISPATCH-SILENT-UNKNOWN"
    )


# -- domain: labels and evidence gates ----------------------------------
def test_the_five_review_labels_never_include_fixed() -> None:
    assert list(STAGE_LABELS.values()) == [
        "Suspected",
        "Reproduced",
        "Patch Verified",
        "Waiting Approval",
        "PR Created",
    ]
    assert not any(label.lower() == "fixed" for label in STAGE_LABELS.values())
    assert ALLOWED_FINDING_STAGES["suspected"] == {"reproduced"}
    assert ALLOWED_FINDING_STAGES["pr_created"] == set()


def test_a_suggested_fix_is_reported_as_a_proposal_not_a_fix() -> None:
    finding = _finding(
        task_id=uuid4(),
        finding_id="1" * 64,
        suggested_fix="Route every request through execute().",
        plan=ReproductionPlan(
            test_file="tests/test_dispatcher.py",
            test_node="tests/test_dispatcher.py::test_unknown_request_is_handled",
        ),
    )

    view = finding.view()
    assert view["stage"] == "suspected"
    assert view["label"] == "Suspected"
    assert view["verified"] is False
    assert view["fix_state"] == "proposed_not_verified"
    assert view["suggested_fix_is_not_applied"] is True
    assert view["suggested_fix"] == "Route every request through execute()."
    assert "proposal, not an applied change" in view["disclaimer"]


def test_a_complete_patch_cannot_skip_the_reproduction_gate() -> None:
    bundle = _bundle()
    suspected = _finding(task_id=uuid4(), finding_id="1" * 64)

    with pytest.raises(DomainError) as caught:
        advance_finding(
            suspected,
            fix=_fix(bundle),
            approval=ApprovalRecord(
                status="pending",
                payload_hash=bundle.payload_hash,
                requested_at="2026-11-09T00:00:00Z",
            ),
        )
    assert caught.value.code == "FINDING_STAGE_INVALID"


def test_the_five_stages_require_their_full_evidence() -> None:
    task_id = uuid4()
    bundle = _bundle()
    reproduced = _finding(
        task_id=task_id,
        finding_id="1" * 64,
        stage="reproduced",
        reproduction=ReproductionRecord(
            status="reproduced",
            record="pytest failed on the unpatched tree",
            command=COMMAND,
            receipt=_receipt(passed=False),
        ),
    )

    verified = advance_finding(reproduced, fix=_fix(bundle))
    assert verified.stage == "patch_verified"
    assert verified.view()["verified"] is True

    pending = ApprovalRecord(
        status="pending",
        payload_hash=bundle.payload_hash,
        requested_at="2026-11-09T00:00:00Z",
    )
    waiting = advance_finding(verified, approval=pending)
    assert waiting.stage == "waiting_approval"

    approved = pending.model_copy(
        update={
            "status": "approved",
            "actor": "owner",
            "approved_at": "2026-11-09T00:01:00Z",
            "approval_id": "a" * 16,
        }
    )
    created = advance_finding(waiting, approval=approved, publication=_publication())
    assert created.stage == "pr_created"
    view = created.view()
    assert view["label"] == "PR Created"
    assert view["fix_state"] == "published"
    assert view["publication"]["merged"] is False
    assert view["publication"]["pull_request"]["draft"] is True


def test_a_fix_requires_a_failing_before_and_a_passing_after_receipt() -> None:
    bundle = _bundle()
    with pytest.raises(ValueError):
        FixRecord(
            suggested_fix="Route every request through execute().",
            diff=DIFF,
            diff_sha256=hashlib.sha256(DIFF.encode()).hexdigest(),
            files=["src/dispatcher.py"],
            tree_hash_before="f" * 64,
            tree_hash_after="d" * 64,
            before_receipt=_receipt(passed=True),
            after_receipt=_receipt(passed=True),
            bundle=bundle,
        )


def test_a_publication_receipt_without_an_approval_is_rejected() -> None:
    with pytest.raises(ValueError):
        derive_stage(reproduction=None, fix=None, approval=None, publication=_publication())


def test_risk_ranking_places_the_high_impact_defect_first() -> None:
    task_id = uuid4()
    error = _finding(
        task_id=task_id,
        finding_id="1" * 64,
        severity="error",
        classification="defect",
        confidence=0.9,
    )
    warning = _finding(
        task_id=task_id,
        finding_id="2" * 64,
        severity="warning",
        classification="risk",
        confidence=0.7,
    )
    info = _finding(
        task_id=task_id,
        finding_id="3" * 64,
        severity="info",
        classification="quality",
        confidence=0.9,
    )

    ranked = rank_findings([info, error, warning])
    assert [item.finding_id for item in ranked] == ["1" * 64, "2" * 64, "3" * 64]
    assert [item.risk_rank for item in ranked] == [1, 2, 3]


# -- API: one surface for the three task modes --------------------------
def test_profile_publishes_modes_labels_and_no_merge(settings, engine) -> None:
    with _client(settings, engine) as client:
        profile = client.get("/api/v3/workbench/profile").json()

    assert profile["modes"] == ["audit", "issue_repair", "audit_and_repair"]
    assert profile["execution_modes"] == ["fixture", "live"]
    assert profile["fixture_repair"] is True
    assert profile["live_repair"] == "r05-profile-required"
    assert profile["merge_supported"] is False
    assert list(profile["stages"].values()) == list(STAGE_LABELS.values())
    assert "analysis" in profile


def test_audit_returns_ranked_suspected_findings_with_sources(settings, engine) -> None:
    with _client(settings, engine) as client:
        response = _create(client)
        assert response.status_code == 202
        payload = response.json()

        assert payload["mode"] == "audit"
        assert payload["status"] == "ready"
        assert payload["repository"] == REPO
        assert len(payload["commit"]) == 40
        assert payload["coverage"]["observed"]["files_scanned"] == 4
        assert payload["coverage"]["observed"]["complete"] is True
        assert "Python" in payload["coverage"]["observed"]["languages"]

        assert payload["findings"]
        assert payload["findings"][0]["risk_rank"] == 1
        assert payload["findings"][0]["rule_id"] == "WB-DISPATCH-SILENT-UNKNOWN"
        scores = [item["risk_score"] for item in payload["findings"]]
        assert scores == sorted(scores, reverse=True)
        assert {item["label"] for item in payload["findings"]} == {"Suspected"}

        top = _dispatcher_finding(payload)
        assert top["suggested_fix"]
        assert top["suggested_fix_is_not_applied"] is True
        assert top["verified"] is False
        assert top["source"]["uri"].startswith(f"github://{REPO}@{payload['commit']}/")
        assert top["source"]["quote"]
        assert len(top["source"]["content_hash"]) == 64
        assert top["reproduction_plan"]["test_node"].endswith("::test_unknown_request_is_handled")

        detail = client.get(
            f"/api/v3/workbench/tasks/{payload['task_id']}/findings/{top['finding_id']}"
        )
        assert detail.status_code == 200
        assert detail.json()["source"]["evidence_id"] == top["source"]["evidence_id"]

        memory = payload["memory"]
        assert memory["retrieval_mode"] != "not_run"
        assert set(memory) >= {"advisories", "plan_hints", "confirmed_facts", "excluded"}

        assert payload["timeline"]
        assert {event["stage"] for event in payload["timeline"]} >= {"task", "audit", "agent"}

        listed = client.get("/api/v3/workbench/tasks").json()
        assert listed[0]["task_id"] == payload["task_id"]


def test_reproduce_repair_review_and_approve_yield_a_draft_pr(settings, engine) -> None:
    with _client(settings, engine) as client:
        task_id = _create(client).json()["task_id"]
        finding_id = _dispatcher_finding(client.get(f"/api/v3/workbench/tasks/{task_id}").json())[
            "finding_id"
        ]

        reproduced = client.post(
            f"/api/v3/workbench/tasks/{task_id}/findings/{finding_id}/reproduce"
        )
        assert reproduced.status_code == 200
        payload = reproduced.json()
        assert payload["stage"] == "reproduced"
        assert payload["label"] == "Reproduced"
        assert payload["reproduction"]["status"] == "reproduced"
        receipt = payload["reproduction"]["receipt"]
        assert receipt["passed"] is False
        assert receipt["exit_code"] != 0
        assert "pytest" in receipt["command"]
        assert len(receipt["log_sha256"]) == 64

        repaired = client.post(f"/api/v3/workbench/tasks/{task_id}/findings/{finding_id}/repair")
        assert repaired.status_code == 200, repaired.text
        payload = repaired.json()
        assert payload["stage"] == "waiting_approval"
        assert payload["label"] == "Waiting Approval"
        fix = payload["fix"]
        assert fix["before_receipt"]["passed"] is False
        assert fix["after_receipt"]["passed"] is True
        assert payload["verified"] is True
        assert payload["approval"]["status"] == "pending"
        assert payload["suggested_fix_is_not_applied"] is False

        diff = client.get(f"/api/v3/workbench/tasks/{task_id}/findings/{finding_id}/diff")
        assert diff.status_code == 200
        review = diff.json()
        assert review["diff"].startswith("diff --git a/src/dispatcher.py b/src/dispatcher.py")
        assert review["diff_sha256"] == hashlib.sha256(review["diff"].encode()).hexdigest()
        assert review["files"] == ["src/dispatcher.py"]
        assert review["before_receipt"]["passed"] is False
        assert review["after_receipt"]["passed"] is True
        assert review["reviewed"] is True

        stale = client.post(
            f"/api/v3/workbench/tasks/{task_id}/findings/{finding_id}/approve",
            json={"payload_hash": "0" * 64},
        )
        assert stale.status_code == 409
        assert stale.json()["code"] == "APPROVAL_STALE"

        approved = client.post(
            f"/api/v3/workbench/tasks/{task_id}/findings/{finding_id}/approve",
            json={"payload_hash": review["payload_hash"]},
        )
        assert approved.status_code == 200
        payload = approved.json()
        assert payload["stage"] == "pr_created"
        assert payload["label"] == "PR Created"
        publication = payload["publication"]
        assert publication["status"] == "published"
        assert publication["merged"] is False
        assert publication["pull_request"]["draft"] is True
        assert [step["kind"] for step in publication["steps"]] == [
            "branch",
            "commit",
            "pull_request",
        ]
        assert publication["pull_request_url"].startswith(
            f"fixture://github/{REPO}/pull/"
        )

        task = client.get(f"/api/v3/workbench/tasks/{task_id}").json()
        assert task["status"] == "completed"
        assert task["summary"]["pr_created"] == 1
        assert task["controls"]["can_cancel"] is False
        assert {event["stage"] for event in task["timeline"]} >= {
            "reproduce",
            "repair",
            "approval",
            "publication",
        }


def test_a_suggested_fix_cannot_be_repaired_without_reproduction(settings, engine) -> None:
    with _client(settings, engine) as client:
        payload = _create(client).json()
        finding_id = _dispatcher_finding(payload)["finding_id"]

        blocked = client.post(
            f"/api/v3/workbench/tasks/{payload['task_id']}/findings/{finding_id}/repair"
        )
        assert blocked.status_code == 409
        assert blocked.json()["code"] == "FINDING_NOT_REPRODUCED"

        unchanged = client.get(
            f"/api/v3/workbench/tasks/{payload['task_id']}/findings/{finding_id}"
        ).json()
        assert unchanged["stage"] == "suspected"
        assert unchanged["fix"] is None
        assert unchanged["suggested_fix_is_not_applied"] is True

        missing = client.get(
            f"/api/v3/workbench/tasks/{payload['task_id']}/findings/{finding_id}/diff"
        )
        assert missing.status_code == 404
        assert missing.json()["code"] == "DIFF_NOT_READY"


def test_issue_repair_mode_seeds_the_issue_and_verifies_a_patch(settings, engine) -> None:
    with _client(settings, engine) as client:
        response = _create(
            client,
            mode="issue_repair",
            issue_number=17,
            task="修复 Issue 17：未知请求被静默吞掉",
        )
        assert response.status_code == 202, response.text
        payload = response.json()

        assert payload["mode"] == "issue_repair"
        assert payload["status"] == "waiting_approval"
        assert payload["stage"] == "waiting_approval"
        finding = next(item for item in payload["findings"] if item["source"]["kind"] == "issue")
        assert finding["source"]["uri"].endswith("/issues/17")
        assert finding["stage"] == "waiting_approval"
        assert finding["fix"]["after_receipt"]["passed"] is True
        assert finding["verified"] is True
        assert finding["approval"]["status"] == "pending"


def test_audit_and_repair_mode_stops_at_human_approval(settings, engine) -> None:
    with _client(settings, engine) as client:
        response = _create(client, mode="audit_and_repair", task="审计并修复未知请求处理路径")
        assert response.status_code == 202, response.text
        payload = response.json()

        assert payload["mode"] == "audit_and_repair"
        assert payload["status"] == "waiting_approval"
        assert payload["next_action"] == "approve"
        assert payload["summary"]["waiting_approval"] == 1
        assert payload["summary"]["total"] >= 4
        verified = [item for item in payload["findings"] if item["fix"] is not None]
        assert [item["rule_id"] for item in verified] == ["WB-DISPATCH-SILENT-UNKNOWN"]
        assert verified[0]["label"] == "Waiting Approval"


def test_pause_resume_cancel_and_follow_up_keep_one_thread(settings, engine) -> None:
    with _client(settings, engine) as client:
        queued = _create(client, auto_start=False).json()
        task_id = queued["task_id"]
        assert queued["status"] == "queued"
        assert queued["next_action"] == "run"

        paused = client.post(f"/api/v3/workbench/tasks/{task_id}/pause").json()
        assert paused["status"] == "paused"
        assert paused["next_action"] == "resume"

        resumed = client.post(f"/api/v3/workbench/tasks/{task_id}/resume").json()
        assert resumed["status"] == "queued"

        cancelled = client.post(f"/api/v3/workbench/tasks/{task_id}/cancel").json()
        assert cancelled["status"] == "cancelled"
        assert cancelled["next_action"] == "none"

        again = client.post(f"/api/v3/workbench/tasks/{task_id}/pause")
        assert again.status_code == 409
        assert again.json()["code"] == "TASK_NOT_PAUSABLE"

        parent = _create(client).json()
        follow_up = client.post(
            f"/api/v3/workbench/tasks/{parent['task_id']}/messages",
            json={"body": "再解释一下这些 finding 的来源", "client_request_id": "follow-1"},
        )
        assert follow_up.status_code == 202
        child = follow_up.json()
        assert child["task_id"] != parent["task_id"]
        assert child["parent_task_id"] == parent["task_id"]
        assert child["thread_id"] == parent["thread_id"]
        assert child["mode"] == "audit"
        assert child["status"] == "ready"
        assert child["findings"]


def test_creation_validates_modes_urls_coverage_and_duplicate_requests(settings, engine) -> None:
    with _client(settings, engine) as client:
        first = _create(client, client_request_id="u01-idem").json()
        second = _create(client, client_request_id="u01-idem").json()
        assert second["task_id"] == first["task_id"]

        reused = _create(
            client, client_request_id="u01-idem", task="另一个完全不同的审计任务"
        )
        assert reused.status_code == 409
        assert reused.json()["code"] == "REQUEST_ID_REUSED"

        bad_url = _create(client, repository_url="https://gitlab.com/octo/example")
        assert bad_url.status_code == 422
        assert bad_url.json()["code"] == "INVALID_INPUT"

        bad_path = _create(
            client, coverage={"scope": "paths", "include_paths": ["../outside"]}
        )
        assert bad_path.status_code == 422

        empty_scope = _create(client, coverage={"scope": "paths", "include_paths": []})
        assert empty_scope.status_code == 422

        missing_issue = _create(client, mode="issue_repair", task="修复这个问题")
        assert missing_issue.status_code == 422

        live_repair = _create(
            client,
            mode="issue_repair",
            issue_number=3,
            execution_mode="live",
            task="修复这个问题",
        )
        assert live_repair.status_code == 403
        assert live_repair.json()["code"] == "WORKBENCH_LIVE_REPAIR_PROFILE_REQUIRED"
