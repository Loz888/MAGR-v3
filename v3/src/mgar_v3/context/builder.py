"""V3.3 M04 context builder.

Every model call receives one :class:`ContextBundle` assembled from the
authoritative run state.  The bundle has fixed sections, a deterministic token
budget, a recent execution window, pinned evidence references and a compacted
history summary.  Safety-relevant sections (permissions, failed attempts,
pending approvals, open questions, evidence ids and artifact hashes) are
validated before and after compaction so compression can never widen
authority or erase an unresolved item.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol

from pydantic import Field

from mgar_v3.context.compactor import (
    CompactionSummary,
    ContextInvariants,
    LineRange,
    Observation,
    PermissionSnapshot,
    TokenCounter,
    compact_observations,
    estimate_tokens,
    merge_failed_attempts,
    validate_compaction,
    verify_artifacts,
)
from mgar_v3.domain import DomainError, Model
from mgar_v3.evidence.artifacts import ArtifactStore

_PLAN_SHARE = 0.10
_MEMORY_SHARE = 0.15


class RemainingBudget(Model):
    model_calls: int = Field(ge=0)
    tool_calls: int = Field(ge=0)
    input_tokens: int = Field(ge=0)


class EvidenceRef(Model):
    evidence_id: str
    source_uri: str
    source_version: str | None = None
    artifact_hash: str | None = None
    locator: LineRange | None = None


class MemoryHitLike(Protocol):
    memory_id: Any
    subject_key: str
    version_status: str
    requires_reverification: bool
    confidence: float
    evidence_ids: Sequence[Any]
    source_uris: Sequence[str]


class MemoryNote(Model):
    memory_id: str
    subject_key: str
    version_status: str
    requires_reverification: bool
    confidence: float
    evidence_ids: list[str] = Field(default_factory=list)
    source_uris: list[str] = Field(default_factory=list)

    @classmethod
    def from_hit(cls, hit: MemoryHitLike) -> MemoryNote:
        return cls(
            memory_id=str(hit.memory_id),
            subject_key=hit.subject_key,
            version_status=hit.version_status,
            requires_reverification=hit.requires_reverification,
            confidence=float(hit.confidence),
            evidence_ids=[str(value) for value in hit.evidence_ids],
            source_uris=[str(value) for value in hit.source_uris],
        )


class ContextBudgetAllocation(Model):
    max_tokens: int
    pinned_tokens: int
    plan_tokens: int
    observation_tokens: int
    memory_tokens: int


class ContextRequest(Model):
    """Everything the builder is allowed to consider for one model call."""

    system_instructions: str
    task: str
    repository: str
    commit: str
    permissions: PermissionSnapshot
    remaining_budget: RemainingBudget
    constraints: list[str] = Field(default_factory=list)
    current_plan: list[str] = Field(default_factory=list)
    observations: list[Observation] = Field(default_factory=list)
    evidence: list[EvidenceRef] = Field(default_factory=list)
    memories: list[MemoryNote] = Field(default_factory=list)
    failed_attempts: list[str] = Field(default_factory=list)
    pending_approvals: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    available_tools: list[str] = Field(default_factory=list)


class ContextBundle(Model):
    """The twelve fixed sections handed to the model, plus budget metadata."""

    system_instructions: str
    task: str
    constraints: list[str] = Field(default_factory=list)
    repository: str
    commit: str
    current_plan: list[str] = Field(default_factory=list)
    recent_observations: list[Observation] = Field(default_factory=list)
    verified_evidence: list[EvidenceRef] = Field(default_factory=list)
    relevant_memories: list[MemoryNote] = Field(default_factory=list)
    failed_attempts: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    pending_approvals: list[str] = Field(default_factory=list)
    permissions: PermissionSnapshot
    available_tools: list[str] = Field(default_factory=list)
    remaining_budget: RemainingBudget
    compaction: CompactionSummary | None = None
    budget: ContextBudgetAllocation | None = None
    estimated_tokens: int = 0

    def invariants(self) -> ContextInvariants:
        evidence_ids = {item.evidence_id for item in self.verified_evidence}
        evidence_ids.update(
            item.evidence_id
            for item in self.recent_observations
            if item.evidence_id is not None
        )
        artifacts = {
            item.artifact_hash: item.artifact_hash
            for item in self.verified_evidence
            if item.artifact_hash is not None
        }
        if self.compaction is not None:
            evidence_ids.update(self.compaction.evidence_ids)
            artifacts.update(self.compaction.artifact_hashes)
        return ContextInvariants(
            permissions=self.permissions,
            available_tools=list(self.available_tools),
            failed_attempts=list(self.failed_attempts),
            pending_approvals=list(self.pending_approvals),
            open_questions=list(self.open_questions),
            evidence_ids=sorted(evidence_ids),
            artifact_hashes=artifacts,
        )

    def to_payload(self) -> dict[str, Any]:
        """Serialise the model-facing sections; the system prompt stays separate."""

        return {
            "task": self.task,
            "constraints": self.constraints,
            "repository": self.repository,
            "commit": self.commit,
            "current_plan": self.current_plan,
            "recent_observations": [
                item.model_dump(mode="json") for item in self.recent_observations
            ],
            "verified_evidence": [
                item.model_dump(mode="json") for item in self.verified_evidence
            ],
            "relevant_memories": [
                item.model_dump(mode="json") for item in self.relevant_memories
            ],
            "failed_attempts": self.failed_attempts,
            "open_questions": self.open_questions,
            "pending_approvals": self.pending_approvals,
            "permissions": self.permissions.model_dump(mode="json"),
            "available_tools": self.available_tools,
            "remaining_budget": self.remaining_budget.model_dump(mode="json"),
            "compaction_summary": (
                self.compaction.model_dump(mode="json")
                if self.compaction is not None
                else None
            ),
        }


def request_invariants(request: ContextRequest) -> ContextInvariants:
    """The pre-compaction safety projection used for consistency checking."""

    evidence_ids = {item.evidence_id for item in request.evidence}
    evidence_ids.update(
        item.evidence_id
        for item in request.observations
        if item.evidence_id is not None
    )
    artifacts = {
        item.artifact_hash: item.artifact_hash
        for item in request.evidence
        if item.artifact_hash is not None
    }
    artifacts.update(
        {
            item.artifact_hash: item.artifact_hash
            for item in request.observations
            if item.artifact_hash is not None
        }
    )
    return ContextInvariants(
        permissions=request.permissions,
        available_tools=list(request.available_tools),
        failed_attempts=merge_failed_attempts(
            request.failed_attempts, request.observations
        ),
        pending_approvals=list(dict.fromkeys(request.pending_approvals)),
        open_questions=list(dict.fromkeys(request.open_questions)),
        evidence_ids=sorted(evidence_ids),
        artifact_hashes=artifacts,
    )


def _fit_texts(values: Sequence[str], budget: int, counter: TokenCounter) -> list[str]:
    kept: list[str] = []
    used = 0
    for value in values:
        cost = counter(value)
        if used + cost > budget:
            break
        kept.append(value)
        used += cost
    return kept


def _fit_memories(
    memories: Sequence[MemoryNote], budget: int, counter: TokenCounter
) -> list[MemoryNote]:
    kept: list[MemoryNote] = []
    used = 0
    for memory in memories:
        cost = counter(memory)
        if used + cost > budget:
            break
        kept.append(memory)
        used += cost
    return kept


class ContextBuilder:
    """Assemble a bounded, invariant-checked ContextBundle for one model call."""

    def __init__(
        self,
        *,
        max_tokens: int = 60000,
        recent_window: int = 4,
        counter: TokenCounter = estimate_tokens,
        artifacts: ArtifactStore | None = None,
    ):
        if not 512 <= max_tokens <= 262144:
            raise DomainError(
                "CONTEXT_BOUND", "Context token budget must be 512 to 262144", 400
            )
        if not 1 <= recent_window <= 32:
            raise DomainError("CONTEXT_BOUND", "Recent window must be 1 to 32", 400)
        self.max_tokens = max_tokens
        self.recent_window = recent_window
        self.counter = counter
        self.artifacts = artifacts

    def _pinned_tokens(self, request: ContextRequest) -> int:
        parts: list[Any] = [
            request.system_instructions,
            request.task,
            *request.constraints,
            request.repository,
            request.commit,
            request.permissions.model_dump(mode="json"),
            request.failed_attempts,
            request.pending_approvals,
            request.open_questions,
            request.available_tools,
            request.remaining_budget.model_dump(mode="json"),
            [item.model_dump(mode="json") for item in request.evidence],
        ]
        return sum(self.counter(part) for part in parts) + 64

    def _validate_scope(self, request: ContextRequest) -> None:
        mismatched = [
            name
            for name in ("repository", "commit")
            if getattr(request.permissions, name) != getattr(request, name)
        ]
        if mismatched:
            raise DomainError(
                "CONTEXT_SCOPE_MISMATCH",
                "Context permissions do not match the requested scope: "
                + ", ".join(mismatched),
                403,
            )

    def build(self, request: ContextRequest) -> ContextBundle:
        self._validate_scope(request)
        pinned = self._pinned_tokens(request)
        if pinned >= self.max_tokens:
            raise DomainError(
                "CONTEXT_BUDGET_EXCEEDED",
                "Pinned context already exceeds the token budget",
                413,
            )
        flex = self.max_tokens - pinned
        plan_budget = int(flex * _PLAN_SHARE)
        memory_budget = int(flex * _MEMORY_SHARE)
        observation_budget = max(32, flex - plan_budget - memory_budget)
        system_tokens = self.counter(request.system_instructions)
        bundle: ContextBundle | None = None
        for _ in range(3):
            plan = _fit_texts(request.current_plan, plan_budget, self.counter)
            memories = _fit_memories(request.memories, memory_budget, self.counter)
            compaction = compact_observations(
                request.observations,
                token_budget=observation_budget,
                recent_window=self.recent_window,
                counter=self.counter,
                failed_attempts=request.failed_attempts,
                pending_approvals=request.pending_approvals,
                open_questions=request.open_questions,
                permissions=request.permissions,
            )
            bundle = ContextBundle(
                system_instructions=request.system_instructions,
                task=request.task,
                constraints=list(request.constraints),
                repository=request.repository,
                commit=request.commit,
                current_plan=plan,
                recent_observations=compaction.observations,
                verified_evidence=list(request.evidence),
                relevant_memories=memories,
                failed_attempts=compaction.summary.failed_attempts,
                open_questions=compaction.summary.open_questions,
                pending_approvals=compaction.summary.pending_approvals,
                permissions=request.permissions,
                available_tools=list(request.available_tools),
                remaining_budget=request.remaining_budget,
                compaction=compaction.summary,
                budget=ContextBudgetAllocation(
                    max_tokens=self.max_tokens,
                    pinned_tokens=pinned,
                    plan_tokens=plan_budget,
                    observation_tokens=observation_budget,
                    memory_tokens=memory_budget,
                ),
            )
            tokens = self.counter(bundle.to_payload()) + system_tokens
            if tokens <= self.max_tokens:
                break
            overflow = tokens - self.max_tokens
            observation_budget = max(32, observation_budget - overflow)
            plan_budget = max(0, plan_budget - overflow // 2)
            memory_budget = max(0, memory_budget - overflow // 2)
        else:
            raise DomainError(
                "CONTEXT_BUDGET_EXCEEDED",
                "Context cannot be compacted inside the token budget",
                413,
            )
        assert bundle is not None
        validate_compaction(request_invariants(request), bundle.invariants())
        if self.artifacts is not None:
            verify_artifacts(self.artifacts, bundle.invariants().artifact_hashes)
        return bundle.model_copy(update={"estimated_tokens": tokens})
