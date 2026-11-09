"""V3.3 M03: permission-first hybrid memory retrieval."""

import hashlib
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb

from mgar_v3.domain import DomainError
from mgar_v3.memory import (
    EpisodicDetail,
    MemoryDraft,
    MemoryRecord,
    MemoryRetriever,
    MemoryStore,
    recency_weight,
    source_validity,
)
from mgar_v3.rag.embedding import FixtureEmbedding
from mgar_v3.settings import Settings

COMMIT_A = "a" * 40
COMMIT_B = "b" * 40


class _MissingEmbedding:
    dimension = 300
    model_id = "missing-embedding-v1"
    mode = "fixture"

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise DomainError("RAG_MODEL_NOT_INSTALLED", "no local vector model", 503)


class _ReverseReranker:
    model_id = "test-reverse-v1"

    def rerank(self, query: str, documents: list[str]) -> list[float]:
        return [float(index) for index in range(len(documents))]


def _conn(settings: Settings) -> psycopg.Connection:
    return psycopg.connect(settings.database_url.get_secret_value())


def _artifact(settings: Settings, name: str) -> str:
    content_hash = hashlib.sha256(f"memory-retrieval-fixture:{name}".encode()).hexdigest()
    with _conn(settings) as conn:
        conn.execute(
            "INSERT INTO app.artifacts(content_hash,byte_count) VALUES (%s,%s) "
            "ON CONFLICT DO NOTHING",
            (content_hash, len(name)),
        )
    return content_hash


def _scope(settings: Settings) -> tuple[UUID, UUID]:
    thread_id, mandate_id, ticket_id, run_id = uuid4(), uuid4(), uuid4(), uuid4()
    with _conn(settings) as conn:
        conn.execute(
            "INSERT INTO app.threads(thread_id,project_id,repository,external_key) "
            "VALUES (%s,%s,%s,%s)",
            (
                thread_id,
                settings.project_id,
                settings.repository.lower(),
                f"thread:{thread_id}",
            ),
        )
        conn.execute(
            "INSERT INTO app.mandates(mandate_id,project_id,actor,scope,policy_version,"
            "execution_mode) VALUES (%s,%s,%s,%s,%s,%s)",
            (
                mandate_id,
                settings.project_id,
                settings.local_actor,
                Jsonb({}),
                "r1-explicit-command-v1",
                "fixture",
            ),
        )
        conn.execute(
            "INSERT INTO app.tickets(ticket_id,mandate_id,thread_id,status,outcome) "
            "VALUES (%s,%s,%s,'ended','succeeded')",
            (ticket_id, mandate_id, thread_id),
        )
        conn.execute(
            "INSERT INTO app.runs(run_id,ticket_id,attempt_no,input_revision,"
            "graph_version,execution_mode,status,question) VALUES "
            "(%s,%s,1,1,%s,'fixture','succeeded',%s)",
            (run_id, ticket_id, "r1-quotes-v1", "memory retrieval fixture"),
        )
    return ticket_id, run_id


def _evidence(settings: Settings, run_id: UUID, artifact_hash: str, source_uri: str) -> UUID:
    evidence_id = uuid4()
    with _conn(settings) as conn:
        conn.execute(
            "INSERT INTO app.evidence(evidence_id,run_id,project_id,source_uri,"
            "source_version,content_hash,start_line,end_line,tool_call_id) "
            "VALUES (%s,%s,%s,%s,%s,%s,1,2,%s)",
            (
                evidence_id,
                run_id,
                settings.project_id,
                source_uri,
                "fixture-v1",
                artifact_hash,
                uuid4(),
            ),
        )
    return evidence_id


def _create(
    settings: Settings,
    *,
    subject: str,
    ticket_id: UUID,
    run_id: UUID,
    scope: str = "repository",
    commit: str | None = None,
    evidence_ids: list[UUID] | None = None,
    artifact_hashes: list[str] | None = None,
    verification: str = "supported",
    confidence: float = 0.8,
) -> MemoryRecord:
    draft = MemoryDraft(
        project_id=settings.project_id,
        repository=settings.repository,
        commit=commit,
        actor=settings.local_actor,
        memory_kind="episodic",
        scope=scope,  # type: ignore[arg-type]
        subject_key=subject,
        summary_hash=_artifact(settings, subject),
        evidence_ids=evidence_ids or [],
        artifact_hashes=artifact_hashes or [],
        confidence=confidence,
        verification_status=verification,  # type: ignore[arg-type]
        detail=EpisodicDetail(
            ticket_id=ticket_id,
            run_id=run_id,
            event_kind="investigation_completed:success",
            occurred_at=datetime.now(UTC),
        ),
    )
    return MemoryStore(settings).create(draft)


def test_relevant_memory_returns_evidence_confidence_and_version(settings: Settings) -> None:
    ticket_id, run_id = _scope(settings)
    artifact = _artifact(settings, "duplicate-summary")
    evidence_id = _evidence(settings, run_id, artifact, "repo://src/app.py")
    record = _create(
        settings,
        subject="duplicate-count:src/app.py",
        ticket_id=ticket_id,
        run_id=run_id,
        evidence_ids=[evidence_id],
        artifact_hashes=[artifact],
        verification="verified",
    )
    _create(
        settings,
        subject="release-window:ops/deploy.py",
        ticket_id=ticket_id,
        run_id=run_id,
        artifact_hashes=[artifact],
    )

    result = MemoryRetriever(settings).search_memory("duplicate count", top_k=5)

    assert result.retrieval_mode == "lexical_fallback"
    assert [hit.memory_id for hit in result.hits] == [record.memory_id]
    hit = result.hits[0]
    assert hit.evidence_ids == [evidence_id]
    assert hit.source_uris == ["repo://src/app.py"]
    assert hit.verification_status == "verified"
    assert hit.confidence == pytest.approx(0.8)
    assert hit.score > 0
    assert hit.version_status == "repository"
    assert hit.requires_reverification is False
    assert hit.created_at.tzinfo is not None


def test_unrelated_project_and_denied_actor_never_leak(settings: Settings) -> None:
    other = settings.model_copy(
        update={"project_id": "other-demo", "repository": "example/other"}
    )
    other_ticket, other_run = _scope(other)
    other_artifact = _artifact(other, "other-summary")
    other_evidence = _evidence(other, other_run, other_artifact, "repo://src/app.py")
    other_record = _create(
        other,
        subject="duplicate-count:src/app.py",
        ticket_id=other_ticket,
        run_id=other_run,
        evidence_ids=[other_evidence],
        verification="verified",
    )

    assert MemoryRetriever(settings).search_memory("duplicate count").hits == []
    visible = MemoryRetriever(other).search_memory("duplicate count")
    assert [hit.memory_id for hit in visible.hits] == [other_record.memory_id]
    with pytest.raises(DomainError) as excinfo:
        MemoryRetriever(settings).search_memory("duplicate count", actor="outsider")
    assert excinfo.value.code == "MEMORY_ACTOR_DENIED"


def test_revoked_memory_is_hidden_but_readable_as_inactive(settings: Settings) -> None:
    ticket_id, run_id = _scope(settings)
    record = _create(
        settings,
        subject="cache-invalidation:src/cache.py",
        ticket_id=ticket_id,
        run_id=run_id,
    )
    MemoryStore(settings).revoke(record.memory_id, "operator withdrew access")

    retriever = MemoryRetriever(settings)
    assert retriever.search_memory("cache invalidation").hits == []
    with pytest.raises(DomainError) as excinfo:
        retriever.read_memory(record.memory_id)
    assert excinfo.value.code == "MEMORY_NOT_FOUND"
    assert retriever.read_memory(record.memory_id, include_inactive=True).status == "revoked"


def test_superseded_version_is_replaced_by_its_successor(settings: Settings) -> None:
    ticket_id, run_id = _scope(settings)
    old = _create(
        settings,
        subject="retry-policy:src/client.py",
        ticket_id=ticket_id,
        run_id=run_id,
    )
    replacement = MemoryDraft(
        project_id=settings.project_id,
        repository=settings.repository,
        actor=settings.local_actor,
        memory_kind="episodic",
        scope="repository",
        subject_key=old.subject_key,
        summary_hash=_artifact(settings, "retry-policy-v2"),
        confidence=0.9,
        verification_status="supported",
        detail=EpisodicDetail(
            ticket_id=ticket_id,
            run_id=run_id,
            event_kind="patch_tests_passed:success",
            occurred_at=datetime.now(UTC),
        ),
    )
    new = MemoryStore(settings).supersede(old.memory_id, replacement)

    result = MemoryRetriever(settings).search_memory("retry policy")
    assert [hit.memory_id for hit in result.hits] == [new.memory_id]
    retired = MemoryRetriever(settings).read_memory(old.memory_id, include_inactive=True)
    assert retired.status == "superseded" and retired.superseded_by == new.memory_id


def test_historical_commit_memory_is_not_current_and_ranks_below_current(
    settings: Settings,
) -> None:
    ticket_id, run_id = _scope(settings)
    old = _create(
        settings,
        subject="retry-policy:src/client.py",
        ticket_id=ticket_id,
        run_id=run_id,
        scope="commit",
        commit=COMMIT_A,
    )
    new = _create(
        settings,
        subject="retry-policy:src/client.py",
        ticket_id=ticket_id,
        run_id=run_id,
        scope="commit",
        commit=COMMIT_B,
    )
    retriever = MemoryRetriever(settings)

    current = retriever.search_memory("retry policy", commit=COMMIT_B)
    assert [hit.memory_id for hit in current.hits] == [new.memory_id, old.memory_id]
    assert [hit.version_status for hit in current.hits] == ["current", "historical"]
    assert current.hits[0].requires_reverification is False
    assert current.hits[1].requires_reverification is True
    assert current.hits[1].commit == COMMIT_A

    unknown = retriever.search_memory("retry policy")
    assert [hit.version_status for hit in unknown.hits] == ["historical", "historical"]
    assert all(hit.requires_reverification for hit in unknown.hits)


def test_stale_memory_is_returned_only_with_reverification(settings: Settings) -> None:
    ticket_id, run_id = _scope(settings)
    record = _create(
        settings,
        subject="schema-drift:src/models.py",
        ticket_id=ticket_id,
        run_id=run_id,
        scope="commit",
        commit=COMMIT_A,
    )
    MemoryStore(settings).mark_stale(record.memory_id, "commit moved")

    result = MemoryRetriever(settings).search_memory("schema drift", commit=COMMIT_A)

    assert [hit.memory_id for hit in result.hits] == [record.memory_id]
    hit = result.hits[0]
    assert hit.status == "stale"
    assert hit.version_status == "stale"
    assert hit.requires_reverification is True
    assert hit.version_status != "current"


def test_repository_knowledge_reports_scope_status(settings: Settings) -> None:
    ticket_id, run_id = _scope(settings)
    repository_record = _create(
        settings,
        subject="build:uses-uv-lock",
        ticket_id=ticket_id,
        run_id=run_id,
    )
    project_record = _create(
        settings,
        subject="process:release-freeze",
        ticket_id=ticket_id,
        run_id=run_id,
        scope="project",
    )
    current = _create(
        settings,
        subject="fix:cache-key",
        ticket_id=ticket_id,
        run_id=run_id,
        scope="commit",
        commit=COMMIT_A,
    )
    _create(
        settings,
        subject="fix:cache-key",
        ticket_id=ticket_id,
        run_id=run_id,
        scope="commit",
        commit=COMMIT_B,
    )
    retriever = MemoryRetriever(settings)

    knowledge = retriever.get_repository_knowledge(COMMIT_A)

    assert knowledge.retrieval_mode == "structured"
    assert knowledge.query == ""
    statuses = {hit.memory_id: hit.version_status for hit in knowledge.hits}
    assert statuses == {
        repository_record.memory_id: "repository",
        project_record.memory_id: "project",
        current.memory_id: "current",
    }
    without_commit = retriever.get_repository_knowledge()
    assert {hit.version_status for hit in without_commit.hits} == {"repository", "project"}


def test_lexical_fallback_and_hybrid_with_dense_scores(settings: Settings) -> None:
    ticket_id, run_id = _scope(settings)
    artifact = _artifact(settings, "lexical-summary")
    evidence_id = _evidence(settings, run_id, artifact, "repo://src/app.py")
    record = _create(
        settings,
        subject="duplicate-count:src/app.py",
        ticket_id=ticket_id,
        run_id=run_id,
        evidence_ids=[evidence_id],
        verification="verified",
    )

    plain = MemoryRetriever(settings).search_memory("duplicate count")
    assert plain.retrieval_mode == "lexical_fallback"
    assert plain.dense_model is None
    assert plain.degraded_reason is None

    hybrid = MemoryRetriever(settings, embedder=FixtureEmbedding()).search_memory(
        "duplicate count"
    )
    assert hybrid.retrieval_mode == "hybrid"
    assert hybrid.dense_model == FixtureEmbedding.model_id
    assert hybrid.hits[0].dense_score is not None

    degraded = MemoryRetriever(settings, embedder=_MissingEmbedding()).search_memory(
        "duplicate count"
    )
    assert degraded.retrieval_mode == "lexical_fallback"
    assert degraded.degraded_reason == "RAG_MODEL_NOT_INSTALLED"
    assert [hit.memory_id for hit in degraded.hits] == [record.memory_id]


def test_rerank_reorders_fused_candidates(settings: Settings) -> None:
    ticket_id, run_id = _scope(settings)
    for subject in ("duplicate-count:src/app.py", "duplicate-count:src/report.py"):
        _create(settings, subject=subject, ticket_id=ticket_id, run_id=run_id)

    plain = MemoryRetriever(settings).search_memory("duplicate count", top_k=5)
    assert len(plain.hits) == 2

    reranked = MemoryRetriever(settings, reranker=_ReverseReranker()).search_memory(
        "duplicate count", top_k=5
    )
    assert reranked.retrieval_mode == "lexical_reranked"
    assert reranked.reranker_model == "test-reverse-v1"
    assert [hit.memory_id for hit in reranked.hits] == [
        plain.hits[1].memory_id,
        plain.hits[0].memory_id,
    ]


def test_source_validity_breaks_relevance_ties(settings: Settings) -> None:
    ticket_id, run_id = _scope(settings)
    artifact = _artifact(settings, "validity-summary")
    evidence_id = _evidence(settings, run_id, artifact, "repo://src/client.py")
    verified = _create(
        settings,
        subject="retry-policy:src/client.py",
        ticket_id=ticket_id,
        run_id=run_id,
        evidence_ids=[evidence_id],
        artifact_hashes=[artifact],
        verification="verified",
    )
    unverified = _create(
        settings,
        subject="retry-policy:src/client.py",
        ticket_id=ticket_id,
        run_id=run_id,
        scope="project",
        evidence_ids=[evidence_id],
        verification="unverified",
    )

    hits = MemoryRetriever(settings).search_memory("retry policy").hits

    assert [hit.memory_id for hit in hits] == [verified.memory_id, unverified.memory_id]
    assert hits[0].source_validity > hits[1].source_validity
    assert hits[0].lexical_score == pytest.approx(hits[1].lexical_score)

    now = datetime.now(UTC)
    assert source_validity("verified", evidence_count=1, artifact_count=1) > source_validity(
        "supported", evidence_count=0, artifact_count=0
    )
    assert recency_weight(now, now) > recency_weight(now - timedelta(days=180), now)


def test_prior_findings_match_evidence_path_and_symbol(settings: Settings) -> None:
    ticket_id, run_id = _scope(settings)
    artifact = _artifact(settings, "prior-summary")
    payments_evidence = _evidence(settings, run_id, artifact, "repo://src/payments.py")
    cache_evidence = _evidence(settings, run_id, artifact, "repo://src/cache.py")
    payments = _create(
        settings,
        subject="charge-total-overflow:src/payments.py",
        ticket_id=ticket_id,
        run_id=run_id,
        evidence_ids=[payments_evidence],
    )
    _create(
        settings,
        subject="cache-warmup:src/cache.py",
        ticket_id=ticket_id,
        run_id=run_id,
        evidence_ids=[cache_evidence],
    )
    retriever = MemoryRetriever(settings)

    by_path = retriever.get_prior_findings("src/payments.py")
    assert [hit.memory_id for hit in by_path.hits] == [payments.memory_id]
    assert by_path.hits[0].source_uris == ["repo://src/payments.py"]

    by_symbol = retriever.get_prior_findings("src/unknown.py", "charge-total-overflow")
    assert [hit.memory_id for hit in by_symbol.hits] == [payments.memory_id]


def test_bounds_and_scope_validation(settings: Settings) -> None:
    retriever = MemoryRetriever(settings)
    for top_k in (0, 33):
        with pytest.raises(DomainError) as excinfo:
            retriever.search_memory("cache", top_k=top_k)
        assert excinfo.value.code == "MEMORY_BOUND_INVALID"
    with pytest.raises(DomainError) as excinfo:
        retriever.search_memory("   ")
    assert excinfo.value.code == "MEMORY_QUERY_INVALID"
    with pytest.raises(DomainError) as excinfo:
        retriever.search_memory("cache", scope="commit")
    assert excinfo.value.code == "MEMORY_SCOPE_INVALID"
    with pytest.raises(DomainError) as excinfo:
        retriever.search_memory("cache", scope="repository", commit=COMMIT_A)
    assert excinfo.value.code == "MEMORY_SCOPE_INVALID"
