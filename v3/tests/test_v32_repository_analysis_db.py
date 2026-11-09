"""DB-backed V3.2 repository analysis tests.

These run only when a real PostgreSQL DSN is configured (for example through
``v3/.env`` + ``export MGAR_V3_DATABASE_URL``).  They are kept separate from the
network-free unit suite so engineering and live evidence stay distinguishable.
"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("MGAR_V3_DATABASE_URL"),
    reason="Set MGAR_V3_DATABASE_URL (v3/.env) to run PostgreSQL integration tests",
)


def _input(task: str, request_id: str):
    from mgar_v3.repository_analysis.domain import CreateAnalysisInput

    return CreateAnalysisInput(
        repository_url="https://github.com/octo/example",
        task=task,
        execution_mode="fixture",
        client_request_id=request_id,
    )


def test_fixture_run_persists_evidence_claims_and_report(engine, settings):
    from mgar_v3.repository_analysis.service import RepositoryAnalysisService

    service = RepositoryAnalysisService(engine, settings)
    spec = service.create(_input("trace the startup path", "req-1"))
    assert spec.execution_mode == "fixture"
    assert len(spec.commit) == 40
    service.execute(spec.run_id)
    detail = service.detail(spec.run_id)
    assert detail["run"]["run_status"] == "succeeded"
    assert detail["run"]["outcome"] == "internal_report"
    assert detail["claims"], "verified claims must be persisted"
    for claim in detail["claims"]:
        assert claim["evidence_id"]
        assert claim["quote"]
        assert claim["start_line"] >= 1
    assert any(call["result_version"] for call in detail["tool_calls"])
    report = service.report_bytes(spec.run_id).decode()
    assert f"github://octo/example@{spec.commit}/src/main.py" in report
    assert "L1-L3" in report
    assert "机械" in report


def test_idempotent_create_and_thread_follow_up_reuses_evidence(engine, settings):
    from mgar_v3.repository_analysis.domain import FollowUpInput
    from mgar_v3.repository_analysis.service import RepositoryAnalysisService

    service = RepositoryAnalysisService(engine, settings)
    first = service.create(_input("trace the startup path", "req-2"))
    duplicate = service.create(_input("trace the startup path", "req-2"))
    assert first.run_id == duplicate.run_id
    service.execute(first.run_id)

    second = service.follow_up(
        first.run_id, FollowUpInput(client_request_id="req-3", body="继续说明分发入口")
    )
    assert second.thread_id == first.thread_id
    assert second.parent_run_id == first.run_id
    assert second.prior_evidence_map, "parent evidence must be copied into the new run"
    service.execute(second.run_id)
    detail = service.detail(second.run_id)
    assert detail["run"]["run_status"] == "succeeded"
    assert detail["run"]["parent_run_id"] == str(first.run_id)


def test_pause_and_resume_checkpoint(engine, settings):
    from mgar_v3.repository_analysis.service import RepositoryAnalysisService

    service = RepositoryAnalysisService(engine, settings)
    spec = service.create(_input("trace the startup path", "req-4"))
    service.pause(spec.run_id)
    assert service.store.spec(spec.run_id).run_status == "paused"
    service.resume(spec.run_id)
    assert service.store.spec(spec.run_id).run_status == "queued"
    service.execute(spec.run_id)
    assert service.store.spec(spec.run_id).run_status == "succeeded"


def test_unknown_repository_identity_is_rejected_before_database(engine, settings):
    from mgar_v3.domain import DomainError
    from mgar_v3.repository_analysis.domain import CreateAnalysisInput
    from mgar_v3.repository_analysis.service import RepositoryAnalysisService

    service = RepositoryAnalysisService(engine, settings)
    with pytest.raises(ValueError):
        CreateAnalysisInput(
            repository_url="https://github.com.do/octo/example",
            task="x",
            client_request_id=str(uuid4()),
        )
    with pytest.raises(DomainError):
        service.detail(uuid4())
