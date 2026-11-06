import base64
import hashlib
import hmac
import json
import time
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from mgar_v3.domain import DomainError

if TYPE_CHECKING:
    from mgar_v3.coordination.engine import Engine

KINDS = {"ticket", "run", "evidence", "chunk", "memory"}
RELATIONS = {
    "triggered_by",
    "part_of",
    "attempt_of",
    "derived_from",
    "cites",
    "supersedes",
    "related_to",
}


class ContextStore:
    def __init__(self, engine: "Engine"):
        self.engine = engine

    def resolve(self, kind: str, object_id: UUID, actor: str | None = None) -> dict[str, Any]:
        actor = self.engine.knowledge.actor(actor)
        if kind not in KINDS:
            raise DomainError("CONTEXT_KIND_DENIED", "Context reference kind is unsupported", 400)
        if kind == "ticket":
            ticket = self.engine.routing.owned_ticket(object_id, actor=actor)
            return {
                "kind": kind,
                "id": str(object_id),
                "summary": ticket["question"],
                "run_id": str(ticket["run_id"]),
            }
        if kind == "evidence":
            with self.engine.transaction() as conn:
                row = conn.execute(
                    "SELECT * FROM app.evidence WHERE evidence_id=%s", (object_id,)
                ).fetchone()
                if not row:
                    raise DomainError("NOT_FOUND", "Evidence reference was not found", 404)
                run = self.engine._run(conn, row["run_id"])
                self.engine.routing.owned_ticket(run["ticket_id"], actor=actor)
            # Evidence verification reads CAS and knowledge bytes outside the business TX.
            self.engine.tools.check_evidence(object_id, actor)
            return {
                "kind": kind,
                "id": str(object_id),
                "summary": row["source_uri"],
                "artifact_hash": row["content_hash"],
                "locator": {"start_line": row["start_line"], "end_line": row["end_line"]},
                "source_version": row["source_version"],
            }
        with self.engine.transaction() as conn:
            if kind == "run":
                run = self.engine._run(conn, object_id)
                self.engine.routing.owned_ticket(run["ticket_id"], actor=actor)
                return {
                    "kind": kind,
                    "id": str(object_id),
                    "summary": run["question"][:512],
                    "report_hash": run["report_hash"],
                }
            if kind == "chunk":
                membership = conn.execute(
                    (
                        "SELECT k.snapshot_id FROM knowledge.snapshot_chunks m JOIN "
                        "knowledge.snapshots k USING(snapshot_id) WHERE m.chunk_id=%s AND "
                        "k.actor=%s AND k.project_id=%s AND k.repository=%s AND "
                        "k.status='ready' ORDER BY k.created_at DESC LIMIT 1"
                    ),
                    (
                        object_id,
                        actor,
                        self.engine.settings.project_id,
                        self.engine.settings.repository.lower(),
                    ),
                ).fetchone()
                if not membership:
                    raise DomainError("NOT_FOUND", "Chunk reference was not found", 404)
                chunk = self.engine.knowledge.read(object_id, membership["snapshot_id"], actor)
                return {
                    "kind": kind,
                    "id": str(object_id),
                    "summary": chunk["text"][:256],
                    "artifact_hash": chunk["content_hash"],
                    "locator": chunk["locator"],
                    "snapshot_id": str(membership["snapshot_id"]),
                    "source_version": chunk["source_version"],
                }
            memory = conn.execute(
                (
                    "SELECT * FROM knowledge.memories WHERE memory_id=%s AND "
                    "project_id=%s AND repository=%s AND actor=%s AND revoked_at IS "
                    "NULL"
                ),
                (
                    object_id,
                    self.engine.settings.project_id,
                    self.engine.settings.repository.lower(),
                    actor,
                ),
            ).fetchone()
            if not memory:
                raise DomainError("NOT_FOUND", "Confirmed memory was not found", 404)
            self.engine.routing.owned_ticket(memory["ticket_id"], actor=actor)
            for evidence_id in memory["evidence_ids"]:
                self.engine.tools.check_evidence(UUID(evidence_id), actor)
            text = self.engine.artifacts.read(memory["summary_hash"]).decode()
            return {
                "kind": kind,
                "id": str(object_id),
                "summary": text[:512],
                "artifact_hash": memory["summary_hash"],
                "source_ticket_id": str(memory["ticket_id"]),
                "confirmed_at": memory["confirmed_at"].isoformat(),
            }

    def add_edge(
        self,
        source_kind: str,
        source_id: UUID,
        target_kind: str,
        target_id: UUID,
        relation: str,
        actor: str | None = None,
        run_id: UUID | None = None,
    ) -> UUID:
        actor = self.engine.knowledge.actor(actor)
        if relation not in RELATIONS:
            raise DomainError("CONTEXT_RELATION_DENIED", "Context edge relation is unsupported")
        self.resolve(source_kind, source_id, actor)
        self.resolve(target_kind, target_id, actor)
        if run_id:
            with self.engine.transaction() as conn:
                run = self.engine._run(conn, run_id)
                self.engine.routing.owned_ticket(run["ticket_id"], actor=actor)
        with self.engine.transaction() as conn:
            value = conn.execute(
                (
                    "INSERT INTO "
                    "knowledge.context_edges(edge_id,project_id,repository,actor,sourc"
                    "e_kind,source_id, target_kind,target_id,relation,run_id) VALUES "
                    "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON "
                    "CONFLICT(source_kind,source_id,target_kind,target_id,relation) "
                    "DO UPDATE SET relation=excluded.relation RETURNING edge_id"
                ),
                (
                    uuid4(),
                    self.engine.settings.project_id,
                    self.engine.settings.repository.lower(),
                    actor,
                    source_kind,
                    source_id,
                    target_kind,
                    target_id,
                    relation,
                    run_id,
                ),
            ).fetchone()
            assert value is not None
            return cast(UUID, value["edge_id"])

    def recall(
        self,
        kind: str,
        object_id: UUID,
        actor: str | None = None,
        *,
        max_hops: int = 2,
        max_nodes: int = 12,
        max_bytes: int = 8192,
    ) -> dict[str, Any]:
        actor = self.engine.knowledge.actor(actor)
        if not 0 <= max_hops <= 3 or not 1 <= max_nodes <= 16 or not 512 <= max_bytes <= 16384:
            raise DomainError("CONTEXT_BOUND", "Context traversal bounds are invalid", 400)
        started = time.monotonic()
        pending = [(kind, object_id, 0)]
        visited = set()
        items: list[dict[str, Any]] = []
        size = 0
        while pending and len(items) < max_nodes:
            if time.monotonic() - started > 0.75:
                raise DomainError("CONTEXT_DEADLINE", "Context traversal deadline exceeded")
            current, current_id, depth = pending.pop(0)
            key = (current, current_id)
            if key in visited:
                continue
            visited.add(key)
            # No edge grants access: the actual target is authorized again on every hop.
            item = self.resolve(current, current_id, actor)
            encoded = len(json.dumps(item, ensure_ascii=False).encode())
            if size + encoded > max_bytes:
                break
            size += encoded
            items.append({**item, "depth": depth})
            if depth < max_hops:
                with self.engine.transaction() as conn:
                    edges = conn.execute(
                        (
                            "SELECT target_kind,target_id FROM knowledge.context_edges WHERE "
                            "project_id=%s AND repository=%s AND actor=%s AND source_kind=%s "
                            "AND source_id=%s ORDER BY edge_id LIMIT 16"
                        ),
                        (
                            self.engine.settings.project_id,
                            self.engine.settings.repository.lower(),
                            actor,
                            current,
                            current_id,
                        ),
                    ).fetchall()
                pending.extend(
                    (edge["target_kind"], edge["target_id"], depth + 1) for edge in edges
                )
        return {"items": items, "bytes": size, "truncated": bool(pending), "max_hops": max_hops}

    def _encode(self, value: dict[str, Any]) -> str:
        encoded = (
            base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode())
            .decode()
            .rstrip("=")
        )
        signature = hmac.new(
            self.engine.settings.local_token.get_secret_value().encode(),
            encoded.encode(),
            hashlib.sha256,
        ).hexdigest()
        return encoded + "." + signature

    def page(
        self,
        kind: str,
        object_id: UUID,
        actor: str | None = None,
        cursor: str | None = None,
        max_bytes: int = 2048,
    ) -> dict[str, Any]:
        actor = self.engine.knowledge.actor(actor)
        if not 128 <= max_bytes <= 4096:
            raise DomainError("CONTEXT_BOUND", "Page size must be 128 to 4096 bytes", 400)
        reference = self.resolve(kind, object_id, actor)
        content_hash = reference.get("artifact_hash")
        if not content_hash:
            return {
                **reference,
                "text": reference["summary"],
                "continuation": None,
                "full_read": True,
            }
        offset = 0
        if cursor:
            try:
                encoded, signature = cursor.split(".", 1)
                expected = hmac.new(
                    self.engine.settings.local_token.get_secret_value().encode(),
                    encoded.encode(),
                    hashlib.sha256,
                ).hexdigest()
                if not hmac.compare_digest(signature, expected):
                    raise ValueError("signature")
                value = json.loads(base64.urlsafe_b64decode(encoded + "=" * ((-len(encoded)) % 4)))
                if (
                    value["actor"] != actor
                    or value["project"] != self.engine.settings.project_id
                    or value["kind"] != kind
                    or value["id"] != str(object_id)
                    or value["hash"] != content_hash
                    or value["expires"] < time.time()
                ):
                    raise ValueError("scope")
                offset = value["offset"]
                if type(offset) is not int or offset < 0:
                    raise ValueError("offset")
            except (ValueError, TypeError, KeyError):
                raise DomainError(
                    "CONTEXT_CURSOR_INVALID", "Continuation token is invalid", 403
                ) from None
        raw = self.engine.artifacts.read(content_hash)
        if offset > len(raw):
            raise DomainError(
                "CONTEXT_CURSOR_INVALID", "Continuation offset is outside source", 403
            )
        end = min(len(raw), offset + max_bytes)
        while end > offset:
            try:
                text = raw[offset:end].decode("utf-8")
                break
            except UnicodeDecodeError:
                end -= 1
        else:
            raise DomainError("CONTEXT_BINARY_UNSUPPORTED", "Context is not readable UTF-8")
        continuation = None
        if end < len(raw):
            continuation = self._encode(
                {
                    "actor": actor,
                    "project": self.engine.settings.project_id,
                    "kind": kind,
                    "id": str(object_id),
                    "hash": content_hash,
                    "offset": end,
                    "expires": int(time.time()) + 600,
                }
            )
        return {
            **reference,
            "text": text,
            "offset": offset,
            "end_offset": end,
            "continuation": continuation,
            "full_read": end == len(raw) and offset == 0,
        }

    def preview_memory(self, ticket_id: UUID) -> dict[str, Any]:
        ticket = self.engine.routing.owned_ticket(ticket_id)
        if ticket["ticket_status"] != "ended":
            raise DomainError("MEMORY_TICKET_OPEN", "Only ended Tickets can become memory")
        resolution = self.engine.tools.resolution(ticket["run_id"])
        if not resolution or not resolution["verdict"]["passed"]:
            raise DomainError("MEMORY_UNVERIFIED", "Ticket has no mechanically verified R4 report")
        summary = "\n".join(f["text"] for f in resolution["payload"]["verified_facts"])[:2000]
        if not summary:
            raise DomainError("MEMORY_EMPTY", "There are no verified observations to confirm")
        content_hash = self.engine.artifacts.put(summary.encode())
        return {
            "ticket_id": str(ticket_id),
            "run_id": str(ticket["run_id"]),
            "report_hash": resolution["report_hash"],
            "summary_hash": content_hash,
            "summary": summary,
            "evidence_ids": sorted(
                {f["evidence_id"] for f in resolution["payload"]["verified_facts"]}
            ),
        }

    def confirm(self, ticket_id: UUID, summary_hash: str, report_hash: str) -> dict[str, Any]:
        actor = self.engine.knowledge.actor()
        preview = self.preview_memory(ticket_id)
        if preview["summary_hash"] != summary_hash or preview["report_hash"] != report_hash:
            raise DomainError("MEMORY_CONFIRMATION_CHANGED", "Memory preview or report changed")
        with self.engine.transaction() as conn:
            conn.execute(
                (
                    "INSERT INTO app.artifacts(content_hash,byte_count) VALUES (%s,%s) ON "
                    "CONFLICT DO NOTHING"
                ),
                (summary_hash, len(preview["summary"].encode())),
            )
            row = conn.execute(
                (
                    "INSERT INTO "
                    "knowledge.memories(memory_id,project_id,repository,actor,ticket_i"
                    "d,run_id,report_hash,summary_hash,evidence_ids,confirmed_by) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON "
                    "CONFLICT(ticket_id,report_hash,summary_hash) DO UPDATE SET "
                    "confirmed_by=knowledge.memories.confirmed_by RETURNING *"
                ),
                (
                    uuid4(),
                    self.engine.settings.project_id,
                    self.engine.settings.repository.lower(),
                    actor,
                    ticket_id,
                    UUID(preview["run_id"]),
                    report_hash,
                    summary_hash,
                    Jsonb(preview["evidence_ids"]),
                    actor,
                ),
            ).fetchone()
            assert row is not None
            self.add_edge("memory", row["memory_id"], "ticket", ticket_id, "derived_from", actor)
            self.add_edge("ticket", ticket_id, "memory", row["memory_id"], "related_to", actor)
            for evidence in preview["evidence_ids"]:
                self.add_edge(
                    "memory", row["memory_id"], "evidence", UUID(evidence), "cites", actor
                )
        return {**row, "summary": preview["summary"]}

    def memories(self) -> list[dict[str, Any]]:
        actor = self.engine.knowledge.actor()
        with self.engine.transaction() as conn:
            rows = conn.execute(
                (
                    "SELECT memory_id FROM knowledge.memories WHERE project_id=%s AND "
                    "repository=%s AND actor=%s AND revoked_at IS NULL ORDER BY "
                    "confirmed_at DESC LIMIT 32"
                ),
                (self.engine.settings.project_id, self.engine.settings.repository.lower(), actor),
            ).fetchall()
        results = []
        for row in rows:
            try:
                results.append(self.resolve("memory", row["memory_id"], actor))
            except DomainError:
                continue
        return results

    def revoke(self, memory_id: UUID) -> None:
        self.resolve("memory", memory_id)
        with self.engine.transaction() as conn:
            conn.execute(
                "UPDATE knowledge.memories SET revoked_at=now() WHERE memory_id=%s", (memory_id,)
            )
