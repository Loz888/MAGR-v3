"""V3.3 M05: memory-aware repository analysis loop.

The agent uses confirmed memory at explicit points: task-start recall, planning
hints, post-tool working memory, historical failure lookup, post-verification
candidates and follow-up reuse of facts pinned to the exact commit.

Memory is advisory data only.  Every runtime authority (tool list, repository,
commit, budgets, approvals and sandbox policy) is derived from the immutable
analysis spec, never from recalled content, so a poisoned memory cannot widen
the agent's capabilities.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast
from uuid import UUID

from pydantic import Field

from mgar_v3.domain import DomainError, Model
from mgar_v3.memory import (
    EpisodicDetail,
    MemoryHit,
    MemoryRecord,
    MemoryRetriever,
    MemoryStore,
    MemoryWriter,
    ProceduralDetail,
    SemanticDetail,
    WorkingDetail,
    execution_candidate,
)
from mgar_v3.memory.model import MemoryDraft
from mgar_v3.repository_analysis.domain import AnalysisSpec
from mgar_v3.settings import Settings

_WORKING_TTL = timedelta(hours=2)
_SUMMARY_LIMIT = 600
_PLAN_HINT_LIMIT = 3
_INJECTION = re.compile(
    r"("
    r"ignore\s+(all\s+)?(previous|prior|above)\s+instructions|"
    r"disregard\s+(all\s+)?(previous|prior|system)|"
    r"system\s+prompt|you\s+are\s+now|developer\s+mode|jailbreak|"
    r"reveal\s+the\s+(system|hidden)|do\s+not\s+tell\s+the\s+user"
    r")",
    re.IGNORECASE,
)
_AUTHORITY = re.compile(
    r"("
    r"available_tools|allowed_tools|allowed_repositories|max_model_calls|"
    r"max_tool_calls|max_input_tokens|sandbox|approval|approve|"
    r"budgets?\s*[:=]|permissions?\s*[:=]|"
    r"(increase|raise|extend|override)\s+(the\s+)?(budget|limit|permission)"
    r")",
    re.IGNORECASE,
)


class MemoryAdvisory(Model):
    """One recalled memory rendered as reference-only guidance."""

    memory_id: str
    memory_kind: str
    subject_key: str
    commit: str | None = None
    version_status: str
    requires_reverification: bool
    downweighted: bool
    confidence: float
    score: float
    reference_only: Literal[True] = True
    evidence_ids: list[str] = Field(default_factory=list)
    artifact_hashes: list[str] = Field(default_factory=list)
    source_uris: list[str] = Field(default_factory=list)
    text: str


class ExcludedMemory(Model):
    memory_id: str
    subject_key: str
    reason: str


class TaskMemory(Model):
    query: str
    commit: str
    retrieval_mode: str
    follow_up: bool = False
    advisories: list[MemoryAdvisory] = Field(default_factory=list)
    plan_hints: list[str] = Field(default_factory=list)
    confirmed_facts: list[str] = Field(default_factory=list)
    excluded: list[ExcludedMemory] = Field(default_factory=list)


class MemoryWriteResult(Model):
    status: Literal["confirmed", "requires_confirmation", "rejected", "skipped"]
    candidate_id: str | None = None
    memory_id: str | None = None
    issues: list[str] = Field(default_factory=list)


def _subject_key(spec: AnalysisSpec) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", spec.task.lower()).strip("-")[:48] or "task"
    return f"investigation:{slug}:{spec.commit[:12]}"


class AnalysisMemory:
    """Repository-scoped memory view used by one analysis runtime."""

    def __init__(self, settings: Settings, *, engine: Any | None = None):
        self.settings = settings
        self.engine = engine
        self._views: dict[
            str, tuple[Settings, MemoryStore, MemoryRetriever, MemoryWriter]
        ] = {}

    # -- scope -----------------------------------------------------------
    def _view(
        self, spec: AnalysisSpec
    ) -> tuple[Settings, MemoryStore, MemoryRetriever, MemoryWriter]:
        repository = spec.repository.lower()
        cached = self._views.get(repository)
        if cached is not None:
            return cached
        scoped = self.settings
        if self.settings.repository.lower() != repository:
            scoped = self.settings.model_copy(update={"repository": repository})
        store = MemoryStore(scoped)
        view = (scoped, store, MemoryRetriever(scoped, store), MemoryWriter(scoped, store))
        self._views[repository] = view
        return view

    # -- task start / planning -------------------------------------------
    def recall_task(self, spec: AnalysisSpec, *, top_k: int = 6) -> TaskMemory:
        _, store, retriever, _ = self._view(spec)
        query = spec.task.strip()
        results = []
        try:
            results.append(
                retriever.search_memory(query, commit=spec.commit, top_k=top_k * 2)
            )
        except DomainError:
            pass
        try:
            results.append(retriever.get_repository_knowledge(spec.commit, top_k=16))
        except DomainError:
            pass
        merged: dict[str, MemoryHit] = {}
        modes: list[str] = []
        for result in results:
            modes.append(result.retrieval_mode)
            for hit in result.hits:
                key = str(hit.memory_id)
                previous = merged.get(key)
                if previous is None or hit.score >= previous.score:
                    merged[key] = hit
        advisories: list[MemoryAdvisory] = []
        excluded: list[ExcludedMemory] = []
        for hit in merged.values():
            if hit.memory_kind == "working":
                continue
            try:
                record = store.get(hit.memory_id)
            except DomainError:
                excluded.append(
                    ExcludedMemory(
                        memory_id=str(hit.memory_id),
                        subject_key=hit.subject_key,
                        reason="UNREADABLE",
                    )
                )
                continue
            reason = self._exclusion(hit, record)
            if reason is not None:
                excluded.append(
                    ExcludedMemory(
                        memory_id=str(hit.memory_id),
                        subject_key=hit.subject_key,
                        reason=reason,
                    )
                )
                continue
            advisories.append(self._advisory(hit, record))
        advisories.sort(key=lambda item: (item.downweighted, -item.score, item.subject_key))
        advisories = advisories[:top_k]
        hints = [
            (
                f"[reference-only] {item.subject_key} "
                f"({item.version_status}, confidence {item.confidence:.2f}): "
                f"{item.text[:240]}"
            )
            for item in advisories
            if not item.requires_reverification and item.confidence >= 0.5
        ][:_PLAN_HINT_LIMIT]
        facts: list[str] = []
        if spec.parent_run_id is not None:
            facts = [
                f"{item.subject_key} @ {item.commit}: {item.text[:240]}"
                for item in advisories
                if item.version_status == "current" and item.evidence_ids
            ][:_PLAN_HINT_LIMIT]
        return TaskMemory(
            query=query,
            commit=spec.commit,
            retrieval_mode="+".join(dict.fromkeys(modes)) or "none",
            follow_up=spec.parent_run_id is not None,
            advisories=advisories,
            plan_hints=hints,
            confirmed_facts=facts,
            excluded=excluded,
        )

    def recall_failures(
        self, spec: AnalysisSpec, *, error_code: str, tool: str, top_k: int = 3
    ) -> list[MemoryAdvisory]:
        _, store, retriever, _ = self._view(spec)
        results = []
        try:
            results.append(
                retriever.search_memory(
                    f"{error_code} {tool} failure", commit=spec.commit, top_k=top_k * 3
                )
            )
        except DomainError:
            pass
        try:
            results.append(retriever.get_repository_knowledge(spec.commit, top_k=32))
        except DomainError:
            pass
        advisories: list[MemoryAdvisory] = []
        seen: set[str] = set()
        for result in results:
            for hit in result.hits:
                if hit.memory_kind == "working" or str(hit.memory_id) in seen:
                    continue
                try:
                    record = store.get(hit.memory_id)
                except DomainError:
                    continue
                if self._exclusion(hit, record) is not None:
                    continue
                text = self._content(record)
                haystack = f"{record.subject_key} {text}".lower()
                if error_code.lower() not in haystack and "fail" not in haystack:
                    continue
                seen.add(str(hit.memory_id))
                advisories.append(self._advisory(hit, record))
                if len(advisories) >= top_k:
                    return advisories
        return advisories

    # -- working memory --------------------------------------------------
    def record_working(
        self, spec: AnalysisSpec, observations: Sequence[dict[str, Any]]
    ) -> MemoryRecord | None:
        if not observations:
            return None
        scoped, store, _, _ = self._view(spec)
        payload = {
            "run_id": str(spec.run_id),
            "commit": spec.commit,
            "observations": [
                {
                    "tool": str(item.get("tool") or ""),
                    "reference": str(item.get("reference") or ""),
                    "evidence_id": item.get("evidence_id"),
                    "error_code": item.get("error_code"),
                }
                for item in observations[-20:]
            ],
        }
        text = json.dumps(payload, ensure_ascii=False, sort_keys=True)[:4000]
        summary_hash = self._artifact(text)
        evidence_ids = [
            UUID(str(item["evidence_id"]))
            for item in observations
            if item.get("evidence_id")
        ]
        draft = MemoryDraft(
            project_id=scoped.project_id,
            repository=spec.repository,
            commit=spec.commit,
            actor=scoped.local_actor,
            memory_kind="working",
            scope="commit",
            subject_key=f"run:{spec.run_id}",
            summary_hash=summary_hash,
            evidence_ids=list(dict.fromkeys(evidence_ids))[:64],
            confidence=0.5,
            verification_status="supported",
            detail=WorkingDetail(
                run_id=spec.run_id,
                expires_at=datetime.now(UTC) + _WORKING_TTL,
            ),
        )
        try:
            current = store.current(
                memory_kind="working",
                scope="commit",
                subject_key=draft.subject_key,
                commit=spec.commit,
            )
            if current is not None:
                return store.supersede(current.memory_id, draft)
            return store.create(draft)
        except DomainError:
            return None

    # -- verification ----------------------------------------------------
    def after_verification(
        self,
        spec: AnalysisSpec,
        *,
        report_hash: str | None,
        passed: bool,
        evidence_ids: Sequence[UUID],
        conclusion: str,
        artifact_hashes: Sequence[str] = (),
    ) -> MemoryWriteResult:
        scoped, _, _, writer = self._view(spec)
        if report_hash is None:
            return MemoryWriteResult(status="skipped", issues=["REPORT_HASH_MISSING"])
        compact = " ".join(conclusion.split())[:800]
        summary = (
            f"task={spec.task[:600]} | outcome={'success' if passed else 'failure'} "
            f"| commit={spec.commit} | conclusion={compact}"
        )[:2000]
        summary_hash = self._artifact(summary)
        evidence = list(dict.fromkeys(evidence_ids))[:32]
        artifacts = list(dict.fromkeys([report_hash, *artifact_hashes]))[:8]
        candidate = execution_candidate(
            trigger="investigation_completed",
            outcome="success" if passed else "failure",
            project_id=scoped.project_id,
            repository=spec.repository,
            actor=scoped.local_actor,
            subject_key=_subject_key(spec),
            summary=summary,
            summary_hash=summary_hash,
            ticket_id=spec.ticket_id,
            run_id=spec.run_id,
            occurred_at=datetime.now(UTC),
            evidence_ids=evidence,
            artifact_hashes=artifacts,
            confidence=0.9 if passed and evidence else 0.6,
            verification_status="verified" if evidence else "supported",
            provenance="mechanical" if evidence else "agent_reported",
            commit=spec.commit,
            scope="commit",
        )
        validation = writer.validate(candidate, actor=scoped.local_actor)
        if not validation.accepted:
            return MemoryWriteResult(
                status="rejected",
                candidate_id=validation.candidate_id,
                issues=sorted({issue.code for issue in validation.issues}),
            )
        if validation.requires_confirmation:
            return MemoryWriteResult(
                status="requires_confirmation", candidate_id=validation.candidate_id
            )
        try:
            record = writer.write(candidate, actor=scoped.local_actor)
        except DomainError as exc:
            return MemoryWriteResult(
                status="rejected",
                candidate_id=validation.candidate_id,
                issues=[exc.code],
            )
        return MemoryWriteResult(
            status="confirmed",
            candidate_id=validation.candidate_id,
            memory_id=str(record.memory_id),
        )

    # -- filtering and rendering -----------------------------------------
    def _exclusion(self, hit: MemoryHit, record: MemoryRecord) -> str | None:
        if hit.verification_status == "disputed":
            return "DISPUTED_EVIDENCE"
        if hit.verification_status == "unverified":
            return "UNVERIFIED"
        text = self._content(record)
        if _INJECTION.search(text) or _INJECTION.search(record.subject_key):
            return "PROMPT_INJECTION"
        if _AUTHORITY.search(text) or _AUTHORITY.search(record.subject_key):
            return "AUTHORITY_CLAIM"
        return None

    def _advisory(self, hit: MemoryHit, record: MemoryRecord) -> MemoryAdvisory:
        downweighted = hit.requires_reverification or hit.version_status in {
            "historical",
            "stale",
        }
        return MemoryAdvisory(
            memory_id=str(record.memory_id),
            memory_kind=record.memory_kind,
            subject_key=record.subject_key,
            commit=record.commit,
            version_status=hit.version_status,
            requires_reverification=hit.requires_reverification,
            downweighted=downweighted,
            confidence=record.confidence,
            score=hit.score,
            evidence_ids=[str(item) for item in record.evidence_ids],
            artifact_hashes=[str(item) for item in record.artifact_hashes],
            source_uris=list(hit.source_uris),
            text=self._content(record),
        )

    def _content(self, record: MemoryRecord) -> str:
        summary = self._summary_text(record)
        if summary:
            return summary[:_SUMMARY_LIMIT]
        detail = record.detail
        if isinstance(detail, EpisodicDetail):
            return f"{detail.event_kind} at {detail.occurred_at.isoformat()}"
        if isinstance(detail, SemanticDetail):
            return f"{detail.subject} {detail.predicate}: {detail.object_value}"[
                :_SUMMARY_LIMIT
            ]
        if isinstance(detail, ProceduralDetail):
            return (
                f"{detail.task_key} succeeded {detail.success_count} times and "
                f"failed {detail.failure_count} times"
            )
        if isinstance(detail, WorkingDetail):
            return f"working notes for run {detail.run_id}"
        return record.subject_key

    def _summary_text(self, record: MemoryRecord) -> str | None:
        if self.engine is None:
            return None
        try:
            data = self.engine.artifacts.read(record.summary_hash)
        except (DomainError, OSError):
            return None
        return cast(bytes, data).decode("utf-8", "replace").strip()

    def _artifact(self, text: str) -> str:
        data = text.encode()
        if self.engine is None:
            return hashlib.sha256(data).hexdigest()
        content_hash = cast(str, self.engine.artifacts.put(data))
        with self.engine.transaction() as conn:
            conn.execute(
                "INSERT INTO app.artifacts(content_hash,byte_count) VALUES (%s,%s) "
                "ON CONFLICT DO NOTHING",
                (content_hash, len(data)),
            )
        return content_hash


__all__ = [
    "AnalysisMemory",
    "ExcludedMemory",
    "MemoryAdvisory",
    "MemoryWriteResult",
    "TaskMemory",
]
