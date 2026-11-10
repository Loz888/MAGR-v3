"""V3.5 X01: on-demand, read-only expert routing.

The default is one main agent.  Experts are consulted only when the
authoritative state shows a deterministic trigger (multiple changed files,
dependency pressure, repeated verification failure, security findings,
conflicting evidence or an explicit user request).  Expert output is advisory:
it never carries write permission, never mutates the main state and never
replaces the main agent's final judgement.

This module is deliberately independent of the LangGraph runner and of the
repair orchestrator so both the R4 expert graph and the V3.5 main agents can
share the same routing and budget policy.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor, wait
from typing import Any, Literal, Protocol

from pydantic import Field

from mgar_v3.domain import Model

ExpertRole = Literal["knowledge", "repo", "data"]
ExpertMode = Literal["single_agent", "experts"]
ExpertStatus = Literal[
    "succeeded",
    "empty",
    "failed",
    "timeout",
    "conflict",
    "invalid_evidence",
]
SignalKind = Literal[
    "multi_file",
    "dependency",
    "verification_failure",
    "security",
    "conflict",
    "user_request",
    "knowledge",
    "data",
    "repo",
    "cross_source",
]
_ROLE_ORDER: tuple[ExpertRole, ...] = ("repo", "knowledge", "data")


class ExpertSignal(Model):
    kind: SignalKind
    detail: str
    evidence_ids: list[str] = Field(default_factory=list)


class ExpertContext(Model):
    """The authoritative projection used to decide whether experts are needed."""

    task: str
    question: str = ""
    changed_files: list[str] = Field(default_factory=list)
    dependency_edges: int = Field(default=0, ge=0)
    failed_tests: list[str] = Field(default_factory=list)
    failed_attempts: int = Field(default=0, ge=0)
    security_findings: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    user_requested_experts: bool = False
    attempted_rounds: int = Field(default=0, ge=0)
    input_revision: int = Field(default=1, ge=1)
    evidence_ids: list[str] = Field(default_factory=list)


class ExpertBudget(Model):
    max_experts: int = Field(default=3, ge=1, le=3)
    max_rounds: int = Field(default=2, ge=1, le=3)
    max_calls_per_revision: int = Field(default=3, ge=1, le=3)
    timeout_seconds: float = Field(default=5.0, gt=0, le=60)


class ExpertDecision(Model):
    mode: ExpertMode
    roles: list[ExpertRole] = Field(default_factory=list)
    signals: list[ExpertSignal] = Field(default_factory=list)
    reason: str
    stopped_reason: str | None = None
    budget: ExpertBudget
    input_revision: int
    main_agent_decides: Literal[True] = True
    write_access: Literal[False] = False


class ExpertAdvisory(Model):
    """A read-only expert diagnosis; the main agent decides what to do."""

    role: ExpertRole
    status: ExpertStatus
    summary: str = Field(default="", max_length=4000)
    facts: list[str] = Field(default_factory=list, max_length=16)
    verification_suggestions: list[str] = Field(default_factory=list, max_length=8)
    evidence_ids: list[str] = Field(default_factory=list, max_length=32)
    open_questions: list[str] = Field(default_factory=list, max_length=8)
    error_code: str | None = None
    input_revision: int
    cost_micros: int = Field(default=0, ge=0)
    read_only: Literal[True] = True
    may_write: Literal[False] = False
    main_agent_decides: Literal[True] = True


class ExpertOutcome(Model):
    decision: ExpertDecision
    advisories: list[ExpertAdvisory] = Field(default_factory=list)
    expert_calls: int = Field(default=0, ge=0)
    duplicates_skipped: list[ExpertRole] = Field(default_factory=list)
    elapsed_ms: float = Field(default=0.0, ge=0)
    cost_micros: int = Field(default=0, ge=0)
    degraded: bool = False
    input_revision: int
    main_state_mutated: Literal[False] = False
    write_access: Literal[False] = False
    main_agent_decides: Literal[True] = True

    def to_payload(self) -> dict[str, Any]:
        return {
            "mode": self.decision.mode,
            "roles": list(self.decision.roles),
            "reason": self.decision.reason,
            "stopped_reason": self.decision.stopped_reason,
            "signals": [item.kind for item in self.decision.signals],
            "advisories": [
                {
                    "role": item.role,
                    "status": item.status,
                    "summary": item.summary,
                    "facts": item.facts,
                    "verification_suggestions": item.verification_suggestions,
                    "evidence_ids": item.evidence_ids,
                    "open_questions": item.open_questions,
                    "error_code": item.error_code,
                    "read_only": item.read_only,
                    "may_write": item.may_write,
                }
                for item in self.advisories
            ],
            "expert_calls": self.expert_calls,
            "duplicates_skipped": list(self.duplicates_skipped),
            "degraded": self.degraded,
            "elapsed_ms": self.elapsed_ms,
            "cost_micros": self.cost_micros,
            "input_revision": self.input_revision,
            "main_agent_decides": self.main_agent_decides,
            "write_access": self.write_access,
        }


class ExpertProvider(Protocol):
    def investigate(
        self,
        role: ExpertRole,
        context: ExpertContext,
        budget_seconds: float,
    ) -> ExpertAdvisory: ...


def _text_hits(text: str, words: tuple[str, ...]) -> bool:
    return any(word in text for word in words)


class ExpertRouter:
    """Deterministic trigger evaluation; no model is called to make the decision."""

    def __init__(self, budget: ExpertBudget | None = None):
        self.budget = budget or ExpertBudget()

    def route(
        self, context: ExpertContext, budget: ExpertBudget | None = None
    ) -> ExpertDecision:
        selected = budget or self.budget
        signals: list[ExpertSignal] = []
        files = set(context.changed_files)
        if len(files) >= 2:
            signals.append(
                ExpertSignal(
                    kind="multi_file",
                    detail=f"{len(files)} files changed in one task",
                    evidence_ids=list(context.evidence_ids),
                )
            )
        if context.dependency_edges >= 2:
            signals.append(
                ExpertSignal(
                    kind="dependency",
                    detail=f"{context.dependency_edges} dependency edges touched",
                )
            )
        failed_files = {
            value.split("::", 1)[0].split("#", 1)[0]
            for value in context.failed_tests
            if value
        }
        if context.failed_tests and (
            len(failed_files) >= 2 or context.failed_attempts >= 2
        ):
            signals.append(
                ExpertSignal(
                    kind="verification_failure",
                    detail=f"{len(failed_files)} test files, "
                    f"{context.failed_attempts} failed attempts",
                )
            )
        if context.security_findings:
            signals.append(
                ExpertSignal(
                    kind="security",
                    detail=", ".join(context.security_findings[:4]),
                )
            )
        if context.conflicts:
            signals.append(
                ExpertSignal(kind="conflict", detail=", ".join(context.conflicts[:4]))
            )
        if context.user_requested_experts:
            signals.append(
                ExpertSignal(kind="user_request", detail="user asked for expert help")
            )
        text = f"{context.task} {context.question}".lower()
        knowledge_hit = _text_hits(
            text,
            (
                "documented",
                "document",
                "docs",
                "knowledge",
                "runbook",
                "policy",
                "faq",
                "文档",
                "知识",
                "规范",
                "口径",
            ),
        )
        data_hit = _text_hits(
            text,
            (
                "count",
                "sum",
                "amount",
                "metric",
                "statistic",
                "orders",
                "sql",
                "统计",
                "订单",
                "指标",
                "数据",
            ),
        )
        repo_hit = _text_hits(
            text,
            (
                "code",
                "repository",
                "repo",
                "duplicate",
                "stack",
                "exception",
                "错误",
                "重复",
                "代码",
                "仓库",
            ),
        )
        if knowledge_hit:
            signals.append(ExpertSignal(kind="knowledge", detail="documented context"))
        if data_hit:
            signals.append(ExpertSignal(kind="data", detail="data/metric question"))
        if repo_hit:
            signals.append(ExpertSignal(kind="repo", detail="repository evidence"))

        roles: set[ExpertRole] = set()
        if any(
            item.kind
            in {"multi_file", "dependency", "verification_failure", "security"}
            for item in signals
        ):
            roles.add("repo")
        if any(item.kind in {"security", "conflict"} for item in signals):
            roles.add("knowledge")
        if knowledge_hit:
            roles.add("knowledge")
        if data_hit:
            roles.add("data")
        if repo_hit:
            roles.add("repo")
        if knowledge_hit and data_hit:
            roles.add("repo")
            signals.append(
                ExpertSignal(
                    kind="cross_source",
                    detail="documented policy and data metrics must be reconciled",
                )
            )
        if context.user_requested_experts:
            roles |= {"knowledge", "repo", "data"}

        if not roles:
            return ExpertDecision(
                mode="single_agent",
                roles=[],
                signals=signals,
                reason="no deterministic expert trigger; the main agent handles it",
                budget=selected,
                input_revision=context.input_revision,
            )
        if context.attempted_rounds >= selected.max_rounds:
            return ExpertDecision(
                mode="single_agent",
                roles=[],
                signals=signals,
                reason="expert round budget is exhausted; return to the main agent",
                stopped_reason="ROUND_BUDGET",
                budget=selected,
                input_revision=context.input_revision,
            )
        ordered = [role for role in _ROLE_ORDER if role in roles]
        stopped: str | None = None
        if len(ordered) > selected.max_experts:
            ordered = ordered[: selected.max_experts]
            stopped = "EXPERT_BUDGET"
        return ExpertDecision(
            mode="experts",
            roles=ordered,
            signals=signals,
            reason="deterministic triggers: "
            + ", ".join(item.kind for item in signals),
            stopped_reason=stopped,
            budget=selected,
            input_revision=context.input_revision,
        )


class ExpertCoordinator:
    """Dispatch selected read-only experts with dedupe, caps and degradation."""

    def __init__(
        self,
        providers: Mapping[ExpertRole, ExpertProvider],
        *,
        budget: ExpertBudget | None = None,
        router: ExpertRouter | None = None,
        clock: Callable[[], float] = time.perf_counter,
    ):
        self.providers = dict(providers)
        self.budget = budget or ExpertBudget()
        self.router = router or ExpertRouter(self.budget)
        self.clock = clock
        self._completed: dict[tuple[ExpertRole, int, str], ExpertAdvisory] = {}
        self._calls: dict[int, int] = {}

    def _advisory(
        self,
        role: ExpertRole,
        context: ExpertContext,
        *,
        status: ExpertStatus,
        error_code: str | None = None,
        summary: str = "",
    ) -> ExpertAdvisory:
        return ExpertAdvisory(
            role=role,
            status=status,
            summary=summary,
            error_code=error_code,
            open_questions=[f"{role}: {error_code or status}"],
            input_revision=context.input_revision,
        )

    def _validate(
        self, advisory: ExpertAdvisory, context: ExpertContext
    ) -> ExpertAdvisory:
        if advisory.input_revision != context.input_revision:
            return self._advisory(
                advisory.role,
                context,
                status="failed",
                error_code="EXPERT_REVISION_MISMATCH",
            )
        if advisory.status != "succeeded":
            return advisory
        if not advisory.summary.strip() and not advisory.facts:
            return self._advisory(
                advisory.role, context, status="empty", error_code="EXPERT_EMPTY"
            )
        if advisory.evidence_ids and any(
            value not in context.evidence_ids for value in advisory.evidence_ids
        ):
            return advisory.model_copy(
                update={
                    "status": "invalid_evidence",
                    "facts": [],
                    "open_questions": [
                        *advisory.open_questions,
                        f"{advisory.role}: cited evidence outside the input",
                    ],
                }
            )
        return advisory

    def _investigate(
        self, provider: ExpertProvider, role: ExpertRole, context: ExpertContext
    ) -> ExpertAdvisory:
        try:
            return provider.investigate(
                role, context, budget_seconds=self.budget.timeout_seconds
            )
        except Exception:
            return self._advisory(
                role, context, status="failed", error_code="EXPERT_FAILED"
            )

    def consult(
        self,
        context: ExpertContext,
        *,
        main_state: Any = None,
        budget: ExpertBudget | None = None,
    ) -> ExpertOutcome:
        started = self.clock()
        decision = self.router.route(context, budget)
        if decision.mode == "single_agent":
            return ExpertOutcome(
                decision=decision,
                elapsed_ms=round(max(0.0, self.clock() - started) * 1000, 3),
                input_revision=context.input_revision,
            )
        used = self._calls.get(context.input_revision, 0)
        remaining = max(0, decision.budget.max_calls_per_revision - used)
        key_question = context.question or context.task
        cached_roles: dict[ExpertRole, ExpertAdvisory] = {}
        pending: list[ExpertRole] = []
        for role in decision.roles:
            cached = self._completed.get((role, context.input_revision, key_question))
            if cached is not None:
                cached_roles[role] = cached
            else:
                pending.append(role)
        # A cached advisory is a dedupe hit, not a new model call: it must not
        # consume the per-revision call budget and must remain available after
        # the budget is exhausted.
        allowed = pending[:remaining]
        if not allowed and not cached_roles:
            stopped = decision.model_copy(
                update={
                    "mode": "single_agent",
                    "roles": [],
                    "stopped_reason": "CALL_BUDGET",
                    "reason": "expert call budget is exhausted for this revision",
                }
            )
            return ExpertOutcome(
                decision=stopped,
                elapsed_ms=round(max(0.0, self.clock() - started) * 1000, 3),
                input_revision=context.input_revision,
            )
        if len(allowed) < len(pending):
            served = [role for role in decision.roles if role in cached_roles or role in allowed]
            decision = decision.model_copy(
                update={"roles": served, "stopped_reason": "CALL_BUDGET"}
            )
        self._calls[context.input_revision] = used + len(allowed)

        advisories: list[ExpertAdvisory] = list(cached_roles.values())
        duplicates: list[ExpertRole] = list(cached_roles)
        expert_calls = 0
        if allowed:
            pool = ThreadPoolExecutor(max_workers=len(allowed))
            futures = {
                pool.submit(self._investigate, self.providers[role], role, context): role
                for role in allowed
                if role in self.providers
            }
            missing = [role for role in allowed if role not in self.providers]
            for role in missing:
                advisories.append(
                    self._advisory(
                        role,
                        context,
                        status="failed",
                        error_code="EXPERT_UNAVAILABLE",
                    )
                )
            done, not_done = wait(futures, timeout=decision.budget.timeout_seconds)
            for future in done:
                role = futures[future]
                try:
                    advisory = future.result()
                except Exception:
                    advisory = self._advisory(
                        role, context, status="failed", error_code="EXPERT_FAILED"
                    )
                advisories.append(self._validate(advisory, context))
            for future in not_done:
                role = futures[future]
                future.cancel()
                advisories.append(
                    self._advisory(
                        role, context, status="timeout", error_code="EXPERT_TIMEOUT"
                    )
                )
            pool.shutdown(wait=False)
            expert_calls = len(futures)
            for advisory in advisories:
                if advisory.role in allowed:
                    self._completed[
                        (advisory.role, context.input_revision, key_question)
                    ] = advisory

        ordered = {item.role: item for item in advisories}
        final = [ordered[role] for role in decision.roles if role in ordered]
        degraded = any(item.status != "succeeded" for item in final)
        return ExpertOutcome(
            decision=decision,
            advisories=final,
            expert_calls=expert_calls,
            duplicates_skipped=duplicates,
            elapsed_ms=round(max(0.0, self.clock() - started) * 1000, 3),
            cost_micros=sum(item.cost_micros for item in final),
            degraded=degraded,
            input_revision=context.input_revision,
        )


class BenchmarkRow(Model):
    case_id: str
    complex_task: bool
    single_success: bool
    expert_success: bool
    single_cost_micros: int = Field(ge=0)
    expert_cost_micros: int = Field(ge=0)
    single_latency_ms: float = Field(ge=0)
    expert_latency_ms: float = Field(ge=0)


class BenchmarkReport(Model):
    rows: list[BenchmarkRow]
    single_success_rate: float
    expert_success_rate: float
    cost_ratio: float
    latency_ratio: float
    simple_task_expert_overhead_micros: int
    conclusion: str


def summarize_benchmark(rows: list[BenchmarkRow]) -> BenchmarkReport:
    """Compare recorded single-agent and on-demand-expert arms honestly.

    This consumes recorded measurements; it does not pretend to be a live model
    experiment.  V35-E02 owns the live ablation.
    """

    if not rows:
        raise ValueError("at least one benchmark row is required")
    total = len(rows)
    single_cost = sum(row.single_cost_micros for row in rows)
    expert_cost = sum(row.expert_cost_micros for row in rows)
    single_latency = sum(row.single_latency_ms for row in rows)
    expert_latency = sum(row.expert_latency_ms for row in rows)
    simple_overhead = sum(
        row.expert_cost_micros - row.single_cost_micros
        for row in rows
        if not row.complex_task
    )
    improved = any(
        row.complex_task and row.expert_success and not row.single_success
        for row in rows
    )
    conclusion = (
        "expert mode only pays off on complex tasks; on-demand routing avoids "
        "its cost on simple tasks. Live ablation is deferred to V35-E02."
        if improved
        else "recorded rows show no success gain from experts; keep single-agent "
        "unless a future live evaluation demonstrates a benefit."
    )
    return BenchmarkReport(
        rows=rows,
        single_success_rate=round(
            sum(row.single_success for row in rows) / total, 4
        ),
        expert_success_rate=round(
            sum(row.expert_success for row in rows) / total, 4
        ),
        cost_ratio=round(expert_cost / single_cost, 4) if single_cost else 0.0,
        latency_ratio=(
            round(expert_latency / single_latency, 4) if single_latency else 0.0
        ),
        simple_task_expert_overhead_micros=simple_overhead,
        conclusion=conclusion,
    )
