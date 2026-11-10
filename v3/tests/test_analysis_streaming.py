"""Automatic execution, provider streaming, verified completion and bounded failure policies."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import httpx
import pytest
from test_v32_repository_analysis import FakeStore, runtime, spec

from mgar_v3.domain import DomainError
from mgar_v3.repository_analysis.agent import RepoAnalysisAgent, initial_state
from mgar_v3.repository_analysis.domain import (
    AcceptanceDecision,
    AgentDecision,
    AnalysisLimits,
    AnalysisModelConfig,
)
from mgar_v3.repository_analysis.model import DeepSeekAnalysisModel
from mgar_v3.repository_analysis.service import RepositoryAnalysisService
from mgar_v3.repository_analysis.streaming import ReportPreview
from mgar_v3.repository_analysis.tools import ToolOutcome


def automatic():
    return AnalysisLimits(
        max_model_calls=None,
        max_tool_calls=None,
        max_input_tokens=500000,
        max_total_tokens=500000,
    )


def final_decision(ledger, *, bad=False, conclusion="The startup function returns ready."):
    evidence = next(iter(ledger.evidence))
    claim = {
        "statement": "The return value is ready.",
        "evidence_id": str(evidence),
        "quote": "made up source" if bad else "    return 'ready'",
    }
    return AgentDecision.model_validate(
        {
            "action": "final",
            "thought": "Enough source has been read.",
            "tool_calls": [],
            "report": {
                "conclusion": conclusion,
                "structure": "A Python startup module.",
                "key_files": [claim],
                "findings": [claim],
            },
        }
    )


class Files:
    def __init__(self):
        self.reads = []

    async def execute(self, call):
        self.reads.append(call.path)
        text = "def main():\n    return 'ready'\n"
        return ToolOutcome(
            tool="read_file",
            text=text,
            content=text.encode(),
            reference="github://octo/example@" + "a" * 40 + "/" + call.path,
            source_version="blob-sha",
            evidence_kind="file",
        )


def run(store, model, tools):
    graph = RepoAnalysisAgent(runtime(store, model, tools)).build()
    graph.invoke(
        initial_state(store.value),
        {
            "configurable": {"thread_id": str(store.value.run_id)},
            "recursion_limit": 200,
        },
    )


def test_progressing_analysis_can_exceed_the_previous_twelve_turn_cap():
    store, tools = FakeStore(spec(automatic())), Files()

    class Model:
        def complete(self, ledger, *args, **kwargs):
            ledger.value = ledger.value.model_copy(
                update={
                    "model_calls_used": ledger.value.model_calls_used + 1,
                }
            )
            schema = args[-1]
            if schema is AcceptanceDecision:
                return AcceptanceDecision(accepted=True, summary="The question is fully answered.")
            if len(tools.reads) < 15:
                return AgentDecision(
                    action="tool",
                    thought="Read the next relevant source file.",
                    tool_calls=[{"tool": "read_file", "path": f"src/file{len(tools.reads)}.py"}],
                )
            return final_decision(ledger)

    run(store, Model(), tools)
    assert len(tools.reads) == 15
    assert store.value.model_calls_used > 12
    assert store.success is not None and store.success[1].passed
    assert "task_question_coverage" in store.success[1].checks
    assert store.partial is None


def test_repeated_reads_reuse_evidence_and_stop_without_false_success():
    store, tools = FakeStore(spec(automatic())), Files()

    class Model:
        calls = 0

        def complete(self, *args, **kwargs):
            self.calls += 1
            return AgentDecision(
                action="tool",
                thought="Read the same source again.",
                tool_calls=[{"tool": "read_file", "path": "src/main.py"}],
            )

    model = Model()
    run(store, model, tools)
    assert tools.reads == ["src/main.py"]
    assert model.calls < 10
    assert store.partial[1] == "AGENT_STALLED"
    assert store.success is None


def test_bad_citations_and_incomplete_answers_feed_back_into_investigation():
    store, tools = FakeStore(spec(automatic())), Files()

    class Model:
        drafts = 0
        checks = 0
        payloads = []

        def complete(self, ledger, *args, **kwargs):
            payload, schema = args[-2:]
            self.payloads.append(payload)
            if schema is AcceptanceDecision:
                self.checks += 1
                return AcceptanceDecision(
                    accepted=self.checks > 1,
                    missing_requirements=["Explain the returned value"] if self.checks == 1 else [],
                    summary="Coverage checked.",
                )
            if not tools.reads:
                return AgentDecision(
                    action="tool",
                    thought="Read startup.",
                    tool_calls=[{"tool": "read_file", "path": "src/main.py"}],
                )
            self.drafts += 1
            return final_decision(ledger, bad=self.drafts == 1)

    model = Model()
    run(store, model, tools)
    assert model.drafts == 3
    assert model.checks == 2
    assert store.success is not None and store.success[1].passed
    feedback = [
        item["content"]
        for payload in model.payloads
        for item in payload.get("history", [])
        if item.get("tool") == "verification"
    ]
    assert any("source quotes" in text for text in feedback)
    assert any("Explain the returned value" in text for text in feedback)


def test_new_evidence_allows_more_than_three_acceptance_revisions():
    store, tools = FakeStore(spec(automatic())), Files()

    class Model:
        checks = 0

        def complete(self, ledger, *args, **kwargs):
            if args[-1] is AcceptanceDecision:
                self.checks += 1
                return AcceptanceDecision(
                    accepted=self.checks == 5,
                    missing_requirements=[] if self.checks == 5 else ["Inspect the next source"],
                    summary="Question coverage checked.",
                )
            if len(tools.reads) <= self.checks:
                return AgentDecision(
                    action="tool",
                    thought="Investigate the outstanding requirement.",
                    tool_calls=[{"tool": "read_file", "path": f"src/file{self.checks}.py"}],
                )
            return final_decision(ledger)

    model = Model()
    run(store, model, tools)
    assert model.checks == 5 and len(tools.reads) == 5
    assert store.success is not None and store.partial is None


def test_rejected_answer_without_new_investigation_stops_instead_of_looping():
    store, tools = FakeStore(spec(automatic())), Files()

    class Model:
        checks = 0

        def complete(self, ledger, *args, **kwargs):
            if args[-1] is AcceptanceDecision:
                self.checks += 1
                return AcceptanceDecision(
                    accepted=False,
                    missing_requirements=["Explain the missing branch"],
                    summary="Still incomplete.",
                )
            if not tools.reads:
                return AgentDecision(
                    action="tool",
                    thought="Read startup.",
                    tool_calls=[{"tool": "read_file", "path": "src/main.py"}],
                )
            return final_decision(ledger)

    model = Model()
    run(store, model, tools)
    assert model.checks == 3
    assert store.partial[1] == "EVIDENCE_REJECTED" and store.success is None


def test_different_empty_search_queries_do_not_count_as_new_information():
    store = FakeStore(spec(automatic()))

    class Search:
        calls = 0

        async def execute(self, call):
            self.calls += 1
            return ToolOutcome(
                tool="search_code",
                text=json.dumps({"query": call.query, "items": []}),
                content=b"{}",
                reference="search:" + call.query,
                source_version="a" * 40,
            )

    search = Search()

    class Model:
        def complete(self, *args, **kwargs):
            return AgentDecision(
                action="tool",
                thought="Try another query.",
                tool_calls=[{"tool": "search_code", "query": f"missing{search.calls}"}],
            )

    run(store, Model(), search)
    assert search.calls == 5
    assert store.partial[1] == "AGENT_STALLED" and store.success is None


def test_insufficient_answer_is_partial_in_automatic_mode():
    store = FakeStore(spec(automatic()))

    class Model:
        def complete(self, *args, **kwargs):
            return AgentDecision(
                action="insufficient",
                thought="The target is missing.",
                insufficient_reason="The target file could not be found.",
            )

    run(store, Model(), Files())
    assert store.success is None
    assert store.partial[1] == "INSUFFICIENT_EVIDENCE"


def test_initial_insufficiency_can_replan_and_finish_with_available_tokens():
    store, tools = FakeStore(spec(automatic())), Files()

    class Model:
        calls = 0

        def complete(self, ledger, *args, **kwargs):
            if args[-1] is AcceptanceDecision:
                return AcceptanceDecision(accepted=True, summary="The question is answered.")
            self.calls += 1
            if self.calls == 1:
                return AgentDecision(
                    action="insufficient",
                    thought="No source is known yet.",
                    insufficient_reason="Find the startup function.",
                )
            if not tools.reads:
                return AgentDecision(
                    action="tool",
                    thought="Try the startup module.",
                    tool_calls=[{"tool": "read_file", "path": "src/main.py"}],
                )
            return final_decision(ledger)

    run(store, Model(), tools)
    assert store.success is not None and store.partial is None


def test_total_token_exhaustion_cannot_be_reported_as_success():
    value = spec(automatic()).model_copy(
        update={
            "input_tokens_used": 499900,
            "output_tokens_used": 100,
        }
    )
    store = FakeStore(value)

    class Model:
        def complete(self, *args, **kwargs):
            raise AssertionError("The model must not be called with no remaining tokens")

    run(store, Model(), Files())
    assert store.success is None
    assert store.partial[1] == "TOKEN_BUDGET_EXHAUSTED"


def test_old_worker_cannot_borrow_the_new_execution_epoch():
    old = spec(automatic())
    store = FakeStore(old.model_copy(update={"execution_epoch": old.execution_epoch + 1}))

    class Model:
        def complete(self, *args, **kwargs):
            raise AssertionError("A stale worker cannot make a new model request")

    graph = RepoAnalysisAgent(runtime(store, Model(), Files())).build()
    graph.invoke(initial_state(old), {"configurable": {"thread_id": str(old.run_id)}})
    assert store.failed is None and store.success is None


def test_json_preview_handles_escaped_unicode_without_exposing_reasoning():
    text = json.dumps(
        {
            "thought": 'private {"report":{"conclusion":"DO NOT EXPOSE"}}',
            "report": {"conclusion": '中文😀\n"quoted"', "structure": "源码结构"},
        }
    )
    preview = ReportPreview()
    for char in text:
        preview.feed(char)
    assert preview.values == {"conclusion": '中文😀\n"quoted"', "structure": "源码结构"}
    assert "DO NOT EXPOSE" not in json.dumps(preview.values)


class Chunks(httpx.SyncByteStream):
    def __init__(self, frames, *, disconnect=False):
        self.frames = frames
        self.finished = False
        self.disconnect = disconnect

    def __iter__(self):
        for frame in self.frames:
            # Split through multibyte characters and SSE line boundaries.
            for offset in range(0, len(frame), 7):
                yield frame[offset : offset + 7]
        if self.disconnect:
            raise httpx.ReadError("Connection was lost before the usage receipt")
        self.finished = True


def frames(text, *, usage=True):
    def event(payload):
        return ("data: " + json.dumps(payload, ensure_ascii=False) + "\r\n\r\n").encode()

    result = [
        event(
            {
                "model": "deepseek-chat",
                "choices": [{"index": 0, "delta": {"reasoning_content": "PRIVATE REASONING"}}],
            }
        )
    ]
    for index in range(0, len(text), 25):
        result.append(
            event(
                {
                    "model": "deepseek-chat",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": text[index : index + 25]},
                            "finish_reason": None,
                        }
                    ],
                }
            )
        )
    result.append(
        event(
            {
                "model": "deepseek-chat",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            }
        )
    )
    if usage:
        result.append(
            event(
                {
                    "model": "deepseek-chat",
                    "choices": [],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 100},
                }
            )
        )
    result.append(b"data: [DONE]\n\n")
    return result


class Ledger:
    accounts_usage = True

    def __init__(self, source):
        self.source = source
        self.events = []
        self.settlements = []
        self.reservations = 0
        self.preview_before_end = False

    def repository_model_result(self, *args):
        return None

    def record_model_input(self, *args):
        pass

    def reserve_repository_call(self, *args, **kwargs):
        self.reservations += 1
        return uuid4()

    def record_model_response(self, *args):
        pass

    def settle_call(self, call_id, cost, *args):
        self.settlements.append(cost)

    def stream_event(self, run_id, epoch, kind, **payload):
        self.events.append((kind, payload))
        if kind == "answer_delta" and not self.source.finished:
            self.preview_before_end = True


def complete(settings, transport, ledger):
    with httpx.Client(transport=transport) as client:
        model = DeepSeekAnalysisModel(
            settings,
            AnalysisModelConfig(),
            client=client,
            rate_limit=lambda *args: None,
        )
        return model.complete(
            ledger,
            uuid4(),
            1,
            "reason:r1",
            "profile",
            "system",
            {"task": "Explain startup"},
            AgentDecision,
            call_limit=None,
            deadline_check=lambda: None,
        )


def completion_text():
    claim = {"statement": "返回 ready", "evidence_id": str(uuid4()), "quote": "return 'ready'"}
    return json.dumps(
        {
            "action": "final",
            "thought": "INTERNAL THOUGHT",
            "report": {
                "conclusion": "启动函数返回 ready。",
                "structure": "一个 Python 模块。",
                "key_files": [claim],
                "findings": [claim],
            },
        },
        ensure_ascii=False,
    )


def test_real_provider_stream_yields_public_preview_before_completion(settings):
    from pydantic import SecretStr

    settings = settings.model_copy(update={"deepseek_api_key": SecretStr("controlled-key")})
    source = Chunks(frames(completion_text()))
    ledger = Ledger(source)

    def provider(request):
        body = json.loads(request.content)
        assert body["stream"] is True and body["stream_options"]["include_usage"] is True
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=source)

    result = complete(settings, httpx.MockTransport(provider), ledger)
    assert result.action == "final"
    assert ledger.preview_before_end
    assert ledger.settlements[0] > 0
    public = "".join(payload["text"] for kind, payload in ledger.events if kind == "answer_delta")
    assert "ready" in public
    assert "PRIVATE REASONING" not in public and "INTERNAL THOUGHT" not in public


@pytest.mark.parametrize("disconnect", [False, True])
def test_missing_stream_usage_is_unknown_and_never_automatically_retried(settings, disconnect):
    from pydantic import SecretStr

    settings = settings.model_copy(update={"deepseek_api_key": SecretStr("controlled-key")})
    source = Chunks(frames(completion_text(), usage=False), disconnect=disconnect)
    ledger = Ledger(source)
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=source,
        )
    )
    with pytest.raises(DomainError, match="usage receipt"):
        complete(settings, transport, ledger)
    assert ledger.reservations == 1
    assert ledger.settlements == [None]


def test_definitive_rate_limit_rejection_has_bounded_retry_without_unknown_usage(settings):
    from pydantic import SecretStr

    settings = settings.model_copy(update={"deepseek_api_key": SecretStr("controlled-key")})
    source = Chunks(frames(completion_text()))
    ledger = Ledger(source)
    attempts = []

    def provider(request):
        attempts.append(request)
        if len(attempts) == 1:
            return httpx.Response(429, json={"error": "rate limited"}, headers={"Retry-After": "0"})
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=source)

    assert complete(settings, httpx.MockTransport(provider), ledger).action == "final"
    assert len(attempts) == 2
    assert ledger.settlements[0] == 0
    assert all(cost is not None for cost in ledger.settlements)


def test_durable_sse_cursor_replay_and_epoch_fence(settings, engine):
    from fastapi.testclient import TestClient

    from mgar_v3.api import create_app
    from mgar_v3.repository_analysis.domain import CreateAnalysisInput

    service = RepositoryAnalysisService(engine, settings)
    value = service.create(
        CreateAnalysisInput(
            repository_url="https://github.com/octo/example",
            task="Explain startup",
            execution_mode="fixture",
            client_request_id="stream-replay",
        )
    )
    service.store.begin_run(value.run_id)
    epoch = service.store.spec(value.run_id).execution_epoch
    assert service.store.stream_event(
        value.run_id,
        epoch,
        "model_stream_started",
        phase="analysis",
    )
    assert service.store.stream_event(
        value.run_id,
        epoch,
        "answer_delta",
        field="conclusion",
        text="你好😀",
        offset=0,
    )
    with TestClient(
        create_app(settings, engine, run_background=False),
        base_url="http://127.0.0.1",
        headers={"X-MGAR-Token": settings.local_token.get_secret_value()},
    ) as client:
        endpoint = f"/api/v3/repository-analyses/{value.run_id}/events?follow=false"
        response = client.get(endpoint)
        assert response.status_code == 200
        assert "text/event-stream" in response.headers["content-type"]
        assert response.headers["x-accel-buffering"] == "no"
        assert "你好😀" in response.text
        ids = [int(line[4:]) for line in response.text.splitlines() if line.startswith("id: ")]
        assert ids == sorted(set(ids))
        resumed = client.get(endpoint, headers={"Last-Event-ID": str(ids[-1])})
        assert "answer_delta" not in resumed.text
        assert "event: done" in resumed.text
        assert client.get(endpoint, headers={"Last-Event-ID": "-1"}).status_code == 400
        assert client.get(endpoint, headers={"X-MGAR-Token": "bad"}).status_code == 401
    service.pause(value.run_id)
    assert not service.store.stream_event(
        value.run_id,
        epoch,
        "answer_delta",
        field="conclusion",
        text="STALE",
        offset=0,
    )
    service.resume(value.run_id)
    service.store.begin_run(value.run_id)
    current = service.store.spec(value.run_id).execution_epoch
    assert current > epoch
    assert not service.store.stream_event(
        value.run_id,
        epoch,
        "answer_delta",
        field="conclusion",
        text="STALE",
        offset=0,
    )
    assert service.store.stream_event(
        value.run_id,
        current,
        "answer_delta",
        field="conclusion",
        text="当前版本",
        offset=0,
    )
    assert "STALE" not in json.dumps(service.detail(value.run_id), ensure_ascii=False)
    assert service.detail(value.run_id)["draft"]["conclusion"] == "当前版本"


def automatic_run(settings, engine, *, tokens=500000):
    from mgar_v3.repository_analysis.domain import CreateAnalysisInput

    service = RepositoryAnalysisService(engine, settings)
    value = service.create(
        CreateAnalysisInput(
            repository_url="https://github.com/octo/example",
            task="Explain startup",
            execution_mode="fixture",
            client_request_id=str(uuid4()),
            limits=AnalysisLimits(
                max_model_calls=None,
                max_tool_calls=None,
                max_input_tokens=tokens,
                max_total_tokens=tokens,
            ),
        )
    )
    service.store.begin_run(value.run_id)
    return service, service.store.spec(value.run_id)


def test_postgres_automatic_ledger_exceeds_old_hundred_call_schema_limit(settings, engine):
    service, value = automatic_run(settings, engine)
    for index in range(105):
        call = service.store.reserve_repository_call(
            value.run_id,
            value.execution_epoch,
            f"reason:{index}",
            value.profile_hash,
            1,
            None,
        )
        service.store.settle_call(call, 0, {"prompt_tokens": 1, "completion_tokens": 1})
    current = service.store.spec(value.run_id)
    assert current.model_calls_used == 105
    assert current.input_tokens_used + current.output_tokens_used == 210
    assert current.reserved_tokens == 0
    with engine.transaction() as conn:
        row = conn.execute(
            "SELECT used_calls,call_limit FROM app.authorization_budgets WHERE profile_hash=%s",
            (value.profile_hash,),
        ).fetchone()
    assert row["used_calls"] == 105 and row["call_limit"] is None


def test_checkpoint_resume_rebases_epoch_and_keeps_completed_tool_results(settings, engine):
    from mgar_v3.graph.checkpoints import checkpoint_saver

    service, value = automatic_run(settings, engine)
    config = {"configurable": {"thread_id": str(value.run_id)}}
    with checkpoint_saver(settings.database_url.get_secret_value()) as saver:
        graph = RepoAnalysisAgent(service._runtime(value)).build(saver)
        graph.invoke(initial_state(value), config, interrupt_after=["tools"])
        snapshot = graph.get_state(config)
        assert snapshot.next == ("reason",)
        assert snapshot.values["epoch"] == value.execution_epoch
    before = service.detail(value.run_id)["tool_calls"]
    assert before and all(item["status"] == "succeeded" for item in before)
    service.pause(value.run_id)
    service.resume(value.run_id)
    service.execute(value.run_id)
    detail = service.detail(value.run_id)
    assert detail["run"]["run_status"] == "succeeded"
    assert detail["run"]["execution_epoch"] > value.execution_epoch
    assert all(item in detail["tool_calls"] for item in before)
    assert len({item["step_key"] for item in detail["tool_calls"]}) == len(detail["tool_calls"])


def test_token_reservations_are_atomic_and_unknown_receipts_keep_capacity(settings, engine):
    service, value = automatic_run(settings, engine, tokens=4096)

    def reserve(index):
        try:
            return service.store.reserve_repository_call(
                value.run_id,
                value.execution_epoch,
                f"parallel:{index}",
                value.profile_hash,
                1,
                None,
                token_reservation=3000,
            )
        except DomainError as exc:
            assert exc.code == "TOKEN_BUDGET_EXHAUSTED"
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        calls = list(pool.map(reserve, range(2)))
    known = [call for call in calls if call is not None]
    assert len(known) == 1
    assert service.store.spec(value.run_id).reserved_tokens == 3000
    # A receipt that exceeds the cost bound is unknown, even when a number was supplied.
    service.store.settle_call(known[0], 2, {"prompt_tokens": 1, "completion_tokens": 1})
    assert service.store.spec(value.run_id).reserved_tokens == 3000
    assert reserve(3) is None
    # Explicit reconciliation releases the reservation exactly once.
    usage = {"prompt_tokens": 100, "completion_tokens": 100}
    service.store.settle_call(known[0], 0, usage)
    service.store.settle_call(known[0], 0, usage)
    current = service.store.spec(value.run_id)
    assert current.reserved_tokens == 0
    assert current.input_tokens_used + current.output_tokens_used == 200
