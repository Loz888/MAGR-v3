"""V3.3 M03 hybrid memory retrieval.

Retrieval starts from the permission-scoped store: project, repository and
actor filters run in SQL before any scoring, so unrelated projects, revoked
records and superseded versions never reach the ranking stage.  Active and
stale records are scored with BM25 and, when an embedding model is available,
a dense ranking fused through reciprocal rank fusion.  An optional reranker
may reorder the fused candidates.

Every hit carries its commit version status, provenance evidence and
confidence.  Commit-scoped memories from another commit are ``historical`` and
stale memories are ``stale``; both require re-verification and are never
presented as current-version facts.  Without an embedding model retrieval
degrades to lexical BM25 and reports ``lexical_fallback``.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any, Literal, Protocol
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from pydantic import Field

from mgar_v3.domain import DomainError, Model
from mgar_v3.memory.model import (
    EpisodicDetail,
    MemoryKind,
    MemoryLifecycle,
    MemoryRecord,
    MemoryScope,
    MemoryVerification,
    ProceduralDetail,
    SemanticDetail,
)
from mgar_v3.memory.store import MemoryStore
from mgar_v3.rag.embedding import Embedder
from mgar_v3.rag.rank import bm25, rrf
from mgar_v3.rag.text import terms
from mgar_v3.settings import Settings

MemoryVersionStatus = Literal["current", "historical", "repository", "project", "stale"]
RetrievalMode = Literal[
    "lexical_fallback",
    "lexical_reranked",
    "hybrid",
    "hybrid_reranked",
    "structured",
]

_CANDIDATE_LIMIT = 64
_TOP_K_MAX = 32
_RECENCY_HALF_LIFE_DAYS = 90.0
_VERSION_PRIORITY: dict[MemoryVersionStatus, int] = {
    "current": 0,
    "repository": 1,
    "project": 2,
    "historical": 3,
    "stale": 4,
}
_VERSION_AFFINITY: dict[MemoryVersionStatus, float] = {
    "current": 1.0,
    "repository": 0.92,
    "project": 0.9,
    "historical": 0.8,
    "stale": 0.7,
}
_VERIFICATION_RELIABILITY: dict[MemoryVerification, float] = {
    "verified": 1.0,
    "supported": 0.8,
    "unverified": 0.5,
    "disputed": 0.2,
}


class Reranker(Protocol):
    """Optional cross-encoder style reranker; scores align with documents."""

    model_id: str

    def rerank(self, query: str, documents: list[str]) -> list[float]: ...


class MemoryHit(Model):
    memory_id: UUID
    memory_kind: MemoryKind
    scope: MemoryScope
    subject_key: str
    commit: str | None
    version_status: MemoryVersionStatus
    requires_reverification: bool
    status: MemoryLifecycle
    verification_status: MemoryVerification
    confidence: float
    evidence_ids: list[UUID]
    source_uris: list[str]
    relevance: float
    lexical_score: float
    dense_score: float | None
    source_validity: float
    recency: float
    score: float
    created_at: datetime


class MemorySearchResult(Model):
    query: str
    commit: str | None
    retrieval_mode: RetrievalMode
    dense_model: str | None = None
    reranker_model: str | None = None
    degraded_reason: str | None = None
    hits: list[MemoryHit] = Field(default_factory=list)


def source_validity(
    verification_status: MemoryVerification,
    *,
    evidence_count: int,
    artifact_count: int,
) -> float:
    """Validity of the memory's provenance, capped at 1.0."""

    value = _VERIFICATION_RELIABILITY[verification_status]
    if evidence_count > 0:
        value += 0.05
    if artifact_count > 0:
        value += 0.05
    return round(min(value, 1.0), 6)


def recency_weight(created_at: datetime, now: datetime) -> float:
    """Day-granular freshness weight; newer memories score higher."""

    age_days = max(0.0, (now - created_at).total_seconds() / 86_400.0)
    whole_days = int(age_days)
    return round(1.0 / (1.0 + whole_days / _RECENCY_HALF_LIFE_DAYS), 6)


def version_status(record: MemoryRecord, commit: str | None) -> MemoryVersionStatus:
    """Version state of a record relative to the commit being investigated."""

    if record.status == "stale":
        return "stale"
    if record.scope == "commit":
        return "current" if commit is not None and record.commit == commit else "historical"
    if record.scope == "repository":
        return "repository"
    return "project"


class MemoryRetriever:
    """Hybrid, permission-first retrieval over confirmed memories."""

    def __init__(
        self,
        settings: Settings,
        store: MemoryStore | None = None,
        *,
        embedder: Embedder | None = None,
        reranker: Reranker | None = None,
        now: Callable[[], datetime] | None = None,
    ):
        self.settings = settings
        self.store = store or MemoryStore(settings)
        self.embedder = embedder
        self.reranker = reranker
        self._clock = now or (lambda: datetime.now(UTC))

    def search_memory(
        self,
        query: str,
        *,
        scope: MemoryScope | None = None,
        commit: str | None = None,
        memory_kinds: Sequence[MemoryKind] | None = None,
        top_k: int = 8,
        actor: str | None = None,
    ) -> MemorySearchResult:
        """BM25 + optional dense RRF search inside the permission scope."""

        query = _require_query(query)
        top_k = _bound(top_k)
        _require_scope(scope, commit)
        records = self._load(scope=scope, commit=commit, memory_kinds=memory_kinds, actor=actor)
        if not records:
            return MemorySearchResult(
                query=query,
                commit=commit,
                retrieval_mode="lexical_fallback",
                hits=[],
            )
        uris = self._source_uris(records)
        texts = {
            record.memory_id: _document_text(record, uris.get(record.memory_id, []))
            for record in records
        }
        lexical = [(key, value) for key, value in _lexical(query, texts) if value > 0]
        dense: list[tuple[str, float]] = []
        dense_scores: dict[str, float] = {}
        mode: RetrievalMode = "lexical_fallback"
        dense_model: str | None = None
        degraded: str | None = None
        if self.embedder is not None:
            try:
                dense = _dense(query, texts, self.embedder)
            except (DomainError, ValueError) as exc:
                degraded = getattr(exc, "code", type(exc).__name__)
            else:
                dense_scores = dict(dense)
                if dense:
                    mode = "hybrid"
                    dense_model = self.embedder.model_id
        if mode == "hybrid":
            relevance = _normalize(rrf(lexical, dense))
        else:
            relevance = _normalize(lexical)
        lexical_scores = dict(lexical)
        now = self._clock()
        hits = [
            self._hit(
                record,
                version=version_status(record, commit),
                relevance=relevance[str(record.memory_id)],
                lexical_score=lexical_scores.get(str(record.memory_id), 0.0),
                dense_score=dense_scores.get(str(record.memory_id)),
                uris=uris.get(record.memory_id, []),
                now=now,
            )
            for record in records
            if relevance.get(str(record.memory_id), 0.0) > 0
        ]
        hits = self._sorted(hits)
        reranker_model: str | None = None
        if self.reranker is not None and hits:
            hits, reranker_model = self._rerank(query, texts, hits)
            mode = "hybrid_reranked" if mode == "hybrid" else "lexical_reranked"
        return MemorySearchResult(
            query=query,
            commit=commit,
            retrieval_mode=mode,
            dense_model=dense_model,
            reranker_model=reranker_model,
            degraded_reason=degraded,
            hits=hits[:top_k],
        )

    def read_memory(
        self, memory_id: UUID, *, include_inactive: bool = False, actor: str | None = None
    ) -> MemoryRecord:
        """Read one record; the store denies anything outside the actor scope."""

        return self.store.get(memory_id, actor=actor, include_inactive=include_inactive)

    def get_prior_findings(
        self,
        path: str,
        symbol: str | None = None,
        *,
        scope: MemoryScope | None = None,
        commit: str | None = None,
        top_k: int = 8,
        actor: str | None = None,
    ) -> MemorySearchResult:
        """Prior findings whose evidence points at a path or whose subject names a symbol."""

        path = _normalize_path(path)
        top_k = _bound(top_k)
        symbol = None if symbol is None or not symbol.strip() else _require_query(symbol)
        _require_scope(scope, commit)
        query = path if symbol is None else f"{path} {symbol}"
        candidates = min(_TOP_K_MAX, max(top_k * 4, top_k))
        result = self.search_memory(
            query,
            scope=scope,
            commit=commit,
            top_k=candidates,
            actor=actor,
        )
        hits = [hit for hit in result.hits if _matches_path(hit, path, symbol)]
        return result.model_copy(update={"hits": hits[:top_k]})

    def get_repository_knowledge(
        self,
        commit: str | None = None,
        *,
        top_k: int = 16,
        actor: str | None = None,
    ) -> MemorySearchResult:
        """Repository/project knowledge plus commit-scoped knowledge for one commit."""

        top_k = _bound(top_k)
        records = [
            record
            for record in self._load(scope=None, commit=None, memory_kinds=None, actor=actor)
            if record.scope != "commit" or (commit is not None and record.commit == commit)
        ]
        uris = self._source_uris(records)
        now = self._clock()
        hits = [
            self._hit(
                record,
                version=version_status(record, commit),
                relevance=1.0,
                lexical_score=0.0,
                dense_score=None,
                uris=uris.get(record.memory_id, []),
                now=now,
            )
            for record in records
        ]
        return MemorySearchResult(
            query="",
            commit=commit,
            retrieval_mode="structured",
            hits=self._sorted(hits)[:top_k],
        )

    def _load(
        self,
        *,
        scope: MemoryScope | None,
        commit: str | None,
        memory_kinds: Sequence[MemoryKind] | None,
        actor: str | None,
    ) -> list[MemoryRecord]:
        if scope == "commit":
            assert commit is not None
            return self.store.recall(
                actor=actor,
                memory_kinds=memory_kinds,
                scope="commit",
                commit=commit,
                limit=_CANDIDATE_LIMIT,
                include_stale=True,
            )
        return self.store.recall(
            actor=actor,
            memory_kinds=memory_kinds,
            scope=scope,
            limit=_CANDIDATE_LIMIT,
            include_stale=True,
        )

    def _source_uris(self, records: Sequence[MemoryRecord]) -> dict[UUID, list[str]]:
        evidence_ids = sorted({item for record in records for item in record.evidence_ids})
        if not evidence_ids:
            return {}
        with psycopg.connect(
            self.settings.database_url.get_secret_value(), row_factory=dict_row
        ) as conn:
            rows = conn.execute(
                "SELECT evidence_id::text AS evidence_id, source_uri FROM app.evidence "
                "WHERE evidence_id = ANY(%s::uuid[]) ORDER BY source_uri",
                ([str(item) for item in evidence_ids],),
            ).fetchall()
        mapping: dict[UUID, list[str]] = {}
        for row in rows:
            mapping.setdefault(UUID(row["evidence_id"]), []).append(str(row["source_uri"]))
        return {
            record.memory_id: sorted(
                {uri for item in record.evidence_ids for uri in mapping.get(item, [])}
            )
            for record in records
        }

    def _hit(
        self,
        record: MemoryRecord,
        *,
        version: MemoryVersionStatus,
        relevance: float,
        lexical_score: float,
        dense_score: float | None,
        uris: Sequence[str],
        now: datetime,
    ) -> MemoryHit:
        validity = source_validity(
            record.verification_status,
            evidence_count=len(record.evidence_ids),
            artifact_count=len(record.artifact_hashes),
        )
        recency = recency_weight(record.created_at, now)
        score = _final_score(
            relevance=relevance,
            confidence=record.confidence,
            validity=validity,
            recency=recency,
            version=version,
        )
        return MemoryHit(
            memory_id=record.memory_id,
            memory_kind=record.memory_kind,
            scope=record.scope,
            subject_key=record.subject_key,
            commit=record.commit,
            version_status=version,
            requires_reverification=version in {"historical", "stale"},
            status=record.status,
            verification_status=record.verification_status,
            confidence=record.confidence,
            evidence_ids=list(record.evidence_ids),
            source_uris=list(uris),
            relevance=round(relevance, 6),
            lexical_score=round(lexical_score, 6),
            dense_score=None if dense_score is None else round(dense_score, 6),
            source_validity=validity,
            recency=recency,
            score=score,
            created_at=record.created_at,
        )

    def _rerank(
        self,
        query: str,
        texts: dict[UUID, str],
        hits: list[MemoryHit],
    ) -> tuple[list[MemoryHit], str]:
        assert self.reranker is not None
        scores = self.reranker.rerank(query, [texts[hit.memory_id] for hit in hits])
        if len(scores) != len(hits) or any(not math.isfinite(float(s)) for s in scores):
            raise DomainError(
                "MEMORY_RERANK_INVALID", "reranker returned an invalid score vector", 502
            )
        best = max((max(0.0, float(score)) for score in scores), default=0.0)
        updated = []
        for hit, value in zip(hits, scores, strict=True):
            relevance = 0.0 if best <= 0 else max(0.0, float(value)) / best
            updated.append(
                hit.model_copy(
                    update={
                        "relevance": round(relevance, 6),
                        "score": _final_score(
                            relevance=relevance,
                            confidence=hit.confidence,
                            validity=hit.source_validity,
                            recency=hit.recency,
                            version=hit.version_status,
                        ),
                    }
                )
            )
        return self._sorted(updated), self.reranker.model_id

    def _sorted(self, hits: list[MemoryHit]) -> list[MemoryHit]:
        return sorted(
            hits,
            key=lambda hit: (
                -hit.score,
                _VERSION_PRIORITY[hit.version_status],
                -hit.created_at.timestamp(),
                str(hit.memory_id),
            ),
        )


def _require_query(value: str) -> str:
    text = value.strip()
    if not 1 <= len(text) <= 2_000:
        raise DomainError("MEMORY_QUERY_INVALID", "query must be 1 to 2000 characters", 400)
    return text


def _normalize_path(value: str) -> str:
    path = _require_query(value).replace("\\", "/")
    if path.startswith("./"):
        path = path[2:]
    return path.lstrip("/")


def _bound(value: int) -> int:
    if not 1 <= value <= _TOP_K_MAX:
        raise DomainError(
            "MEMORY_BOUND_INVALID", f"top_k must be between 1 and {_TOP_K_MAX}", 400
        )
    return value


def _require_scope(scope: MemoryScope | None, commit: str | None) -> None:
    if scope == "commit" and commit is None:
        raise DomainError("MEMORY_SCOPE_INVALID", "commit scope requires a commit", 400)
    if scope is not None and scope != "commit" and commit is not None:
        raise DomainError(
            "MEMORY_SCOPE_INVALID", "a commit cannot accompany a non-commit scope", 400
        )


def _document_text(record: MemoryRecord, uris: Sequence[str]) -> str:
    parts = [record.memory_kind, record.subject_key]
    detail = record.detail
    if isinstance(detail, EpisodicDetail):
        parts.append(detail.event_kind)
    elif isinstance(detail, SemanticDetail):
        parts.extend([detail.subject, detail.predicate, detail.object_value])
    elif isinstance(detail, ProceduralDetail):
        parts.append(detail.task_key)
    parts.extend(uris)
    return " ".join(parts)[:8_192]


def _lexical(query: str, texts: dict[UUID, str]) -> list[tuple[str, float]]:
    documents: list[dict[str, Any]] = []
    for memory_id, text in texts.items():
        counts = Counter(terms(text))
        documents.append(
            {
                "chunk_id": str(memory_id),
                "terms": dict(counts),
                "token_count": sum(counts.values()),
            }
        )
    return bm25(query, documents)


def _dense(query: str, texts: dict[UUID, str], embedder: Embedder) -> list[tuple[str, float]]:
    if not texts:
        return []
    vectors = embedder.embed([query, *texts.values()])
    if len(vectors) != len(texts) + 1:
        raise DomainError("MEMORY_EMBEDDING_INVALID", "embedding count does not match", 502)
    query_vector = vectors[0]
    scores = {
        str(memory_id): _cosine(query_vector, vector)
        for memory_id, vector in zip(texts.keys(), vectors[1:], strict=True)
    }
    return sorted(
        [(key, value) for key, value in scores.items() if value > 0],
        key=lambda item: (-item[1], item[0]),
    )


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right):
        raise DomainError("MEMORY_EMBEDDING_INVALID", "embedding dimensions differ", 502)
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm < 1e-12 or right_norm < 1e-12:
        return 0.0
    return dot / (left_norm * right_norm)


def _normalize(ranking: Sequence[tuple[str, float]]) -> dict[str, float]:
    best = max((score for _, score in ranking), default=0.0)
    if best <= 0:
        return {}
    return {key: max(0.0, score) / best for key, score in ranking if score > 0}


def _final_score(
    *,
    relevance: float,
    confidence: float,
    validity: float,
    recency: float,
    version: MemoryVersionStatus,
) -> float:
    return round(
        relevance
        * (0.5 + 0.5 * confidence)
        * validity
        * recency
        * _VERSION_AFFINITY[version],
        6,
    )


def _matches_path(hit: MemoryHit, path: str, symbol: str | None) -> bool:
    lowered = path.lower()
    if any(lowered in uri.replace("\\", "/").lower() for uri in hit.source_uris):
        return True
    if lowered in hit.subject_key.lower():
        return True
    return symbol is not None and symbol.lower() in hit.subject_key.lower()
