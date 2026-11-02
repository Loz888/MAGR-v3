import hashlib
import json
import secrets
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from mgar_v3.agents.data import DataReader
from mgar_v3.domain import DomainError, Evidence, digest
from mgar_v3.rag.domain import Search
from mgar_v3.settings import ROOT

if TYPE_CHECKING:
    from mgar_v3.coordination.engine import Engine, Row

TOOLS = {
    "knowledge": {"search_knowledge", "read_knowledge", "recall_memory"},
    "repo": {"read_repository"},
    "data": {"describe_tables", "query_readonly"},
}


class ToolAuthority:
    def __init__(self, engine: "Engine"):
        self.engine = engine

    def requested_snapshots(self, run_id: UUID, ids: list[UUID], duplicate: bool = False) -> None:
        with self.engine.transaction() as conn:
            run = self.engine._run(conn, run_id)
            actor = conn.execute(
                "SELECT actor FROM app.mandates WHERE mandate_id=%s", (run["mandate_id"],)
            ).fetchone()
            assert actor is not None
            for snapshot_id in ids:
                self.engine.knowledge.eligible(snapshot_id, actor["actor"])
            values = [str(i) for i in ids]
            if duplicate and values != run["r4_requested_snapshots"]:
                raise DomainError(
                    "EVENT_ID_REUSED", "Duplicate request has different fixed knowledge snapshots"
                )
            if not duplicate:
                conn.execute(
                    "UPDATE app.runs SET r4_requested_snapshots=%s WHERE run_id=%s",
                    (Jsonb(values), run_id),
                )

    def bind_context(self, run_id: UUID, epoch: int, revision: int) -> dict[str, Any]:
        with self.engine.transaction() as conn:
            run = self.engine._run(conn, run_id, epoch)
            if run["status"] != "running":
                raise DomainError("FENCED", "Context requires the running execution")
            existing = conn.execute(
                "SELECT * FROM app.r4_context WHERE run_id=%s AND input_revision=%s",
                (run_id, revision),
            ).fetchone()
            if existing:
                return existing
            mandate = conn.execute(
                "SELECT actor FROM app.mandates WHERE mandate_id=%s", (run["mandate_id"],)
            ).fetchone()
            assert mandate is not None
            actor = mandate["actor"]
            self.engine.knowledge.actor(actor)
            snapshots = [UUID(value) for value in run["r4_requested_snapshots"]]
            if not snapshots:
                active = self.engine.knowledge.active(actor)
                if active:
                    snapshots = [active]
            for snapshot_id in snapshots:
                self.engine.knowledge.eligible(snapshot_id, actor)
            profile = {
                "actor": actor,
                "project_id": run["project_id"],
                "repository": run["repository"],
                "max_tool_calls": 36,
                "max_rounds": 2,
                "deadline": (datetime.now(UTC) + timedelta(seconds=90)).isoformat(),
                "repo_mode": run["execution_mode"],
                "sql_profile": None,
                "knowledge_modes": [],
            }
            try:
                reader = self.reader(actor)
                profile["sql_profile"] = {
                    k: v for k, v in reader.profile.items() if k != "database_url"
                }
            except DomainError as exc:
                if exc.code != "DATA_SOURCE_NOT_CONFIGURED":
                    raise
            profile["repository_hash"] = (
                hashlib.sha256((ROOT / "fixtures/orders/service.py").read_bytes()).hexdigest()
                if run["execution_mode"] == "fixture"
                else None
            )
            for snapshot_id in snapshots:
                k, _, _ = self.engine.knowledge.eligible(snapshot_id, actor)
                profile["knowledge_modes"].append(k["config"])
            row = conn.execute(
                (
                    "INSERT INTO "
                    "app.r4_context(run_id,input_revision,knowledge_snapshot_ids,profi"
                    "le,profile_hash) VALUES (%s,%s,%s,%s,%s) RETURNING *"
                ),
                (
                    run_id,
                    revision,
                    Jsonb([str(x) for x in snapshots]),
                    Jsonb(profile),
                    digest(profile),
                ),
            ).fetchone()
            assert row is not None
            return row

    def issue(self, run_id: UUID, epoch: int, task_id: str, role: str, revision: int) -> str:
        if role not in TOOLS:
            raise DomainError("TOOL_ROLE_DENIED", "Expert role has no tool capability", 403)
        context = self.bind_context(run_id, epoch, revision)
        if datetime.fromisoformat(context["profile"]["deadline"]) <= datetime.now(UTC):
            raise DomainError("INVESTIGATION_DEADLINE", "Investigation deadline is exhausted")
        token = secrets.token_urlsafe(32)
        with self.engine.transaction() as conn:
            run = self.engine._run(conn, run_id, epoch)
            if run["status"] != "running":
                raise DomainError("FENCED", "Cannot issue tools after the execution stopped")
            existing = conn.execute(
                "SELECT remaining_calls FROM app.tool_grants WHERE run_id=%s AND task_id=%s "
                "AND execution_epoch=%s",
                (run_id, task_id, epoch),
            ).fetchone()
            remaining = existing["remaining_calls"] if existing else 12
            conn.execute(
                "DELETE FROM app.tool_grants WHERE run_id=%s AND task_id=%s AND execution_epoch=%s",
                (run_id, task_id, epoch),
            )
            scope = {
                "snapshot_ids": context["knowledge_snapshot_ids"],
                "input_revision": revision,
                "profile_hash": context["profile_hash"],
            }
            conn.execute(
                (
                    "INSERT INTO "
                    "app.tool_grants(token_hash,run_id,task_id,role,execution_epoch,ac"
                    "tor,scope,remaining_calls,expires_at) VALUES "
                    "(%s,%s,%s,%s,%s,%s,%s,%s,%s)"
                ),
                (
                    hashlib.sha256(token.encode()).hexdigest(),
                    run_id,
                    task_id,
                    role,
                    epoch,
                    context["profile"]["actor"],
                    Jsonb(scope),
                    remaining,
                    datetime.fromisoformat(context["profile"]["deadline"]),
                ),
            )
            self.engine._event(conn, run_id, "expert_tools_issued", task_id=task_id, role=role)
        return token

    def _grant(
        self, token: str, tool: str, expected_role: str | None = None, charge: bool = True
    ) -> "Row":
        if not 32 <= len(token) <= 128:
            raise DomainError("TOOL_AUTH_REQUIRED", "A bounded tool token is required", 401)
        with self.engine.transaction() as conn:
            grant = conn.execute(
                "SELECT * FROM app.tool_grants WHERE token_hash=%s FOR UPDATE",
                (hashlib.sha256(token.encode()).hexdigest(),),
            ).fetchone()
            if not grant:
                raise DomainError("TOOL_AUTH_REQUIRED", "Tool capability is unknown", 401)
            run = self.engine._run(conn, grant["run_id"], grant["execution_epoch"])
            if run["status"] != "running" or grant["expires_at"] <= datetime.now(UTC):
                raise DomainError(
                    "TOOL_GRANT_EXPIRED", "Tool capability expired or execution stopped", 403
                )
            self.engine.knowledge.actor(grant["actor"])
            if tool not in TOOLS.get(grant["role"], set()) or (
                expected_role is not None and expected_role != grant["role"]
            ):
                raise DomainError("TOOL_ROLE_DENIED", "Expert cannot use this tool", 403)
            if charge:
                budget = conn.execute(
                    (
                        "UPDATE app.r4_context SET tool_calls_used=tool_calls_used+1 "
                        "WHERE run_id=%s AND input_revision=%s AND "
                        "tool_calls_used<(profile->>'max_tool_calls')::int RETURNING "
                        "run_id"
                    ),
                    (grant["run_id"], grant["scope"]["input_revision"]),
                ).fetchone()
                if budget is None or grant["remaining_calls"] <= 0:
                    raise DomainError(
                        "TOOL_BUDGET_EXHAUSTED", "Investigation readonly tool budget is exhausted"
                    )
                conn.execute(
                    (
                        "UPDATE app.tool_grants SET remaining_calls=remaining_calls-1 WHERE "
                        "token_hash=%s"
                    ),
                    (grant["token_hash"],),
                )
            return {
                **grant,
                "project_id": run["project_id"],
                "repository": run["repository"],
                "execution_mode": run["execution_mode"],
            }

    def reader(self, actor: str) -> DataReader:
        path = self.engine.settings.data_profile_path
        if not path.is_file():
            raise DomainError(
                "DATA_SOURCE_NOT_CONFIGURED", "Initialize an independent data reader first", 409
            )
        try:
            profile = json.loads(path.read_text())
        except (ValueError, OSError):
            raise DomainError(
                "DATA_PROFILE_INVALID", "Data reader configuration is unavailable"
            ) from None
        if (
            profile.get("project_id") != self.engine.settings.project_id
            or profile.get("repository") != self.engine.settings.repository.lower()
            or profile.get("actor") != actor
        ):
            raise DomainError("DATA_SCOPE_DENIED", "Data source is outside this task scope", 403)
        return DataReader(profile)

    def record(
        self, grant: "Row", content: bytes, uri: str, version: str, origin: dict[str, Any]
    ) -> dict[str, Any]:
        evidence = Evidence(
            evidence_id=uuid4(),
            run_id=grant["run_id"],
            project_id=grant["project_id"],
            source_uri=uri,
            source_version=version,
            content_hash=hashlib.sha256(content).hexdigest(),
            start_line=1,
            end_line=max(1, len(content.decode().splitlines())),
            tool_call_id=uuid4(),
        )
        self.engine.record_read(
            evidence,
            grant["execution_epoch"],
            content,
            r4_origin={"role": grant["role"], "task_id": grant["task_id"], "origin": origin},
        )
        return {**evidence.model_dump(mode="json"), "origin": origin, "content": content.decode()}

    def call(
        self, token: str, tool: str, arguments: dict[str, Any], expected_role: str | None = None
    ) -> dict[str, Any]:
        grant = self._grant(token, tool, expected_role)
        if tool == "recall_memory":
            if arguments:
                raise DomainError("TOOL_ARGUMENTS", "Memory recall accepts no source identity")
            # Confirmed history is background context, not a new verified fact.
            return {"items": self.engine.context.memories(), "use": "background_only"}
        if tool == "search_knowledge":
            request = Search.model_validate(arguments)
            if (
                request.snapshot_id is None
                or str(request.snapshot_id) not in grant["scope"]["snapshot_ids"]
            ):
                raise DomainError(
                    "TOOL_SNAPSHOT_DENIED", "Search snapshot is not in the fixed task scope", 403
                )
            return self.engine.knowledge.search(request, grant["actor"], grant["run_id"])
        if tool == "read_knowledge":
            if set(arguments) != {"chunk_id", "snapshot_id"}:
                raise DomainError("TOOL_ARGUMENTS", "Knowledge read arguments are invalid")
            snapshot_id = UUID(arguments["snapshot_id"])
            chunk_id = UUID(arguments["chunk_id"])
            if str(snapshot_id) not in grant["scope"]["snapshot_ids"]:
                raise DomainError("TOOL_SNAPSHOT_DENIED", "Read snapshot is outside this task", 403)
            chunk = self.engine.knowledge.read(chunk_id, snapshot_id, grant["actor"])
            result = self.record(
                grant,
                chunk["text"].encode(),
                chunk["source_uri"],
                chunk["source_version"],
                {
                    "kind": "knowledge",
                    "chunk_id": str(chunk_id),
                    "snapshot_id": str(snapshot_id),
                    "document_id": str(chunk["document_id"]),
                    "version_id": str(chunk["version_id"]),
                    "locator": chunk["locator"],
                    "raw_hash": chunk["raw_hash"],
                },
            )
            self.engine.context.add_edge(
                "run",
                grant["run_id"],
                "evidence",
                UUID(result["evidence_id"]),
                "cites",
                grant["actor"],
                grant["run_id"],
            )
            return result
        if tool == "read_repository":
            if arguments:
                raise DomainError("TOOL_ARGUMENTS", "Repository source is fixed by the task scope")
            if grant["execution_mode"] == "fixture":
                content = (ROOT / "fixtures/orders/service.py").read_bytes()
                with self.engine.transaction() as conn:
                    fixed = conn.execute(
                        (
                            "SELECT profile FROM app.r4_context WHERE run_id=%s AND "
                            "input_revision=%s"
                        ),
                        (grant["run_id"], grant["scope"]["input_revision"]),
                    ).fetchone()
                if (
                    not fixed
                    or fixed["profile"]["repository_hash"] != hashlib.sha256(content).hexdigest()
                ):
                    raise DomainError(
                        "REPOSITORY_SOURCE_CHANGED", "Repository differs from the fixed Run source"
                    )
                uri = "fixture://orders/service.py"
                version = hashlib.sha256(content).hexdigest()
            else:
                from mgar_v3.graph.live_investigator import LiveInvestigator
                from mgar_v3.live import load_permit

                settings = self.engine.settings
                if not settings.live_profile_path or not settings.live_authorization_path:
                    raise DomainError(
                        "LIVE_PROFILE_REQUIRED",
                        "Repository live source requires its frozen profile",
                    )
                permit = load_permit(
                    settings.live_profile_path, settings.live_authorization_path, settings
                )
                content, uri, version = LiveInvestigator(
                    self.engine, grant["run_id"], permit
                ).read()
            return self.record(
                grant,
                content,
                uri,
                version,
                {"kind": "repo", "source_hash": hashlib.sha256(content).hexdigest()},
            )
        reader = self.reader(grant["actor"])
        with self.engine.transaction() as conn:
            fixed = conn.execute(
                ("SELECT profile FROM app.r4_context WHERE run_id=%s AND input_revision=%s"),
                (grant["run_id"], grant["scope"]["input_revision"]),
            ).fetchone()
        assert fixed is not None
        if fixed["profile"]["sql_profile"] != {
            k: v for k, v in reader.profile.items() if k != "database_url"
        }:
            raise DomainError(
                "DATA_SOURCE_CHANGED", "Data source differs from the fixed investigation context"
            )
        if tool == "describe_tables":
            if arguments:
                raise DomainError("TOOL_ARGUMENTS", "Describe has no identity or source parameters")
            return reader.describe()
        if set(arguments) != {"sql", "parameters"} or not isinstance(arguments["parameters"], list):
            raise DomainError("TOOL_ARGUMENTS", "Readonly query requires SQL and a parameter array")
        result = reader.query(arguments["sql"], arguments["parameters"])
        content = json.dumps(
            result, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
        return self.record(
            grant,
            content,
            "sql://" + reader.profile["schema"] + ".orders",
            reader.profile["fixture_version"],
            {
                "kind": "data",
                "fixture_hash": reader.profile["fixture_hash"],
                "fixture_version": reader.profile["fixture_version"],
                "result_hash": result["result_hash"],
            },
        )

    def check_evidence(self, evidence_id: UUID, actor: str | None = None, depth: int = 0) -> None:
        actor = self.engine.knowledge.actor(actor)
        if depth > 3:
            raise DomainError("CONTEXT_DEPTH", "Evidence provenance exceeds its depth bound")
        with self.engine.transaction() as conn:
            evidence = conn.execute(
                (
                    "SELECT e.*,r.origin,r.role FROM app.evidence e LEFT JOIN "
                    "app.r4_reads r USING(evidence_id) WHERE e.evidence_id=%s"
                ),
                (evidence_id,),
            ).fetchone()
            if not evidence:
                raise DomainError("NOT_FOUND", "Evidence was not found", 404)
            run = self.engine._run(conn, evidence["run_id"])
            self.engine.routing.owned_ticket(run["ticket_id"], actor=actor)
        origin = evidence["origin"]
        if origin and origin["kind"] == "knowledge":
            chunk = self.engine.knowledge.read(
                UUID(origin["chunk_id"]), UUID(origin["snapshot_id"]), actor
            )
            if (
                chunk["content_hash"] != evidence["content_hash"]
                or chunk["source_version"] != evidence["source_version"]
                or chunk["source_uri"] != evidence["source_uri"]
                or chunk["raw_hash"] != origin["raw_hash"]
                or chunk["locator"] != origin["locator"]
                or str(chunk["document_id"]) != origin["document_id"]
                or str(chunk["version_id"]) != origin["version_id"]
            ):
                raise DomainError(
                    "EVIDENCE_SOURCE_CHANGED", "Knowledge evidence provenance changed"
                )
        elif origin and origin["kind"] == "data":
            reader = self.reader(actor)
            if reader.profile["fixture_hash"] != origin["fixture_hash"]:
                raise DomainError(
                    "EVIDENCE_SOURCE_CHANGED", "Data source version differs from the read evidence"
                )
        self.engine.artifacts.read(evidence["content_hash"])

    def resolution(self, run_id: UUID) -> dict[str, Any] | None:
        with self.engine.transaction() as conn:
            run = self.engine._run(conn, run_id)
            self.engine.routing.owned_ticket(run["ticket_id"])
            row = conn.execute(
                (
                    "SELECT * FROM app.resolutions WHERE run_id=%s ORDER BY input_revision "
                    "DESC LIMIT 1"
                ),
                (run_id,),
            ).fetchone()
        if row:
            for fact in row["payload"]["verified_facts"]:
                self.check_evidence(UUID(fact["evidence_id"]))
        return row

    def validate_publication(self, run_id: UUID) -> None:
        from mgar_v3.agents.domain import Finding
        from mgar_v3.agents.verify import verify_findings

        self.resolution(run_id)
        with self.engine.transaction() as conn:
            run = self.engine._run(conn, run_id)
            rows = conn.execute(
                "SELECT result,task_id FROM app.expert_results WHERE run_id=%s "
                "AND input_revision=%s ORDER BY task_id",
                (run_id, run["input_revision"]),
            ).fetchall()
        latest = {}
        for row in sorted(rows, key=lambda r: int(r["task_id"].split(":")[1])):
            finding = Finding.model_validate(row["result"])
            latest[finding.role] = finding
        resolution, verdict = verify_findings(
            self.engine, run_id, list(latest.values()), ["knowledge", "repo", "data"]
        )
        if not verdict.passed or not resolution.complete:
            raise DomainError(
                "R4_PUBLICATION_EVIDENCE", "Current evidence no longer authorizes publication", 403
            )

    def recover(self) -> None:
        with self.engine.transaction() as conn:
            conn.execute("DELETE FROM app.tool_grants")
