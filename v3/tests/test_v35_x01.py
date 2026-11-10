"""V3.5 X01: on-demand, read-only experts instead of a default expert trio."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from mgar_v3.repository_analysis.agent import RepoAnalysisAgent
from mgar_v3.repository_analysis.domain import (
    AnalysisLimits,
    AnalysisModelConfig,
    AnalysisSpec,
)
from mgar_v3.routing.experts import (
    BenchmarkRow,
    ExpertAdvisory,
    ExpertBudget,
    ExpertContext,
    ExpertCoordinator,
    ExpertDecision,
    ExpertRouter,
    summarize_benchmark,
)

COMMIT = "c" * 40


def _analysis_spec() -> AnalysisSpec:
    return AnalysisSpec(
        run_id=uuid4(),
        thread_id=uuid4(),
        ticket_id=uuid4(),
        repository="example/mgar-test",
        requested_ref=None,
        commit=COMMIT,
        task="fix the nil pointer",
        execution_mode="fixture",
        limits=AnalysisLimits(max_model_calls=6, max_tool_calls=20, max_input_tokens=6000),
        model=AnalysisModelConfig(),
        deadline_at=datetime.now(UTC) + timedelta(minutes=5),
        authorization_hash="a" * 64,
        profile_hash="b" * 64,
    )


def _advisory(
    role: str, context: ExpertContext, *, summary: str | None = None, **update: Any
) -> ExpertAdvisory:
    return ExpertAdvisory(
        role=role,  # type: ignore[arg-type]
        status=update.pop("status", "succeeded"),
        summary=summary if summary is not None else f"{role} diagnosis",
        input_revision=context.input_revision,
        **update,
    )


class _Provider:
    def __init__(self, handler: Callable[[str, ExpertContext], ExpertAdvisory]):
        self.handler = handler
        self.calls = 0

    def investigate(
        self, role: str, context: ExpertContext, *, budget_seconds: float
    ) -> ExpertAdvisory:
        self.calls += 1
        return self.handler(role, context)


def _providers(**handlers: Callable[[str, ExpertContext], ExpertAdvisory]) -> dict[str, Any]:
    return {role: _Provider(handler) for role, handler in handlers.items()}


def _success(role: str, context: ExpertContext) -> ExpertAdvisory:
    return _advisory(role, context)


def _all_roles() -> dict[str, Any]:
    return _providers(repo=_success, knowledge=_success, data=_success)


def _complex_question() -> ExpertContext:
    return ExpertContext(
        task="Summarize documented duplicate-count symptoms",
        question="Summarize documented duplicate-count symptoms",
        input_revision=4,
        evidence_ids=["ev-1"],
    )


def test_single_file_bug_keeps_one_main_agent() -> None:
    decision = ExpertRouter().route(
        ExpertContext(
            task="Fix nil pointer in cmd/server.go",
            question="Fix nil pointer in cmd/server.go",
            changed_files=["cmd/server.go"],
            input_revision=3,
        )
    )
    assert decision.mode == "single_agent"
    assert decision.roles == []
    assert decision.signals == []
    assert decision.input_revision == 3
    assert decision.main_agent_decides is True
    assert decision.write_access is False


def test_deterministic_triggers_map_to_expected_roles() -> None:
    router = ExpertRouter()
    cases = [
        (
            ExpertContext(task="apply change", changed_files=["a.py", "b.py"]),
            ["repo"],
            "multi_file",
        ),
        (ExpertContext(task="apply change", dependency_edges=2), ["repo"], "dependency"),
        (
            ExpertContext(
                task="apply change",
                failed_tests=["tests/a.py::x", "tests/b.py::y"],
            ),
            ["repo"],
            "verification_failure",
        ),
        (
            ExpertContext(task="apply change", failed_tests=["tests/a.py::x"], failed_attempts=2),
            ["repo"],
            "verification_failure",
        ),
        (
            ExpertContext(task="apply change", security_findings=["hardcoded token"]),
            ["repo", "knowledge"],
            "security",
        ),
        (
            ExpertContext(task="apply change", conflicts=["documented policy conflicts"]),
            ["knowledge"],
            "conflict",
        ),
        (
            ExpertContext(task="apply change", user_requested_experts=True),
            ["repo", "knowledge", "data"],
            "user_request",
        ),
    ]
    for context, roles, kind in cases:
        decision = router.route(context)
        assert decision.mode == "experts"
        assert decision.roles == roles
        assert kind in {signal.kind for signal in decision.signals}


def test_single_failed_file_does_not_trigger_verification_experts() -> None:
    decision = ExpertRouter().route(
        ExpertContext(task="apply change", failed_tests=["tests/a.py::x"])
    )
    assert decision.mode == "single_agent"
    assert decision.roles == []


def test_documented_data_question_selects_three_roles_and_cross_source() -> None:
    decision = ExpertRouter().route(_complex_question())
    assert decision.mode == "experts"
    assert decision.roles == ["repo", "knowledge", "data"]
    assert "cross_source" in {signal.kind for signal in decision.signals}


def test_chinese_documented_metric_question_selects_three_roles() -> None:
    decision = ExpertRouter().route(
        ExpertContext(task="根据文档口径统计订单重复问题", input_revision=2)
    )
    assert decision.roles == ["repo", "knowledge", "data"]


def test_round_budget_and_expert_cap_stop_selection() -> None:
    exhausted = ExpertRouter().route(
        ExpertContext(task="documented count duplicate", attempted_rounds=2, input_revision=5)
    )
    assert exhausted.mode == "single_agent"
    assert exhausted.roles == []
    assert exhausted.stopped_reason == "ROUND_BUDGET"

    capped = ExpertRouter(ExpertBudget(max_experts=2)).route(
        ExpertContext(task="documented count duplicate", input_revision=6)
    )
    assert capped.mode == "experts"
    assert capped.roles == ["repo", "knowledge"]
    assert capped.stopped_reason == "EXPERT_BUDGET"


def test_expert_budget_hard_upper_bounds() -> None:
    for update in (
        {"max_experts": 4},
        {"max_rounds": 4},
        {"max_calls_per_revision": 4},
        {"timeout_seconds": 0},
    ):
        with pytest.raises(ValidationError):
            ExpertBudget(**update)


def test_experts_run_in_parallel_on_one_revision() -> None:
    barrier = threading.Barrier(3)

    def handler(role: str, context: ExpertContext) -> ExpertAdvisory:
        barrier.wait(timeout=5)
        return _advisory(role, context)

    coordinator = ExpertCoordinator(
        _providers(repo=handler, knowledge=handler, data=handler),
        budget=ExpertBudget(timeout_seconds=5),
    )
    outcome = coordinator.consult(_complex_question())
    assert outcome.expert_calls == 3
    assert {item.role for item in outcome.advisories} == {"repo", "knowledge", "data"}
    assert all(item.input_revision == 4 for item in outcome.advisories)
    assert outcome.duplicates_skipped == []
    assert outcome.degraded is False
    assert outcome.main_state_mutated is False
    assert outcome.write_access is False
    payload = outcome.to_payload()
    assert payload["main_agent_decides"] is True
    assert payload["write_access"] is False
    assert all(item["read_only"] is True for item in payload["advisories"])


def test_repeated_consult_deduplicates_without_new_calls_or_budget() -> None:
    coordinator = ExpertCoordinator(
        _all_roles(), budget=ExpertBudget(max_calls_per_revision=3)
    )
    context = _complex_question()
    first = coordinator.consult(context)
    assert first.expert_calls == 3

    second = coordinator.consult(context)
    assert second.expert_calls == 0
    assert second.duplicates_skipped == ["repo", "knowledge", "data"]
    assert [item.role for item in second.advisories] == ["repo", "knowledge", "data"]
    assert second.decision.mode == "experts"

    other = context.model_copy(update={"question": "different question"})
    third = coordinator.consult(other)
    assert third.expert_calls == 0
    assert third.decision.mode == "single_agent"
    assert third.decision.stopped_reason == "CALL_BUDGET"
    assert third.decision.roles == []


def test_call_budget_truncates_roles_and_keeps_cached_results() -> None:
    coordinator = ExpertCoordinator(
        _all_roles(), budget=ExpertBudget(max_calls_per_revision=1)
    )
    context = _complex_question()
    first = coordinator.consult(context)
    assert first.expert_calls == 1
    assert first.decision.mode == "experts"
    assert first.decision.roles == ["repo"]
    assert first.decision.stopped_reason == "CALL_BUDGET"
    assert [item.role for item in first.advisories] == ["repo"]

    second = coordinator.consult(context)
    assert second.expert_calls == 0
    assert second.duplicates_skipped == ["repo"]
    assert [item.role for item in second.advisories] == ["repo"]
    assert second.decision.stopped_reason == "CALL_BUDGET"


def test_provider_failure_is_degraded_and_cannot_mutate_main_state() -> None:
    def broken(role: str, context: ExpertContext) -> ExpertAdvisory:
        raise RuntimeError("provider exploded")

    main_state = {"status": "running", "history": [{"tool": "read_file"}]}
    coordinator = ExpertCoordinator(_providers(repo=broken))
    outcome = coordinator.consult(
        ExpertContext(task="fix code error", input_revision=1), main_state=main_state
    )
    assert [item.role for item in outcome.advisories] == ["repo"]
    assert outcome.advisories[0].status == "failed"
    assert outcome.advisories[0].error_code == "EXPERT_FAILED"
    assert outcome.degraded is True
    assert outcome.main_state_mutated is False
    assert main_state == {"status": "running", "history": [{"tool": "read_file"}]}


def test_timeout_empty_conflict_and_invalid_evidence_degrade() -> None:
    def legacy(role: str, context: ExpertContext) -> ExpertAdvisory:
        time.sleep(0.2)
        return _advisory(role, context)

    slow = ExpertCoordinator(
        _providers(repo=legacy), budget=ExpertBudget(timeout_seconds=0.05)
    )
    timed_out = slow.consult(ExpertContext(task="fix code error", input_revision=1))
    assert timed_out.advisories[0].status == "timeout"
    assert timed_out.advisories[0].error_code == "EXPERT_TIMEOUT"

    empty = ExpertCoordinator(
        _providers(
            repo=lambda role, context: ExpertAdvisory(
                role="repo", status="succeeded", summary="  ", input_revision=1
            )
        )
    ).consult(ExpertContext(task="fix code error", input_revision=1))
    assert empty.advisories[0].status == "empty"
    assert empty.degraded is True

    conflict = ExpertCoordinator(
        _providers(
            repo=lambda role, context: _advisory(role, context, status="conflict")
        )
    ).consult(ExpertContext(task="fix code error", input_revision=1))
    assert conflict.advisories[0].status == "conflict"
    assert conflict.degraded is True

    forged = ExpertCoordinator(
        _providers(
            repo=lambda role, context: _advisory(
                role, context, facts=["forged"], evidence_ids=["ev-outside"]
            )
        )
    ).consult(
        ExpertContext(
            task="fix code error", input_revision=1, evidence_ids=["ev-allowed"]
        )
    )
    assert forged.advisories[0].status == "invalid_evidence"
    assert forged.advisories[0].facts == []
    assert forged.degraded is True

    no_evidence = ExpertCoordinator(
        _providers(
            repo=lambda role, context: _advisory(role, context, evidence_ids=["ev-1"])
        )
    ).consult(ExpertContext(task="fix code error", input_revision=1))
    assert no_evidence.advisories[0].status == "invalid_evidence"


def test_unavailable_provider_and_stale_revision_degrade() -> None:
    coordinator = ExpertCoordinator(_providers(repo=_success))
    outcome = coordinator.consult(_complex_question())
    statuses = {item.role: item for item in outcome.advisories}
    assert statuses["repo"].status == "succeeded"
    assert statuses["knowledge"].error_code == "EXPERT_UNAVAILABLE"
    assert statuses["data"].error_code == "EXPERT_UNAVAILABLE"
    assert outcome.degraded is True

    stale = ExpertCoordinator(
        _providers(
            repo=lambda role, context: ExpertAdvisory(
                role="repo", status="succeeded", summary="old", input_revision=99
            )
        )
    ).consult(ExpertContext(task="fix code error", input_revision=1))
    assert stale.advisories[0].status == "failed"
    assert stale.advisories[0].error_code == "EXPERT_REVISION_MISMATCH"


def test_experts_cannot_escalate_permissions_or_decision_ownership() -> None:
    for update in (
        {"read_only": False},
        {"may_write": True},
        {"main_agent_decides": False},
    ):
        with pytest.raises(ValidationError):
            ExpertAdvisory(
                role="repo",
                status="succeeded",
                summary="x",
                input_revision=1,
                **update,
            )
    for update in ({"write_access": True}, {"main_agent_decides": False}):
        with pytest.raises(ValidationError):
            ExpertDecision(
                mode="experts",
                roles=["repo"],
                reason="x",
                budget=ExpertBudget(),
                input_revision=1,
                **update,
            )


def test_benchmark_reports_both_arms_without_faking_live_results() -> None:
    rows = [
        BenchmarkRow(
            case_id="simple",
            complex_task=False,
            single_success=True,
            expert_success=True,
            single_cost_micros=100,
            expert_cost_micros=400,
            single_latency_ms=10,
            expert_latency_ms=45,
        ),
        BenchmarkRow(
            case_id="complex",
            complex_task=True,
            single_success=False,
            expert_success=True,
            single_cost_micros=1000,
            expert_cost_micros=2500,
            single_latency_ms=100,
            expert_latency_ms=260,
        ),
    ]
    report = summarize_benchmark(rows)
    assert report.single_success_rate == 0.5
    assert report.expert_success_rate == 1.0
    assert report.simple_task_expert_overhead_micros == 300
    assert report.cost_ratio == pytest.approx(2.6364)
    assert report.latency_ratio == pytest.approx(2.7727)
    assert "pays off" in report.conclusion
    assert "V35-E02" in report.conclusion

    flat = summarize_benchmark(
        [
            BenchmarkRow(
                case_id="simple",
                complex_task=False,
                single_success=True,
                expert_success=True,
                single_cost_micros=100,
                expert_cost_micros=300,
                single_latency_ms=10,
                expert_latency_ms=30,
            )
        ]
    )
    assert "keep single-agent" in flat.conclusion

    with pytest.raises(ValueError):
        summarize_benchmark([])


class _Runtime:
    store = None
    model = None
    tools = None

    def system_prompt(self, spec: Any) -> str:
        return "system"

    def prior_report_text(self, spec: Any) -> str | None:
        return None


class _HookRuntime(_Runtime):
    def __init__(self, hook: Callable[[Any, str], Any]):
        self.hook = hook
        self.calls = 0

    def expert_advice(self, spec: Any, revision: str) -> Any:
        self.calls += 1
        return self.hook(spec, revision)


def test_repo_agent_defaults_to_single_agent_without_expert_hook() -> None:
    agent = RepoAnalysisAgent(_Runtime())
    assert agent._expert_advice(_analysis_spec(), "rev-1") is None


def test_repo_agent_compacts_and_caches_expert_advice_by_revision() -> None:
    def hook(spec: Any, revision: str) -> dict[str, Any]:
        return {
            "mode": "experts",
            "degraded": False,
            "advisories": [
                {
                    "role": f"role-{index}",
                    "status": "succeeded",
                    "summary": "s" * 1000,
                    "facts": [f"fact-{item}" for item in range(10)],
                    "verification_suggestions": [f"check-{item}" for item in range(10)],
                    "evidence_ids": [f"ev-{item}" for item in range(40)],
                }
                for index in range(5)
            ],
        }

    runtime = _HookRuntime(hook)
    agent = RepoAnalysisAgent(runtime)
    first = agent._expert_advice(_analysis_spec(), "rev-1")
    assert first is not None
    assert len(first["advisories"]) == 3
    assert all(len(item["summary"]) <= 600 for item in first["advisories"])
    assert all(len(item["facts"]) <= 4 for item in first["advisories"])
    assert all(len(item["evidence_ids"]) <= 16 for item in first["advisories"])
    assert all(item["read_only"] is True for item in first["advisories"])
    assert all(item["may_write"] is False for item in first["advisories"])
    assert first["main_agent_decides"] is True
    assert first["write_access"] is False

    agent._expert_advice(_analysis_spec(), "rev-1")
    assert runtime.calls == 1
    agent._expert_advice(_analysis_spec(), "rev-2")
    assert runtime.calls == 2


def test_repo_agent_degrades_when_expert_hook_fails_or_returns_garbage() -> None:
    def broken(spec: Any, revision: str) -> dict[str, Any]:
        raise RuntimeError("hook exploded")

    failed = RepoAnalysisAgent(_HookRuntime(broken))._expert_advice(
        _analysis_spec(), "rev-1"
    )
    assert failed is not None
    assert failed["degraded"] is True
    assert failed["stopped_reason"] == "EXPERT_FAILED"
    assert failed["write_access"] is False

    garbage = RepoAnalysisAgent(_HookRuntime(lambda spec, revision: "not-a-dict"))
    invalid = garbage._expert_advice(_analysis_spec(), "rev-1")
    assert invalid is not None
    assert invalid["degraded"] is True
    assert invalid["stopped_reason"] == "EXPERT_SCHEMA"
