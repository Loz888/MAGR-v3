"""V3.5 U02: unified Finding -> content-addressed RepairHandoff.

A finding only leaves ``suspected`` with retained evidence: either a failing
execution receipt or an existing static-analysis confirmation.  The handoff
then pins the repository, commit, parent audit, allowed paths, budget and
authorization.  Live repair never falls back to the fixture Dispatcher, and a
missing handoff stops the task instead of inventing a target.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import pytest

from mgar_v3.domain import DomainError
from mgar_v3.findings.model import FindingEvidence
from mgar_v3.repair.handoff import RepairAuthorization
from mgar_v3.repository_analysis.domain import AnalysisLimits
from mgar_v3.scanners.domain import ScanLocation
from mgar_v3.workbench.domain import CreateWorkbenchTask
from mgar_v3.workbench.service import DISPATCHER_PATH, WorkbenchService

REPO = "octo/example"


def _service(settings, engine) -> WorkbenchService:
    return WorkbenchService(engine, settings)


def _create(
    service: WorkbenchService,
    *,
    mode: str = "audit",
    execution_mode: str = "fixture",
    auto_start: bool = True,
    request_id: str = "u02-test",
) -> dict:
    return service.create(
        CreateWorkbenchTask(
            repository_url=f"https://github.com/{REPO}",
            mode=mode,
            task="检查未知请求处理路径",
            execution_mode=execution_mode,
            auto_start=auto_start,
            client_request_id=request_id,
        )
    )


def _audit(service: WorkbenchService, *, request_id: str = "u02-audit") -> dict:
    payload = _create(service, request_id=request_id)
    return service.run(payload["task_id"])


def _claim(payload: dict) -> dict:
    return next(item for item in payload["findings"] if item["rule_id"] == "analysis-claim")


def _static_evidence(finding: dict) -> FindingEvidence:
    return FindingEvidence(
        evidence_id=finding["source"]["evidence_id"],
        kind="investigation",
        source_uri=finding["source"]["uri"],
        description="The read-only audit evidence was statically confirmed",
        content_hash=finding["source"]["content_hash"],
        location=ScanLocation(
            path=finding["path"],
            line=finding["start_line"],
            end_line=finding["end_line"],
        ),
    )


def _confirm(service: WorkbenchService, payload: dict, finding: dict | None = None) -> dict:
    target = finding or _claim(payload)
    return service.confirm_static(
        payload["task_id"],
        target["finding_id"],
        evidence=_static_evidence(target),
    )


def _authorization(**overrides) -> RepairAuthorization:
    body = {
        "approved_by": "owner",
        "expires_at": datetime.now(UTC) + timedelta(hours=1),
        "allowed_repositories": [REPO],
        "limits": AnalysisLimits(),
        "allow_repair": True,
    }
    body.update(overrides)
    return RepairAuthorization(**body)


def _tamper(service: WorkbenchService, payload: dict, finding: dict, **source) -> None:
    task_id = payload["task_id"]
    stored = service.store.finding(task_id, finding["finding_id"])
    tampered = stored.model_copy(
        update={"source": stored.source.model_copy(update=source)}
    )
    service.store.upsert_findings(task_id, [tampered])


def _clone(service: WorkbenchService, payload: dict, target: dict) -> dict:
    source_task = payload["task_id"]
    target_task = target["task_id"]
    original = service.store.finding(source_task, _claim(payload)["finding_id"])
    clone = original.model_copy(update={"task_id": target_task})
    service.store.upsert_findings(target_task, [clone])
    return clone.model_dump(mode="json")


# -- evidence binding ----------------------------------------------------
def test_audit_findings_bind_evidence_uri_lines_and_commit(settings, engine) -> None:
    service = _service(settings, engine)
    payload = _audit(service)

    assert payload["findings"]
    for finding in payload["findings"]:
        source = finding["source"]
        assert source["evidence_id"]
        assert source["uri"].startswith(f"github://{REPO}@{payload['commit']}/")
        assert source["commit"] == payload["commit"]
        assert len(source["content_hash"]) == 64
        assert 1 <= finding["start_line"] <= finding["end_line"]
        assert finding["path"] in source["uri"]
        assert finding["verification"] == "suspected"


def test_a_guessed_finding_cannot_be_confirmed_without_static_evidence(
    settings, engine
) -> None:
    service = _service(settings, engine)
    payload = _audit(service)
    claim = _claim(payload)
    evidence = _static_evidence(claim).model_copy(update={"kind": "file"})

    with pytest.raises(DomainError) as caught:
        service.confirm_static(
            payload["task_id"], claim["finding_id"], evidence=evidence
        )
    assert caught.value.code == "STATIC_EVIDENCE_REQUIRED"


# -- handoff gates -------------------------------------------------------
def test_suspected_finding_cannot_mint_a_handoff(settings, engine) -> None:
    service = _service(settings, engine)
    payload = _audit(service)

    with pytest.raises(DomainError) as caught:
        service.handoff(
            payload["task_id"],
            _claim(payload)["finding_id"],
            authorization=_authorization(),
        )
    assert caught.value.code == "HANDOFF_TARGET_UNVERIFIED"


def test_static_confirmation_mints_one_content_addressed_handoff(
    settings, engine
) -> None:
    service = _service(settings, engine)
    payload = _audit(service)
    claim = _claim(payload)
    confirmed = _confirm(service, payload, claim)
    assert confirmed["verification"] == "confirmed_static"

    handoff = service.handoff(
        payload["task_id"], claim["finding_id"], authorization=_authorization()
    )

    assert handoff["handoff_id"] == handoff["content_hash"]
    assert handoff["handoff_id"] == handoff["handoff"]["handoff_id"]
    assert handoff["repository"] == REPO
    assert handoff["base_commit"] == payload["commit"]
    assert handoff["finding_id"] == claim["finding_id"]
    assert handoff["parent_run_id"] == str(payload["audit_run_id"])
    assert handoff["parent_report_hash"]
    assert handoff["allowed_paths"] == [claim["path"]]
    assert claim["source"]["evidence_id"] in handoff["evidence_ids"]
    assert handoff["reproduction"]["kind"] == "static_confirmation"
    assert handoff["reproduction"]["receipt_id"] is None
    assert handoff["execution_mode"] == "fixture"
    assert handoff["reused"] is False

    content = engine.artifacts.read(handoff["content_hash"])
    assert hashlib.sha256(content).hexdigest() == handoff["handoff_id"]

    with engine.transaction() as conn:
        rows = conn.execute(
            "SELECT handoff_id,content_hash FROM app.workbench_handoffs WHERE task_id=%s",
            (payload["task_id"],),
        ).fetchall()
        artifact = conn.execute(
            "SELECT byte_count FROM app.artifacts WHERE content_hash=%s",
            (handoff["content_hash"],),
        ).fetchone()
    assert len(rows) == 1
    assert rows[0]["handoff_id"] == handoff["handoff_id"]
    assert artifact["byte_count"] == len(content)

    again = service.handoff(
        payload["task_id"], claim["finding_id"], authorization=_authorization()
    )
    assert again["reused"] is True
    assert again["handoff_id"] == handoff["handoff_id"]
    with engine.transaction() as conn:
        count = conn.execute(
            "SELECT count(*) AS n FROM app.workbench_handoffs WHERE task_id=%s",
            (payload["task_id"],),
        ).fetchone()
    assert count["n"] == 1


def test_handoff_requires_explicit_repair_authorization(settings, engine) -> None:
    service = _service(settings, engine)
    payload = _audit(service)
    _confirm(service, payload)

    with pytest.raises(DomainError) as caught:
        service.handoff(
            payload["task_id"],
            _claim(payload)["finding_id"],
            authorization=_authorization(allow_repair=False),
        )
    assert caught.value.code == "HANDOFF_AUTHORIZATION_DENIED"


def test_a_broader_authorization_cannot_replace_a_bound_handoff(
    settings, engine
) -> None:
    service = _service(settings, engine)
    payload = _audit(service)
    claim = _claim(payload)
    _confirm(service, payload)
    task_id = payload["task_id"]
    first = service.handoff(task_id, claim["finding_id"], authorization=_authorization())

    again = service.handoff(
        task_id,
        claim["finding_id"],
        authorization=_authorization(allowed_repositories=["*"]),
    )
    assert again["reused"] is True
    assert again["handoff_id"] == first["handoff_id"]
    assert again["handoff"]["authorization"]["allowed_repositories"] == [REPO]
    assert service.stored_handoff(task_id, claim["finding_id"])["handoff_id"] == first[
        "handoff_id"
    ]


def test_stale_commit_source_cross_repository_and_missing_evidence_fail(
    settings, engine
) -> None:
    service = _service(settings, engine)
    payload = _audit(service)
    claim = _claim(payload)
    _confirm(service, payload)
    task_id = payload["task_id"]

    other_commit = "a" * 40
    _tamper(service, payload, claim, commit=other_commit)
    with pytest.raises(DomainError) as caught:
        service.handoff(task_id, claim["finding_id"], authorization=_authorization())
    assert caught.value.code == "HANDOFF_VERSION_MISMATCH"

    _tamper(
        service,
        payload,
        claim,
        commit=payload["commit"],
        uri=f"github://other/repository@{payload['commit']}/{claim['path']}",
    )
    with pytest.raises(DomainError) as caught:
        service.handoff(task_id, claim["finding_id"], authorization=_authorization())
    assert caught.value.code == "HANDOFF_REPOSITORY_MISMATCH"

    _tamper(
        service,
        payload,
        claim,
        commit=payload["commit"],
        uri=f"github://{REPO}@{payload['commit']}/{claim['path']}",
        content_hash="",
    )
    with pytest.raises(DomainError) as caught:
        service.handoff(task_id, claim["finding_id"], authorization=_authorization())
    assert caught.value.code == "HANDOFF_EVIDENCE_MISSING"


def test_finding_evidence_changed_after_the_handoff_is_rejected(
    settings, engine
) -> None:
    service = _service(settings, engine)
    payload = _audit(service)
    claim = _claim(payload)
    _confirm(service, payload)
    task_id = payload["task_id"]
    first = service.handoff(task_id, claim["finding_id"], authorization=_authorization())

    _tamper(
        service,
        payload,
        claim,
        uri=f"github://{REPO}@{payload['commit']}/{claim['path']}?rev=2",
    )
    with pytest.raises(DomainError) as caught:
        service.handoff(task_id, claim["finding_id"], authorization=_authorization())
    assert caught.value.code == "HANDOFF_PATH_DENIED"
    assert service.stored_handoff(task_id, claim["finding_id"])["handoff_id"] == first[
        "handoff_id"
    ]


def test_an_unsettled_parent_audit_blocks_the_handoff(settings, engine) -> None:
    service = _service(settings, engine)
    payload = _audit(service)
    queued = _create(service, auto_start=False, request_id="u02-queued")
    clone = _clone(service, payload, queued)
    _confirm(service, queued, clone)

    with pytest.raises(DomainError) as caught:
        service.handoff(
            queued["task_id"],
            clone["finding_id"],
            authorization=_authorization(),
        )
    assert caught.value.code == "HANDOFF_PARENT_NOT_SETTLED"


def _live_task(service: WorkbenchService, payload: dict) -> dict:
    request = CreateWorkbenchTask(
        repository_url=f"https://github.com/{REPO}",
        mode="audit_and_repair",
        task="检查未知请求处理路径",
        execution_mode="live",
        client_request_id="u02-live",
    )
    task = service.store.create_task(
        request,
        repository=REPO,
        commit=payload["commit"],
        request_key="u02-live",
        request_digest="u02-live",
        task_text=request.task,
        limits=AnalysisLimits().model_dump(mode="json"),
    )
    service.store.link_run(task["task_id"], "audit", payload["audit_run_id"])
    return task


# -- live auto-repair ----------------------------------------------------
def test_live_auto_repair_waits_instead_of_using_the_dispatcher(
    settings, engine
) -> None:
    service = _service(settings, engine)
    audit = _audit(service)
    task = _live_task(service, audit)

    payload = service.run(task["task_id"])

    assert payload["execution_mode"] == "live"
    assert payload["status"] == "ready"
    assert payload["phase"] == "waiting_input"
    assert payload["findings"]
    assert all(item["path"] != DISPATCHER_PATH for item in payload["findings"])
    assert payload["handoffs"] == []
    assert any(event["kind"] == "handoff.waiting" for event in payload["timeline"])

    again = service.run(task["task_id"])
    assert again["phase"] == "waiting_input"
    assert again["handoffs"] == []
    assert service.stored_handoff(task["task_id"], payload["findings"][0]["finding_id"]) is None


def test_live_task_with_a_static_finding_still_needs_an_explicit_handoff(
    settings, engine
) -> None:
    service = _service(settings, engine)
    audit = _audit(service)
    task = _live_task(service, audit)
    live_view = service.run(task["task_id"])
    claim = _claim(live_view)
    confirmed = _confirm(service, live_view, claim)
    assert confirmed["verification"] == "confirmed_static"

    waiting = service.run(task["task_id"])
    assert waiting["phase"] == "waiting_input"
    assert waiting["handoffs"] == []

    handoff = service.handoff(
        task["task_id"], claim["finding_id"], authorization=_authorization()
    )
    assert handoff["execution_mode"] == "live"
    ready = service.run(task["task_id"])
    assert ready["phase"] == "waiting_input"
    assert any(item["handoff_id"] == handoff["handoff_id"] for item in ready["handoffs"])
