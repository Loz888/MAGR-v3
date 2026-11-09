"""V3.3 M04: context builder, compaction and pagination invariants."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from mgar_v3.context.builder import (
    ContextBuilder,
    ContextRequest,
    EvidenceRef,
    MemoryNote,
    RemainingBudget,
    request_invariants,
)
from mgar_v3.context.compactor import (
    ArtifactPaginator,
    LineRange,
    Observation,
    PermissionSnapshot,
    compress_observation,
    diff_invariants,
    validate_compaction,
    verify_artifacts,
)
from mgar_v3.domain import DomainError
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.repository_analysis.agent import RepoAnalysisAgent
from mgar_v3.repository_analysis.domain import (
    AnalysisLimits,
    AnalysisModelConfig,
    AnalysisSpec,
)

COMMIT = "a" * 40
SYSTEM = "SYSTEM RULES: read the fixed commit only; never write."


def _permissions(
    *, tools: list[str] | None = None, budgets: dict[str, int] | None = None
) -> PermissionSnapshot:
    return PermissionSnapshot(
        project_id="orders-demo",
        repository="example/mgar-test",
        commit=COMMIT,
        actor="owner",
        read_only=True,
        allowed_tools=tools if tools is not None else ["read_file", "search_code"],
        budgets=budgets if budgets is not None else {"max_input_tokens": 60000},
    )


def _observation(index: int, *, size: int = 400, tool: str = "read_file") -> Observation:
    return Observation(
        tool=tool,
        reference=f"github://example/mgar-test@{COMMIT}/src/file_{index}.py",
        content=f"line {index}: " + ("x" * size),
        evidence_id=str(UUID(int=index + 1)),
        source_version=f"blob-{index}",
        locator=LineRange(start_line=index * 10 + 1, end_line=index * 10 + 10),
    )


def _request(**overrides: object) -> ContextRequest:
    values: dict[str, object] = {
        "system_instructions": SYSTEM,
        "task": "trace where the duplicate counter is incremented",
        "repository": "example/mgar-test",
        "commit": COMMIT,
        "permissions": _permissions(),
        "remaining_budget": RemainingBudget(
            model_calls=4, tool_calls=12, input_tokens=50000
        ),
    }
    values.update(overrides)
    return ContextRequest(**values)


def test_bundle_carries_every_fixed_section_and_recent_window() -> None:
    observations = [_observation(index, size=1200) for index in range(12)]
    request = _request(
        constraints=["read-only", "stay inside example/mgar-test"],
        current_plan=["read the counter module", "trace its callers"],
        observations=observations,
        evidence=[
            EvidenceRef(
                evidence_id=str(UUID(int=500)),
                source_uri="github://example/mgar-test/src/counter.py",
                artifact_hash=None,
            )
        ],
        memories=[
            MemoryNote(
                memory_id=str(uuid4()),
                subject_key="counter.py increments twice",
                version_status="historical",
                requires_reverification=True,
                confidence=0.71,
                evidence_ids=[str(UUID(int=500))],
                source_uris=["github://example/mgar-test/src/counter.py"],
            )
        ],
        failed_attempts=["read_file src/missing.py: GITHUB_NOT_FOUND"],
        pending_approvals=["publish the report"],
        open_questions=["is the cache shared between requests?"],
        available_tools=["read_file", "search_code"],
    )
    bundle = ContextBuilder(max_tokens=4000, recent_window=3).build(request)
    assert bundle.system_instructions == SYSTEM
    assert bundle.task == request.task
    assert bundle.constraints == request.constraints
    assert bundle.repository == "example/mgar-test"
    assert bundle.commit == COMMIT
    assert bundle.current_plan == request.current_plan
    assert bundle.verified_evidence == request.evidence
    assert bundle.relevant_memories[0].requires_reverification is True
    assert bundle.relevant_memories[0].version_status == "historical"
    assert bundle.failed_attempts == ["read_file src/missing.py: GITHUB_NOT_FOUND"]
    assert bundle.pending_approvals == ["publish the report"]
    assert bundle.open_questions == ["is the cache shared between requests?"]
    assert bundle.permissions == request.permissions
    assert bundle.available_tools == request.available_tools
    assert bundle.remaining_budget == request.remaining_budget
    assert bundle.compaction is not None
    assert bundle.compaction.observations_total == 12
    assert bundle.compaction.observations_compressed > 0
    assert bundle.estimated_tokens <= 4000
    assert bundle.budget is not None
    assert (
        bundle.budget.pinned_tokens
        + bundle.budget.plan_tokens
        + bundle.budget.observation_tokens
        + bundle.budget.memory_tokens
        <= bundle.budget.max_tokens
    )
    newest = observations[-1]
    assert any(
        item.evidence_id == newest.evidence_id
        for item in bundle.recent_observations
    )
    payload = bundle.to_payload()
    for key in (
        "task",
        "constraints",
        "repository",
        "commit",
        "current_plan",
        "recent_observations",
        "verified_evidence",
        "relevant_memories",
        "failed_attempts",
        "open_questions",
        "pending_approvals",
        "permissions",
        "available_tools",
        "remaining_budget",
        "compaction_summary",
    ):
        assert key in payload


def test_long_task_keeps_running_inside_the_token_limit() -> None:
    observations = [_observation(index, size=6000) for index in range(60)]
    bundle = ContextBuilder(max_tokens=3000, recent_window=4).build(
        _request(observations=observations)
    )
    assert bundle.estimated_tokens <= 3000
    assert bundle.compaction is not None
    assert bundle.compaction.observations_total == 60
    assert bundle.compaction.observations_omitted > 0
    newest = observations[-1]
    kept = [
        item
        for item in bundle.recent_observations
        if item.evidence_id == newest.evidence_id
    ]
    assert kept
    assert kept[0].tool == "read_file"
    assert kept[0].reference == newest.reference


def test_constraints_failures_questions_and_evidence_survive_compaction() -> None:
    observations = [_observation(index, size=3000) for index in range(40)]
    request = _request(
        constraints=["read-only", "fixed commit only"],
        observations=observations,
        failed_attempts=["read_file src/gone.py: GITHUB_NOT_FOUND"],
        pending_approvals=["approve the publish step"],
        open_questions=["is the cache shared?"],
        available_tools=["read_file"],
    )
    bundle = ContextBuilder(max_tokens=3000, recent_window=2).build(request)
    assert bundle.constraints == request.constraints
    assert "read_file src/gone.py: GITHUB_NOT_FOUND" in bundle.failed_attempts
    assert bundle.pending_approvals == ["approve the publish step"]
    assert bundle.open_questions == ["is the cache shared?"]
    expected = {item.evidence_id for item in observations}
    assert expected <= set(bundle.invariants().evidence_ids)
    assert diff_invariants(request_invariants(request), bundle.invariants()) == []
    assert bundle.estimated_tokens <= 3000


def test_duplicate_observations_merge_without_losing_evidence_ids() -> None:
    base = _observation(7, size=200)
    copies = [
        base.model_copy(update={"evidence_id": str(UUID(int=700 + index))})
        for index in range(5)
    ]
    bundle = ContextBuilder(max_tokens=4000, recent_window=2).build(
        _request(observations=copies, available_tools=["read_file"])
    )
    assert bundle.compaction is not None
    assert bundle.compaction.observations_deduplicated == 4
    assert len(bundle.recent_observations) == 1
    assert len(bundle.compaction.evidence_ids) == 5


def test_artifact_pagination_round_trip_and_backtracking(tmp_path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    content = ("alpha\n" * 400).encode()
    content_hash = store.put(content)
    paginator = ArtifactPaginator(store, page_bytes=128)
    pages = []
    cursor = None
    while True:
        page = paginator.page(content_hash, cursor)
        pages.append(page)
        cursor = page.continuation
        if cursor is None:
            break
    assert len(pages) > 1
    assert b"".join(page.text.encode() for page in pages) == content
    assert pages[0].previous is None
    assert pages[-1].continuation is None
    previous = paginator.page(content_hash, pages[-1].previous)
    assert previous.offset == max(0, pages[-1].offset - 128)
    assert content[previous.offset : previous.end_offset] == previous.text.encode()
    assert store.read(content_hash) == content
    with pytest.raises(DomainError) as invalid:
        paginator.page(content_hash, "not-a-cursor")
    assert invalid.value.code == "CONTEXT_CURSOR_INVALID"
    other_hash = store.put(b"other artifact")
    with pytest.raises(DomainError) as wrong_source:
        paginator.page(other_hash, pages[0].continuation)
    assert wrong_source.value.code == "CONTEXT_CURSOR_INVALID"


def test_long_observation_becomes_a_segment_reference() -> None:
    observation = _observation(3, size=5000)
    compacted, changed = compress_observation(observation, max_chars=300)
    assert changed is True
    assert compacted.truncated is True
    assert compacted.segment is not None
    assert compacted.segment.total_chars == len(observation.content)
    assert compacted.segment.locator == observation.locator
    assert compacted.segment.reference == observation.reference
    assert len(compacted.content) < len(observation.content)
    assert compacted.evidence_id == observation.evidence_id


def test_compaction_cannot_widen_permissions_or_tools() -> None:
    bundle = ContextBuilder(max_tokens=3000).build(
        _request(available_tools=["read_file", "search_code"])
    )
    base = bundle.invariants()
    escalated = base.model_copy(
        update={
            "permissions": base.permissions.model_copy(
                update={
                    "allowed_tools": [
                        *base.permissions.allowed_tools,
                        "create_pull_request",
                    ],
                    "budgets": {
                        **base.permissions.budgets,
                        "max_input_tokens": 120000,
                    },
                    "read_only": False,
                }
            ),
            "available_tools": [*base.available_tools, "create_pull_request"],
        }
    )
    with pytest.raises(DomainError) as excinfo:
        validate_compaction(base, escalated)
    message = str(excinfo.value)
    assert excinfo.value.code == "CONTEXT_INVARIANT_VIOLATED"
    assert "permission_escalation:tool:create_pull_request" in message
    assert "permission_escalation:budget:max_input_tokens" in message
    assert "permission_escalation:read_only" in message
    assert "tool_escalation:create_pull_request" in message


def test_compaction_rejects_lost_failures_questions_and_evidence() -> None:
    bundle = ContextBuilder(max_tokens=3000).build(
        _request(
            evidence=[
                EvidenceRef(
                    evidence_id=str(UUID(int=900)),
                    source_uri="github://example/mgar-test/src/counter.py",
                    artifact_hash="c" * 64,
                )
            ],
            failed_attempts=["search_code duplicate: GITHUB_TIMEOUT"],
            open_questions=["why are there two counters?"],
        )
    )
    base = bundle.invariants()
    tampered = base.model_copy(
        update={
            "failed_attempts": [],
            "open_questions": [],
            "evidence_ids": [],
            "artifact_hashes": {},
        }
    )
    with pytest.raises(DomainError) as excinfo:
        validate_compaction(base, tampered)
    message = str(excinfo.value)
    assert "missing_failed_attempt" in message
    assert "missing_open_question" in message
    assert "missing_evidence" in message
    assert "missing_artifact" in message


def test_artifact_mutation_is_detected(tmp_path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    content_hash = store.put(b"original immutable bytes")
    assert verify_artifacts(store, [content_hash]) == {content_hash: content_hash}
    store.path(content_hash).write_bytes(b"tampered bytes")
    with pytest.raises(DomainError) as excinfo:
        verify_artifacts(store, [content_hash])
    assert excinfo.value.code == "ARTIFACT_CORRUPTED"


class _Runtime:
    def system_prompt(self, spec: AnalysisSpec) -> str:
        return SYSTEM

    def prior_report_text(self, spec: AnalysisSpec) -> str | None:
        return None


def _analysis_spec() -> AnalysisSpec:
    return AnalysisSpec(
        run_id=uuid4(),
        thread_id=uuid4(),
        ticket_id=uuid4(),
        repository="example/mgar-test",
        requested_ref=None,
        commit=COMMIT,
        task="trace the duplicate counter",
        execution_mode="fixture",
        limits=AnalysisLimits(
            max_model_calls=6, max_tool_calls=20, max_input_tokens=6000
        ),
        model=AnalysisModelConfig(),
        deadline_at=datetime.now(UTC) + timedelta(minutes=5),
        authorization_hash="a" * 64,
        profile_hash="b" * 64,
    )


def test_agent_payload_is_compacted_without_losing_evidence() -> None:
    spec = _analysis_spec()
    history = [
        {
            "tool": "read_file",
            "reference": f"github://example/mgar-test/src/file_{index}.py",
            "content": "z" * 4000,
            "evidence_id": str(UUID(int=1000 + index)),
            "source_version": f"blob-{index}",
            "truncated": False,
            "locator": {"start_line": 1, "end_line": 400},
        }
        for index in range(30)
    ]
    state = {"run_id": str(spec.run_id), "history": history, "evidence": history}
    payload = RepoAnalysisAgent(_Runtime())._payload(spec, state, 3)
    assert payload["task"] == spec.task
    assert payload["repository"] == spec.repository
    assert payload["remaining_budget"]["model_calls"] == 6
    assert payload["compaction_summary"]["observations_total"] == 30
    assert len(payload["history"]) < len(history)
    assert payload["history"][0]["segment"] is not None
    expected = {item["evidence_id"] for item in history}
    assert expected <= set(payload["compaction_summary"]["evidence_ids"])
    newest = history[-1]
    kept = [
        item for item in payload["history"] if item["evidence_id"] == newest["evidence_id"]
    ]
    assert kept
    assert kept[0]["reference"] == newest["reference"]
    if kept[0]["segment"] is not None:
        assert kept[0]["segment"]["total_chars"] == len(newest["content"])
    else:
        assert kept[0]["content"] == newest["content"]
    encoded = SYSTEM + json.dumps(payload, ensure_ascii=False)
    assert len(encoded.encode()) // 3 <= spec.limits.max_input_tokens
