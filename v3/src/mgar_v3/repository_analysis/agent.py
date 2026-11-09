"""LangGraph agent loop: Reason -> Tool Call -> Observation -> Reason."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Protocol, TypedDict
from uuid import UUID

from langgraph.graph import END, START, StateGraph
from pydantic import TypeAdapter

from mgar_v3.domain import DomainError
from mgar_v3.repository_analysis.domain import (
    AgentDecision,
    AnalysisSpec,
    RepoAnalysisReport,
    ToolCall,
    ToolObservation,
)
from mgar_v3.repository_analysis.report import (
    ReportVerification,
    insufficient_report,
    render_partial,
    render_report,
    verify_report,
)
from mgar_v3.repository_analysis.tools import TOOL_CATALOG, ToolOutcome

TOOL_ADAPTER = TypeAdapter(ToolCall)
MAX_OBSERVATION_CHARS = 6000
MAX_PRIOR_REPORT_CHARS = 8000
PARTIAL_CODES = {
    "BUDGET_EXHAUSTED",
    "INPUT_TOKEN_BUDGET",
    "DEADLINE_EXCEEDED",
    "MODEL_INPUT_LIMIT",
    "CONTEXT_LIMIT",
}


class AnalysisState(TypedDict, total=False):
    run_id: str
    epoch: int
    round: int
    decision_round: int
    history: list[dict[str, Any]]
    evidence: list[dict[str, Any]]
    pending_calls: list[dict[str, Any]]
    thought: str
    final_report: dict[str, Any] | None
    insufficient_reason: str | None
    stop_reason: str | None
    finished: bool


class AgentRuntime(Protocol):
    store: Any
    model: Any
    tools: Any

    def system_prompt(self, spec: AnalysisSpec) -> str: ...

    def prior_report_text(self, spec: AnalysisSpec) -> str | None: ...


def _tool_args_hash(call: Any) -> str:
    return hashlib.sha256(
        json.dumps(call.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def run_sync(value: Any) -> Any:
    """Execute a possibly-async tool call from a synchronous LangGraph node."""

    if not inspect.isawaitable(value):
        return value
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(value)
    # LangGraph nodes run inside the API event loop; the MCP session needs its own.
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, value).result()



class RepoAnalysisAgent:
    def __init__(self, runtime: AgentRuntime):
        self.runtime = runtime

    def _spec_epoch(self, state: AnalysisState) -> tuple[AnalysisSpec, int]:
        spec = self.runtime.store.spec(UUID(state["run_id"]))
        if spec.run_status != "running":
            raise DomainError("FENCED", "Repository analysis is not running", 409)
        return spec, spec.execution_epoch

    def _stop(self, state: AnalysisState, code: str) -> AnalysisState:
        return {**state, "stop_reason": code, "pending_calls": []}

    def reason(self, state: AnalysisState) -> AnalysisState:
        try:
            spec, epoch = self._spec_epoch(state)
        except DomainError as exc:
            return {**state, "stop_reason": exc.code, "finished": True}
        current_round = int(state.get("round", 1))
        if spec.model_calls_used >= spec.limits.max_model_calls:
            return self._stop(state, "BUDGET_EXHAUSTED")
        try:
            self.runtime.store.deadline_check(spec)
        except DomainError as exc:
            return self._stop(state, exc.code)
        system = self.runtime.system_prompt(spec)
        payload = self._payload(spec, state, current_round)
        encoded_chars = len((system + json.dumps(payload, ensure_ascii=False)).encode())
        if spec.input_tokens_used + (encoded_chars // 3) > spec.limits.max_input_tokens:
            return self._stop(state, "INPUT_TOKEN_BUDGET")
        try:
            decision = self.runtime.model.complete(
                self.runtime.store,
                UUID(state["run_id"]),
                epoch,
                f"reason:r{current_round}",
                spec.profile_hash,
                system,
                payload,
                AgentDecision,
                call_limit=spec.limits.max_model_calls,
                deadline_check=lambda: self.runtime.store.deadline_check(spec),
            )
        except DomainError as exc:
            if exc.code in PARTIAL_CODES:
                return self._stop(state, exc.code)
            return {**state, "stop_reason": exc.code, "finished": True}
        history = list(state.get("history", []))
        history.append(
            {
                "tool": "decision",
                "reference": "",
                "content": decision.thought,
                "evidence_id": None,
                "truncated": False,
            }
        )
        if decision.action == "tool":
            return {
                **state,
                "epoch": epoch,
                "decision_round": current_round,
                "round": current_round + 1,
                "pending_calls": [
                    call.model_dump(mode="json") for call in decision.tool_calls
                ],
                "history": history,
                "thought": decision.thought,
            }
        if decision.action == "final":
            return {
                **state,
                "epoch": epoch,
                "history": history,
                "final_report": decision.report.model_dump(mode="json")
                if decision.report
                else None,
                "pending_calls": [],
                "thought": decision.thought,
            }
        return {
            **state,
            "epoch": epoch,
            "history": history,
            "insufficient_reason": decision.insufficient_reason,
            "pending_calls": [],
            "thought": decision.thought,
        }

    def _payload(self, spec: AnalysisSpec, state: AnalysisState, round_no: int) -> dict[str, Any]:
        history = list(state.get("history", []))
        prior = self.runtime.prior_report_text(spec)
        budget_chars = max(4000, spec.limits.max_input_tokens * 3 - 4000)
        while history and len(json.dumps(history, ensure_ascii=False)) > budget_chars:
            history = history[1:]
        if prior and len(prior) > MAX_PRIOR_REPORT_CHARS:
            prior = prior[:MAX_PRIOR_REPORT_CHARS] + "\n…(prior report truncated)"
        return {
            "task": spec.task,
            "repository": spec.repository,
            "commit": spec.commit,
            "round": round_no,
            "remaining_model_calls": spec.limits.max_model_calls - spec.model_calls_used,
            "remaining_tool_calls": spec.limits.max_tool_calls - spec.tool_calls_used,
            "remaining_input_tokens": spec.limits.max_input_tokens - spec.input_tokens_used,
            "prior_report": prior,
            "prior_evidence_id_map": spec.prior_evidence_map if prior else {},
            "history": history,
        }

    def tools(self, state: AnalysisState) -> AnalysisState:
        try:
            spec, epoch = self._spec_epoch(state)
        except DomainError as exc:
            return self._stop(state, exc.code)
        history = list(state.get("history", []))
        evidence = list(state.get("evidence", []))
        decision_round = int(state.get("decision_round", 1))
        for index, raw in enumerate(state.get("pending_calls", [])):
            try:
                call = TOOL_ADAPTER.validate_python(raw)
            except ValueError:
                history.append(
                    {
                        "tool": "tool_error",
                        "reference": "",
                        "content": "Tool call failed strict schema validation and was not executed.",
                        "evidence_id": None,
                        "truncated": False,
                    }
                )
                continue
            step_key = f"tool:r{decision_round}:{index}"
            try:
                attempt = self.runtime.store.start_tool(
                    UUID(state["run_id"]), epoch, step_key, call.tool, _tool_args_hash(call)
                )
            except DomainError as exc:
                if exc.code in PARTIAL_CODES:
                    return self._stop({**state, "history": history}, exc.code)
                history.append(
                    {
                        "tool": "tool_error",
                        "reference": step_key,
                        "content": f"{exc.code}: the tool step was not retried.",
                        "evidence_id": None,
                        "truncated": False,
                    }
                )
                continue
            evidence_id = attempt.evidence_id
            reference = attempt.reference
            source_version = attempt.source_version
            if attempt.reused_result is not None:
                text = attempt.reused_result.decode("utf-8", "replace")
                truncated = False
            else:
                try:
                    outcome: ToolOutcome = run_sync(self.runtime.tools.execute(call))
                except DomainError as exc:
                    self.runtime.store.finish_tool(
                        attempt.call_id, epoch, outcome=None, error_code=exc.code
                    )
                    history.append(
                        {
                            "tool": call.tool,
                            "reference": step_key,
                            "content": (
                                f"{exc.code}: read-only tool failed; do not assume content. "
                                + str(exc)[:200]
                            ),
                            "evidence_id": None,
                            "truncated": False,
                        }
                    )
                    continue
                except Exception:
                    self.runtime.store.finish_tool(
                        attempt.call_id, epoch, outcome=None, error_code="TOOL_INTERNAL_ERROR"
                    )
                    history.append(
                        {
                            "tool": call.tool,
                            "reference": step_key,
                            "content": "TOOL_INTERNAL_ERROR: tool outcome was not recorded.",
                            "evidence_id": None,
                            "truncated": False,
                        }
                    )
                    continue
                evidence_id = self.runtime.store.finish_tool(
                    attempt.call_id, epoch, outcome=outcome
                )
                reference = outcome.reference
                source_version = outcome.source_version
                text = outcome.text
                truncated = False
                if len(text) > MAX_OBSERVATION_CHARS:
                    hint = "\n…(observation truncated)"
                    if call.tool == "read_file":
                        total = max(1, outcome.text.count("\n") + 1)
                        hint = (
                            f"\n…(observation truncated at {MAX_OBSERVATION_CHARS} characters; "
                            f"this file has {total} lines; call read_file with "
                            "start_line/end_line for the part you need)"
                        )
                    text = text[:MAX_OBSERVATION_CHARS] + hint
                    truncated = True
            item = {
                "tool": call.tool,
                "reference": reference,
                "content": text,
                "evidence_id": str(evidence_id) if evidence_id else None,
                "source_version": source_version,
                "truncated": truncated,
            }
            history.append(item)
            if evidence_id is not None:
                evidence.append(item)
        return {**state, "history": history, "evidence": evidence, "pending_calls": []}

    @staticmethod
    def route(state: AnalysisState) -> str:
        if state.get("stop_reason"):
            return "partial" if state["stop_reason"] in PARTIAL_CODES else "failure"
        if state.get("final_report") is not None:
            return "final"
        if state.get("insufficient_reason"):
            return "insufficient"
        return "tools" if state.get("pending_calls") else "reason"

    def final(self, state: AnalysisState) -> AnalysisState:
        spec, epoch = self._spec_epoch(state)
        report = RepoAnalysisReport.model_validate(state["final_report"])
        verification = verify_report(
            report,
            self.runtime.store.evidence_rows(spec.run_id),
            lambda content_hash: self.runtime.store.artifact_bytes(content_hash, spec.run_id),
        )
        rendered = render_report(spec, report, verification)
        self.runtime.store.finish_success(spec.run_id, epoch, rendered, verification)
        return {**state, "finished": True}

    def insufficient(self, state: AnalysisState) -> AnalysisState:
        spec, epoch = self._spec_epoch(state)
        reason = state.get("insufficient_reason") or "模型声明现有证据不足。"
        report = insufficient_report(
            reason,
            "模型没有形成可验证的仓库整体结构判断。",
            ["没有找到可引用的源码证据支持最终结论"],
        )
        verification = ReportVerification(
            passed=True, checks=["insufficient_declared"], failures=[], bindings=[]
        )
        rendered = render_report(spec, report, verification)
        self.runtime.store.finish_success(spec.run_id, epoch, rendered, verification)
        return {**state, "finished": True}

    def partial(self, state: AnalysisState) -> AnalysisState:
        spec, epoch = self._spec_epoch(state)
        code = state.get("stop_reason") or "BUDGET_EXHAUSTED"
        observations = [
            ToolObservation(
                tool=str(item["tool"]),
                reference=str(item.get("reference") or ""),
                content=str(item.get("content") or "")[:300],
                evidence_id=UUID(item["evidence_id"])
                if item.get("evidence_id")
                else None,
                source_version=item.get("source_version"),
                truncated=bool(item.get("truncated")),
            )
            for item in state.get("evidence", [])
        ]
        detail = (
            "模型轮数、工具次数、输入 Token、费用或执行期限已达到上限"
            if code in {"BUDGET_EXHAUSTED", "INPUT_TOKEN_BUDGET", "DEADLINE_EXCEEDED"}
            else "请求超出了模型上下文或输入限制"
        )
        from mgar_v3.repository_analysis.report import partial_from_evidence

        partial = partial_from_evidence(code, observations, detail=detail)
        self.runtime.store.finish_partial(spec.run_id, epoch, render_partial(spec, partial), code)
        return {**state, "finished": True}

    def failure(self, state: AnalysisState) -> AnalysisState:
        try:
            spec = self.runtime.store.spec(UUID(state["run_id"]))
            self.runtime.store.fail(spec.run_id, state.get("stop_reason") or "AGENT_FAILED")
        except DomainError:
            pass
        return {**state, "finished": True}

    def build(self, checkpointer: Any = None):
        graph = StateGraph(AnalysisState)
        graph.add_node("reason", self.reason)
        graph.add_node("tools", self.tools)
        graph.add_node("final", self.final)
        graph.add_node("insufficient", self.insufficient)
        graph.add_node("partial", self.partial)
        graph.add_node("failure", self.failure)
        graph.add_edge(START, "reason")
        graph.add_conditional_edges(
            "reason",
            self.route,
            {
                "tools": "tools",
                "final": "final",
                "insufficient": "insufficient",
                "partial": "partial",
                "failure": "failure",
            },
        )
        graph.add_conditional_edges(
            "tools",
            self.route,
            {
                "reason": "reason",
                "final": "final",
                "insufficient": "insufficient",
                "partial": "partial",
                "failure": "failure",
            },
        )
        graph.add_edge("final", END)
        graph.add_edge("insufficient", END)
        graph.add_edge("partial", END)
        graph.add_edge("failure", END)
        return graph.compile(checkpointer=checkpointer)


def initial_state(spec: AnalysisSpec) -> AnalysisState:
    return {
        "run_id": str(spec.run_id),
        "epoch": spec.execution_epoch,
        "round": 1,
        "decision_round": 1,
        "history": [],
        "evidence": [],
        "pending_calls": [],
        "thought": "",
        "final_report": None,
        "insufficient_reason": None,
        "stop_reason": None,
        "finished": False,
    }
