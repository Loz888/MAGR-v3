import base64
import hashlib
import hmac
import json
import threading
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID, uuid4, uuid5

from psycopg.types.json import Jsonb
from redis import Redis
from redis.exceptions import RedisError

from mgar_v3.domain import DomainError, digest
from mgar_v3.rag.domain import ACLUpdate, Search, Upload
from mgar_v3.rag.embedding import Embedder, FixtureEmbedding, SpacyEmbedding, normalize
from mgar_v3.rag.rank import PairwiseRanker, bm25, features, rrf
from mgar_v3.rag.text import frequencies, parse, terms
from mgar_v3.settings import ROOT

if TYPE_CHECKING:
    from mgar_v3.coordination.engine import Engine, Row


def plain(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


class Knowledge:
    def __init__(self, engine: "Engine"):
        self.engine = engine
        self._backend_lock = threading.Lock()
        self.embedder: Embedder | None = None
        self.ranker: PairwiseRanker | None = None

    def actor(self, actor: str | None = None) -> str:
        value = (actor or self.engine.settings.local_actor).lower()
        if value not in {a.lower() for a in self.engine.settings.allowed_actors}:
            raise DomainError("ACTOR_DENIED", "Knowledge actor is no longer authorized", 403)
        return value

    def backend(self) -> tuple[Embedder, PairwiseRanker]:
        with self._backend_lock:
            if self.embedder is None:
                self.embedder = (
                    SpacyEmbedding()
                    if self.engine.settings.rag_mode == "local"
                    else FixtureEmbedding()
                )
            if self.ranker is None:
                self.ranker = (
                    PairwiseRanker.load(ROOT / ".local/models/reranker-linear.json", self.embedder)
                    if self.embedder.mode != "fixture"
                    else PairwiseRanker.fixture(self.embedder.dimension)
                )
            return self.embedder, self.ranker

    def config(self) -> dict[str, Any]:
        embedder, ranker = self.backend()
        return {
            "embedding_model": embedder.model_id,
            "embedding_mode": embedder.mode,
            "dimension": embedder.dimension,
            "normalization": "l2",
            "distance": "cosine",
            "tokenizer": "jieba-0.42.1:hmm-off:identifiers-v1",
            "reranker": ranker.model_id,
            "reranker_mode": ranker.mode,
            "rrf_k": 60,
            "parser": "r4-text-v1",
        }

    def _document(self, conn: Any, document_id: UUID, actor: str, owner: bool = False) -> "Row":
        s = self.engine.settings
        row = conn.execute(
            "SELECT * FROM knowledge.documents WHERE document_id=%s AND project_id=%s "
            "AND repository=%s AND (owner=%s OR %s=ANY(readers))",
            (document_id, s.project_id, s.repository.lower(), actor, actor),
        ).fetchone()
        if not row or (owner and row["owner"] != actor):
            raise DomainError("NOT_FOUND", "Knowledge source was not found", 404)
        if not owner and row["status"] != "active":
            raise DomainError("KNOWLEDGE_REVOKED", "Knowledge source access was revoked", 403)
        return cast("Row", row)

    def active(self, actor: str | None = None) -> UUID | None:
        actor = self.actor(actor)
        s = self.engine.settings
        with self.engine.transaction() as conn:
            row = conn.execute(
                "SELECT snapshot_id FROM knowledge.active_snapshots WHERE project_id=%s "
                "AND repository=%s AND actor=%s",
                (s.project_id, s.repository.lower(), actor),
            ).fetchone()
            return row["snapshot_id"] if row else None

    def upload(self, body: Upload, actor: str | None = None) -> "Row":
        actor = self.actor(actor)
        s = self.engine.settings
        try:
            content = base64.b64decode(body.content_base64, validate=True)
        except (ValueError, TypeError):
            raise DomainError("DOCUMENT_ENCODING", "Document base64 is invalid", 400) from None
        if not content or len(content) > 2_000_000:
            raise DomainError("DOCUMENT_SIZE_LIMIT", "Document exceeds its byte bound", 413)
        config = self.config()
        raw_hash = self.engine.artifacts.put(content)
        with self.engine.transaction() as conn:
            conn.execute(
                (
                    "INSERT INTO app.artifacts(content_hash,byte_count) VALUES (%s,%s) ON "
                    "CONFLICT DO NOTHING"
                ),
                (raw_hash, len(content)),
            )
            conn.execute(
                (
                    "INSERT INTO "
                    "knowledge.documents(document_id,project_id,repository,owner,sourc"
                    "e_uri,title) VALUES (%s,%s,%s,%s,%s,%s) ON "
                    "CONFLICT(project_id,repository,owner,source_uri) DO NOTHING"
                ),
                (uuid4(), s.project_id, s.repository.lower(), actor, body.source_uri, body.title),
            )
            doc = conn.execute(
                "SELECT * FROM knowledge.documents WHERE project_id=%s AND repository=%s "
                "AND owner=%s AND source_uri=%s FOR UPDATE",
                (s.project_id, s.repository.lower(), actor, body.source_uri),
            ).fetchone()
            assert doc is not None
            if doc["status"] != "active":
                raise DomainError("KNOWLEDGE_REVOKED", "Restore source access before indexing")
            version = conn.execute(
                (
                    "SELECT * FROM knowledge.document_versions WHERE document_id=%s AND "
                    "source_version=%s"
                ),
                (doc["document_id"], body.source_version),
            ).fetchone()
            if version and (
                version["raw_hash"] != raw_hash or version["media_type"] != body.media_type
            ):
                raise DomainError(
                    "SOURCE_VERSION_REUSED", "A source version already has different bytes"
                )
            if version is None:
                version = conn.execute(
                    (
                        "INSERT INTO "
                        "knowledge.document_versions(version_id,document_id,source_version"
                        ",raw_hash,media_type) VALUES (%s,%s,%s,%s,%s) RETURNING *"
                    ),
                    (uuid4(), doc["document_id"], body.source_version, raw_hash, body.media_type),
                ).fetchone()
            assert version is not None
            existing = conn.execute(
                (
                    "SELECT j.* FROM knowledge.ingestion_jobs j JOIN "
                    "knowledge.snapshots k USING(snapshot_id) WHERE j.version_id=%s "
                    "AND j.actor=%s AND k.config_hash=%s AND j.status IN "
                    "('queued','indexing','succeeded') ORDER BY j.created_at DESC "
                    "LIMIT 1"
                ),
                (version["version_id"], actor, digest(config)),
            ).fetchone()
            if existing:
                return existing
            prior = self.active(actor)
            snapshot_id = uuid4()
            conn.execute(
                (
                    "INSERT INTO "
                    "knowledge.snapshots(snapshot_id,project_id,repository,actor,statu"
                    "s,config,config_hash,dimension) VALUES "
                    "(%s,%s,%s,%s,'building',%s,%s,%s)"
                ),
                (
                    snapshot_id,
                    s.project_id,
                    s.repository.lower(),
                    actor,
                    Jsonb(config),
                    digest(config),
                    config["dimension"],
                ),
            )
            result = conn.execute(
                (
                    "INSERT INTO "
                    "knowledge.ingestion_jobs(job_id,version_id,actor,project_id,snaps"
                    "hot_id,expected_snapshot_id,status) VALUES "
                    "(%s,%s,%s,%s,%s,%s,'queued') RETURNING *"
                ),
                (uuid4(), version["version_id"], actor, s.project_id, snapshot_id, prior),
            ).fetchone()
            assert result is not None
            return result

    def jobs(self, actor: str | None = None) -> list["Row"]:
        actor = self.actor(actor)
        with self.engine.transaction() as conn:
            return conn.execute(
                (
                    "SELECT j.*,v.source_version,d.document_id,d.title,d.source_uri "
                    "FROM knowledge.ingestion_jobs j JOIN knowledge.document_versions "
                    "v USING(version_id) JOIN knowledge.documents d "
                    "USING(document_id) WHERE j.project_id=%s AND j.actor=%s ORDER BY "
                    "j.created_at DESC LIMIT 50"
                ),
                (self.engine.settings.project_id, actor),
            ).fetchall()

    def index(self, job_id: UUID) -> UUID:
        with self.engine.transaction() as conn:
            job = conn.execute(
                (
                    "SELECT "
                    "j.*,v.document_id,v.raw_hash,v.media_type,k.config,k.config_hash "
                    "FROM knowledge.ingestion_jobs j JOIN knowledge.document_versions "
                    "v USING(version_id) JOIN knowledge.snapshots k "
                    "USING(snapshot_id) WHERE j.job_id=%s AND j.project_id=%s FOR "
                    "UPDATE OF j"
                ),
                (job_id, self.engine.settings.project_id),
            ).fetchone()
            if not job:
                raise DomainError("NOT_FOUND", "Index job was not found", 404)
            self.actor(job["actor"])
            self._document(conn, job["document_id"], job["actor"])
            if job["status"] == "succeeded":
                return cast(UUID, job["snapshot_id"])
            if job["status"] != "queued":
                raise DomainError("INGESTION_NOT_QUEUED", "Index job is not available")
            lease = uuid4()
            conn.execute(
                (
                    "UPDATE knowledge.ingestion_jobs SET status='indexing',lease_token=%s "
                    "WHERE job_id=%s"
                ),
                (lease, job_id),
            )
            previous = []
            if job["expected_snapshot_id"]:
                previous = conn.execute(
                    (
                        "SELECT c.*,m.embedding::text AS vector_text,v.document_id FROM "
                        "knowledge.snapshot_chunks m JOIN knowledge.chunks c "
                        "USING(chunk_id) JOIN knowledge.document_versions v "
                        "USING(version_id) JOIN knowledge.documents d USING(document_id) "
                        "WHERE m.snapshot_id=%s AND v.document_id<>%s AND "
                        "d.status='active' AND (d.owner=%s OR %s=ANY(d.readers))"
                    ),
                    (job["expected_snapshot_id"], job["document_id"], job["actor"], job["actor"]),
                ).fetchall()
        try:
            parsed = parse(self.engine.artifacts.read(job["raw_hash"]), job["media_type"])
            embedder, ranker = self.backend()
            if digest(self.config()) != job["config_hash"]:
                raise DomainError(
                    "INDEX_MODEL_CHANGED", "Index job was created for a different model"
                )
            chunks = []
            for chunk in parsed:
                content_hash = self.engine.artifacts.put(chunk.text.encode())
                chunks.append(
                    {
                        "chunk_id": uuid5(
                            job["version_id"],
                            digest({"hash": content_hash, "locator": chunk.locator}),
                        ),
                        "version_id": job["version_id"],
                        "content_hash": content_hash,
                        "text_content": chunk.text,
                        "locator": chunk.locator,
                        "terms": frequencies(chunk.text),
                        "token_count": len(terms(chunk.text)),
                    }
                )
            if len(chunks) + len(previous) > 1024:
                raise DomainError("SNAPSHOT_SIZE_LIMIT", "Snapshot exceeds 1024 chunks")
            vectors = embedder.embed([c["text_content"] for c in chunks])
            if len(vectors) != len(chunks):
                raise DomainError("EMBEDDING_COUNT", "Embedding result count differs")
            for item, vector in zip(chunks, vectors, strict=True):
                item["vector"] = normalize(vector, embedder.dimension)
            if previous:
                with self.engine.transaction() as conn:
                    old = conn.execute(
                        "SELECT config_hash FROM knowledge.snapshots WHERE snapshot_id=%s",
                        (job["expected_snapshot_id"],),
                    ).fetchone()
                reuse = old and old["config_hash"] == job["config_hash"]
                reused = (
                    [json.loads(c["vector_text"]) for c in previous]
                    if reuse
                    else embedder.embed([c["text_content"] for c in previous])
                )
                for c, v in zip(previous, reused, strict=True):
                    c["vector"] = normalize(v, embedder.dimension)
            with self.engine.transaction() as conn:
                current = conn.execute(
                    "SELECT * FROM knowledge.ingestion_jobs WHERE job_id=%s FOR UPDATE", (job_id,)
                ).fetchone()
                if (
                    not current
                    or current["status"] != "indexing"
                    or current["lease_token"] != lease
                ):
                    raise DomainError("INGESTION_FENCED", "Index job lease is no longer current")
                self._document(conn, job["document_id"], job["actor"])
                if self.active(job["actor"]) != job["expected_snapshot_id"]:
                    raise DomainError(
                        "SNAPSHOT_CONFLICT", "Active knowledge changed; submit a reviewed retry"
                    )
                for c in chunks:
                    conn.execute(
                        (
                            "INSERT INTO app.artifacts(content_hash,byte_count) VALUES (%s,%s) ON "
                            "CONFLICT DO NOTHING"
                        ),
                        (c["content_hash"], len(c["text_content"].encode())),
                    )
                    conn.execute(
                        (
                            "INSERT INTO "
                            "knowledge.chunks(chunk_id,version_id,content_hash,text_content,lo"
                            "cator,terms,token_count) VALUES (%s,%s,%s,%s,%s,%s,%s) ON "
                            "CONFLICT(chunk_id) DO NOTHING"
                        ),
                        (
                            c["chunk_id"],
                            c["version_id"],
                            c["content_hash"],
                            c["text_content"],
                            Jsonb(c["locator"]),
                            Jsonb(c["terms"]),
                            c["token_count"],
                        ),
                    )
                for c in [*previous, *chunks]:
                    conn.execute(
                        (
                            "INSERT INTO knowledge.snapshot_chunks(snapshot_id,chunk_id,embedding) "
                            "VALUES (%s,%s,%s::vector)"
                        ),
                        (job["snapshot_id"], c["chunk_id"], json.dumps(c["vector"])),
                    )
                manifest = digest(sorted(str(c["chunk_id"]) for c in [*previous, *chunks]))
                conn.execute(
                    (
                        "UPDATE knowledge.snapshots SET status='ready',manifest_hash=%s WHERE "
                        "snapshot_id=%s"
                    ),
                    (manifest, job["snapshot_id"]),
                )
                conn.execute(
                    (
                        "INSERT INTO "
                        "knowledge.active_snapshots(project_id,repository,actor,snapshot_i"
                        "d) VALUES (%s,%s,%s,%s) ON CONFLICT(project_id,repository,actor) "
                        "DO UPDATE SET snapshot_id=excluded.snapshot_id"
                    ),
                    (
                        self.engine.settings.project_id,
                        self.engine.settings.repository.lower(),
                        job["actor"],
                        job["snapshot_id"],
                    ),
                )
                conn.execute(
                    (
                        "UPDATE knowledge.ingestion_jobs SET "
                        "status='succeeded',lease_token=NULL WHERE job_id=%s"
                    ),
                    (job_id,),
                )
            return cast(UUID, job["snapshot_id"])
        except Exception as exc:
            reason = exc.code if isinstance(exc, DomainError) else "INDEXING_FAILED"
            with self.engine.transaction() as conn:
                conn.execute(
                    (
                        "UPDATE knowledge.ingestion_jobs SET "
                        "status='failed',lease_token=NULL,reason_code=%s WHERE job_id=%s "
                        "AND lease_token=%s"
                    ),
                    (reason, job_id, lease),
                )
                conn.execute(
                    (
                        "UPDATE knowledge.snapshots SET status='failed' WHERE snapshot_id=%s "
                        "AND status='building'"
                    ),
                    (job["snapshot_id"],),
                )
            if isinstance(exc, DomainError):
                raise
            raise DomainError(
                "INDEXING_FAILED", "Indexing failed; prior snapshot remains active"
            ) from None

    def eligible(self, snapshot_id: UUID, actor: str) -> tuple["Row", list["Row"], str]:
        actor = self.actor(actor)
        s = self.engine.settings
        with self.engine.transaction() as conn:
            snapshot = conn.execute(
                "SELECT * FROM knowledge.snapshots WHERE snapshot_id=%s AND project_id=%s "
                "AND repository=%s AND actor=%s",
                (snapshot_id, s.project_id, s.repository.lower(), actor),
            ).fetchone()
            if not snapshot:
                raise DomainError("NOT_FOUND", "Knowledge snapshot was not found", 404)
            if snapshot["status"] != "ready":
                raise DomainError("SNAPSHOT_NOT_READY", "Knowledge snapshot is not ready", 409)
            rows = conn.execute(
                (
                    "SELECT "
                    "c.*,v.source_version,v.raw_hash,v.document_id,d.source_uri,d.titl"
                    "e,d.acl_revision FROM knowledge.snapshot_chunks m JOIN "
                    "knowledge.chunks c USING(chunk_id) JOIN "
                    "knowledge.document_versions v USING(version_id) JOIN "
                    "knowledge.documents d USING(document_id) WHERE m.snapshot_id=%s "
                    "AND d.project_id=%s AND d.repository=%s AND d.status='active' "
                    "AND (d.owner=%s OR %s=ANY(d.readers)) ORDER BY c.chunk_id"
                ),
                (snapshot_id, s.project_id, s.repository.lower(), actor, actor),
            ).fetchall()
            scope = digest(
                {
                    "actor": actor,
                    "allowed_actors": sorted(s.allowed_actors),
                    "chunks": [(str(c["chunk_id"]), c["acl_revision"]) for c in rows],
                }
            )
            return snapshot, rows, scope

    def search(
        self, request: Search, actor: str | None = None, run_id: UUID | None = None
    ) -> dict[str, Any]:
        actor = self.actor(actor)
        snapshot_id = request.snapshot_id or self.active(actor)
        if snapshot_id is None:
            raise DomainError(
                "KNOWLEDGE_NOT_INDEXED", "No searchable knowledge snapshot is active", 409
            )
        snapshot, rows, scope = self.eligible(snapshot_id, actor)
        embedder, ranker = self.backend()
        if snapshot["config_hash"] != digest(self.config()):
            raise DomainError(
                "RETRIEVAL_MODEL_MISMATCH", "Query model differs from the fixed knowledge snapshot"
            )
        if request.source_versions:
            rows = [r for r in rows if r["source_version"] in request.source_versions]
        by_id = {str(row["chunk_id"]): row for row in rows}
        request_hash = digest(request.model_dump(mode="json"))
        key = "mgar:rag:" + digest(
            {
                "request": request_hash,
                "snapshot": str(snapshot_id),
                "scope": scope,
                "config": snapshot["config_hash"],
            }
        )
        cache_state = "miss"
        cached = None
        try:
            with Redis.from_url(
                self.engine.settings.redis_url.get_secret_value(),
                socket_timeout=0.3,
                socket_connect_timeout=0.3,
            ) as redis:
                raw = redis.get(key)
                if raw:
                    envelope = json.loads(raw)
                    value = envelope["value"]
                    signature = hmac.new(
                        self.engine.settings.local_token.get_secret_value().encode(),
                        digest(value).encode(),
                        hashlib.sha256,
                    ).hexdigest()
                    if (
                        hmac.compare_digest(envelope["signature"], signature)
                        and value["scope_hash"] == scope
                        and value["config_hash"] == snapshot["config_hash"]
                        and all(
                            c["chunk_id"] in by_id
                            and c["content_hash"] == by_id[c["chunk_id"]]["content_hash"]
                            for c in value["hits"]
                        )
                    ):
                        cached = value
                        cache_state = "hit"
        except (RedisError, OSError, ValueError, KeyError, TypeError):
            cache_state = "unavailable"
        if cached is None:
            qvec = embedder.embed([request.query])[0]
            with self.engine.transaction() as conn:
                # MATERIALIZED enforces authorization/snapshot filtering before vector distance.
                vector = conn.execute(
                    (
                        "WITH permitted AS MATERIALIZED (SELECT m.chunk_id,m.embedding "
                        "FROM knowledge.snapshot_chunks m WHERE m.snapshot_id=%s AND "
                        "m.chunk_id=ANY(%s)) SELECT chunk_id,1-(embedding<=>%s::vector) "
                        "AS similarity,embedding::text AS vector_text FROM permitted "
                        "ORDER BY embedding<=>%s::vector,chunk_id LIMIT %s"
                    ),
                    (
                        snapshot_id,
                        [r["chunk_id"] for r in rows],
                        json.dumps(qvec),
                        json.dumps(qvec),
                        request.candidate_k,
                    ),
                ).fetchall()
                all_vectors = conn.execute(
                    "SELECT chunk_id,embedding::text AS vector_text FROM knowledge.snapshot_chunks "
                    "WHERE snapshot_id=%s AND chunk_id=ANY(%s)",
                    (snapshot_id, [r["chunk_id"] for r in rows]),
                ).fetchall()
            vectors = {str(r["chunk_id"]): json.loads(r["vector_text"]) for r in all_vectors}
            vector_scores = [(str(r["chunk_id"]), float(r["similarity"])) for r in vector]
            lexical = bm25(request.query, rows)[: request.candidate_k]
            fused = rrf(vector_scores, lexical)
            ranked = []
            for chunk_id, fused_score in fused:
                row = by_id[chunk_id]
                score = ranker.score(
                    features(request.query, row["text_content"], qvec, vectors[chunk_id])
                )
                ranked.append((chunk_id, score, fused_score))
            ranked.sort(key=lambda x: (-x[1], -x[2], x[0]))
            hits = []
            remaining = request.max_context_bytes
            for chunk_id, score, fused_score in ranked[: request.top_k]:
                row = by_id[chunk_id]
                snippet = (
                    row["text_content"]
                    .encode()[: min(768, remaining)]
                    .decode("utf-8", errors="ignore")
                )
                if not snippet:
                    break
                remaining -= len(snippet.encode())
                hits.append(
                    plain(
                        {
                            k: row[k]
                            for k in [
                                "chunk_id",
                                "document_id",
                                "version_id",
                                "content_hash",
                                "source_uri",
                                "source_version",
                                "locator",
                                "title",
                            ]
                        }
                    )
                    | {
                        "snippet": snippet,
                        "rerank_score": score,
                        "rrf_score": fused_score,
                        "read": False,
                    }
                )
            cached = {
                "scope_hash": scope,
                "config_hash": snapshot["config_hash"],
                "hits": hits,
                "snapshot_id": str(snapshot_id),
                "observed_at": snapshot["created_at"].isoformat(),
                "embedding_mode": embedder.mode,
                "reranker_mode": ranker.mode,
                "vector_ranking": vector_scores,
                "bm25_ranking": lexical,
            }
            try:
                with Redis.from_url(
                    self.engine.settings.redis_url.get_secret_value(),
                    socket_timeout=0.3,
                    socket_connect_timeout=0.3,
                ) as redis:
                    signature = hmac.new(
                        self.engine.settings.local_token.get_secret_value().encode(),
                        digest(cached).encode(),
                        hashlib.sha256,
                    ).hexdigest()
                    redis.setex(
                        key,
                        600,
                        json.dumps({"value": cached, "signature": signature}, ensure_ascii=False),
                    )
            except (RedisError, OSError):
                cache_state = "unavailable"
        if self.eligible(snapshot_id, actor)[2] != scope:
            raise DomainError(
                "KNOWLEDGE_SCOPE_CHANGED", "Knowledge permissions changed during search"
            )
        trace_id = uuid4()
        with self.engine.transaction() as conn:
            conn.execute(
                (
                    "INSERT INTO "
                    "knowledge.search_traces(trace_id,run_id,actor,snapshot_id,request"
                    "_hash,scope_hash,config_hash,cache_state,results) VALUES "
                    "(%s,%s,%s,%s,%s,%s,%s,%s,%s)"
                ),
                (
                    trace_id,
                    run_id,
                    actor,
                    snapshot_id,
                    request_hash,
                    scope,
                    snapshot["config_hash"],
                    cache_state,
                    Jsonb(cached),
                ),
            )
        return {**cached, "cache_state": cache_state, "trace_id": str(trace_id)}

    def read(self, chunk_id: UUID, snapshot_id: UUID, actor: str | None = None) -> "Row":
        actor = self.actor(actor)
        _, rows, _ = self.eligible(snapshot_id, actor)
        row = next((r for r in rows if r["chunk_id"] == chunk_id), None)
        if not row:
            raise DomainError("NOT_FOUND", "Knowledge chunk is not readable in this snapshot", 404)
        content = self.engine.artifacts.read(row["content_hash"])
        raw = self.engine.artifacts.read(row["raw_hash"])
        with self.engine.transaction() as conn:
            version = conn.execute(
                "SELECT media_type FROM knowledge.document_versions WHERE version_id=%s",
                (row["version_id"],),
            ).fetchone()
        assert version is not None
        if not any(
            c.text.encode() == content and c.locator == row["locator"]
            for c in parse(raw, version["media_type"])
        ):
            raise DomainError(
                "KNOWLEDGE_SOURCE_MISMATCH", "Chunk no longer matches its source locator"
            )
        return {**row, "text": content.decode(), "snapshot_id": snapshot_id}

    def acl(self, document_id: UUID, body: ACLUpdate) -> "Row":
        actor = self.actor()
        readers = sorted(set(x.lower() for x in body.readers))
        if any(r not in {a.lower() for a in self.engine.settings.allowed_actors} for r in readers):
            raise DomainError(
                "ACL_READER_DENIED", "Reader is outside the configured project actors", 403
            )
        with self.engine.transaction() as conn:
            self._document(conn, document_id, actor, owner=True)
            result = conn.execute(
                "UPDATE knowledge.documents SET readers=%s,status=%s,acl_revision=acl_revision+1 "
                "WHERE document_id=%s RETURNING *",
                (readers, "revoked" if body.revoked else "active", document_id),
            ).fetchone()
            assert result is not None
            return result

    def documents(self) -> list["Row"]:
        actor = self.actor()
        s = self.engine.settings
        with self.engine.transaction() as conn:
            return conn.execute(
                (
                    "SELECT d.*,v.version_id,v.source_version,v.media_type,v.raw_hash "
                    "FROM knowledge.documents d LEFT JOIN knowledge.document_versions "
                    "v USING(document_id) WHERE d.project_id=%s AND d.repository=%s "
                    "AND (d.owner=%s OR %s=ANY(d.readers)) ORDER BY d.created_at "
                    "DESC,v.created_at DESC LIMIT 128"
                ),
                (s.project_id, s.repository.lower(), actor, actor),
            ).fetchall()

    def recover(self) -> None:
        with self.engine.transaction() as conn:
            conn.execute(
                "UPDATE knowledge.ingestion_jobs SET "
                "status='queued',lease_token=NULL,reason_code='PROCESS_RESTARTED' "
                "WHERE status='indexing'"
            )
