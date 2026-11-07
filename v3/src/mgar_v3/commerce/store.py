import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from mgar_v3.agents.data import DataReader
from mgar_v3.commerce.domain import CaseInput, CaseMessage, RepairRequest
from mgar_v3.domain import DomainError, TriggerInput, digest
from mgar_v3.live import LivePermit, load_permit
from mgar_v3.repair.domain import Snapshot

if TYPE_CHECKING:
    from mgar_v3.coordination.engine import Engine, Row


class Commerce:
    def __init__(self, engine: "Engine"):
        self.engine = engine

    def bind_live(self, conn: Any, run_id: UUID) -> None:
        from mgar_v3.commerce.budget import bind

        bind(self, conn, run_id)

    def prepare_action(self, action_id: UUID) -> None:
        from mgar_v3.commerce.guards import prepare_action

        prepare_action(self, action_id)

    def ready_transports(self) -> list[Any]:
        from mgar_v3.repair.publication import LiveDraftMCPTransport

        if not self.engine.settings.allow_external_write:
            return []
        with self.engine.transaction() as conn:
            rows = conn.execute(
                "SELECT DISTINCT a.run_id FROM app.actions a JOIN app.outbox o USING(action_id) "
                "JOIN app.commerce_case_runs l USING(run_id) JOIN app.runs r USING(run_id) "
                "WHERE a.status='ready' AND o.status='ready' AND r.execution_mode='live'"
            ).fetchall()
        transports = []
        for row in rows:
            try:
                permit = self.permit(self.manifest(row["run_id"]), "repair")
                assert permit is not None
                permit.validate(external_write=True)
                transports.append(LiveDraftMCPTransport(self.engine, permit))
            except (DomainError, OSError, ValueError):
                continue
        return transports

    def register(self, kind: str, manifest: dict[str, Any]) -> None:
        s = self.engine.settings
        if (
            kind not in {"duplicate_paid", "status_filter", "refunded_status"}
            or manifest["kind"] != kind
            or manifest["project_id"] != s.project_id
            or manifest["repository"] != s.repository.lower()
            or manifest["actor"] != s.local_actor.lower()
        ):
            raise DomainError("COMMERCE_SCOPE_DENIED", "Template is outside this server scope")
        with self.engine.transaction() as conn:
            old = conn.execute(
                "SELECT * FROM app.commerce_templates WHERE template_id=%s AND "
                "project_id=%s AND repository=%s AND actor=%s",
                (kind, s.project_id, s.repository.lower(), s.local_actor.lower()),
            ).fetchone()
            if old and old["manifest_hash"] != digest(manifest):
                raise DomainError(
                    "COMMERCE_TEMPLATE_CHANGED", "Register a reviewed template version"
                )
            conn.execute(
                "INSERT INTO "
                "app.commerce_templates(template_id,project_id,repository,actor,m"
                "anifest,manifest_hash) "
                "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (
                    kind,
                    s.project_id,
                    s.repository.lower(),
                    s.local_actor.lower(),
                    Jsonb(manifest),
                    digest(manifest),
                ),
            )

    def templates(self) -> list[dict[str, Any]]:
        with self.engine.transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM app.commerce_templates ORDER BY template_id"
            ).fetchall()
        return [
            {
                "template_id": r["template_id"],
                "enabled": r["enabled"],
                "execution_mode": r["manifest"]["execution_mode"],
                "window": r["manifest"]["policy"]["window"],
                "manifest_hash": r["manifest_hash"],
            }
            for r in rows
            if r["manifest"]["project_id"] == self.engine.settings.project_id
            and r["manifest"]["repository"] == self.engine.settings.repository.lower()
            and r["manifest"]["actor"] == self.engine.settings.local_actor.lower()
        ]

    def template(self, kind: str) -> "Row":
        s = self.engine.settings
        with self.engine.transaction() as conn:
            value = conn.execute(
                "SELECT * FROM app.commerce_templates WHERE template_id=%s AND "
                "project_id=%s AND repository=%s AND actor=%s",
                (kind, s.project_id, s.repository.lower(), s.local_actor.lower()),
            ).fetchone()
        if not value:
            raise DomainError("COMMERCE_NOT_CONFIGURED", "Register the commerce sources first")
        m, s = value["manifest"], self.engine.settings
        if (
            m["project_id"] != s.project_id
            or m["repository"] != s.repository.lower()
            or m["actor"] != s.local_actor.lower()
        ):
            raise DomainError(
                "COMMERCE_SCOPE_DENIED", "Template does not belong to this actor", 403
            )
        return value

    def case(self, case_id: UUID) -> "Row":
        with self.engine.transaction() as conn:
            row = conn.execute(
                "SELECT c.*,s.payload AS spec,s.spec_hash,s.template_hash "
                "FROM app.commerce_cases c JOIN app.commerce_case_specs s ON "
                "s.case_id=c.case_id AND s.revision=c.current_revision WHERE c.case_id=%s",
                (case_id,),
            ).fetchone()
        s = self.engine.settings
        if (
            not row
            or row["project_id"] != s.project_id
            or row["repository"] != s.repository.lower()
        ):
            raise DomainError("NOT_FOUND", "Commerce case was not found", 404)
        if row["actor"] != s.local_actor.lower() or row["actor"] not in {
            a.lower() for a in s.allowed_actors
        }:
            raise DomainError("ACTOR_DENIED", "Case is outside the current actor scope", 403)
        return row

    def binding(self, run_id: UUID) -> "Row | None":
        with self.engine.transaction() as conn:
            return conn.execute(
                "SELECT l.*,c.current_revision,c.template_id,c.actor,"
                "s.payload AS spec,s.spec_hash,s.template_hash FROM app.commerce_case_runs l "
                "JOIN app.commerce_cases c USING(case_id) JOIN app.commerce_case_specs s "
                "ON s.case_id=l.case_id AND s.revision=l.revision WHERE l.run_id=%s "
                "ORDER BY l.revision DESC LIMIT 1",
                (run_id,),
            ).fetchone()

    def manifest(self, run_id: UUID) -> dict[str, Any]:
        binding = self.binding(run_id)
        if not binding:
            raise DomainError("COMMERCE_RUN_REQUIRED", "Run is not a commerce stage")
        self.case(binding["case_id"])
        template = self.template(binding["template_id"])
        if not template["enabled"] or template["manifest_hash"] != binding["template_hash"]:
            raise DomainError("COMMERCE_TEMPLATE_CHANGED", "Source registration changed")
        return dict(template["manifest"])

    def permit(
        self, manifest: dict[str, Any], stage: str, *, read_only: bool = False
    ) -> LivePermit | None:
        if manifest["execution_mode"] == "fixture":
            return None
        from pathlib import Path

        refs = manifest.get("live", {}).get(stage)
        if not refs:
            raise DomainError(
                "LIVE_PROFILE_REQUIRED", "This stage needs its own reviewed permit", 403
            )
        settings = self.engine.settings
        if stage == "repair":
            if not refs.get("snapshot"):
                raise DomainError(
                    "LIVE_PROFILE_REQUIRED", "Repair snapshot reference is required", 403
                )
            settings = settings.model_copy(update={"repair_snapshot_path": Path(refs["snapshot"])})
        try:
            permit = load_permit(Path(refs["profile"]), Path(refs["authorization"]), settings)
        except (OSError, ValueError):
            raise DomainError(
                "LIVE_PROFILE_REQUIRED", "Reviewed stage permit is unavailable", 403
            ) from None
        expected = "r4-experts-v1" if stage == "investigation" else "r5-repair-v1"
        if permit.profile.investigation_version != expected:
            raise DomainError("LIVE_PROFILE_REQUIRED", "Wrong stage permission", 403)
        p = permit.profile
        if (
            p.project_id != manifest["project_id"]
            or p.repository.lower() != manifest["repository"]
            or p.actor.lower() != manifest["actor"]
            or p.source_ref != manifest["source_ref"]
            or p.source_path != manifest["source_path"]
            or p.expected_source_hash != manifest["source_content_hash"]
        ):
            raise DomainError(
                "COMMERCE_SCOPE_DENIED", "Permit differs from the registered source scope", 403
            )
        if stage == "investigation" and (
            p.knowledge_snapshot_ids != manifest["knowledge_snapshot_ids"]
            or p.data_fixture_hash != manifest["data_profile"]["fixture_hash"]
            or p.rag_config_hash != manifest.get("knowledge_config_hash")
        ):
            raise DomainError(
                "COMMERCE_SCOPE_DENIED", "Investigation sources differ from the permit", 403
            )
        if stage == "repair" and not read_only:
            from mgar_v3.repair.live import load_snapshot

            snapshot = load_snapshot(permit)
            if (
                hashlib.sha256(snapshot.model_dump_json().encode()).hexdigest()
                != manifest["snapshot_hash"]
            ):
                raise DomainError(
                    "COMMERCE_SCOPE_DENIED", "Repair snapshot differs from the case source", 403
                )
        return permit

    def reader_for_run(self, run_id: UUID) -> DataReader | None:
        if not self.binding(run_id):
            return None
        from pathlib import Path

        m = self.manifest(run_id)
        try:
            profile = json.loads(Path(m["reader_path"]).read_text())
        except (OSError, ValueError):
            raise DomainError(
                "DATA_SOURCE_NOT_CONFIGURED", "Registered reader is unavailable"
            ) from None
        if {k: v for k, v in profile.items() if k != "database_url"} != m["data_profile"]:
            raise DomainError("DATA_SOURCE_CHANGED", "Reader metadata changed")
        return DataReader(profile)

    def bound_permit(
        self, run_id: UUID, *, external_write: bool = False, read_only: bool = False
    ) -> LivePermit | None:
        link = self.binding(run_id)
        assert link is not None
        stage = "repair" if link["stage"] == "repair_proposal" else "investigation"
        permit = self.permit(self.manifest(run_id), stage, read_only=read_only)
        if permit:
            expected = link["spec"]["authorization_refs"][stage + "_profile_hash"]
            with self.engine.transaction() as conn:
                run = self.engine._run(conn, run_id)
                mandate = conn.execute(
                    "SELECT scope FROM app.mandates WHERE mandate_id=%s", (run["mandate_id"],)
                ).fetchone()
            if (
                permit.profile.profile_hash != expected
                or not mandate
                or mandate["scope"].get("profile_hash") != expected
            ):
                raise DomainError(
                    "COMMERCE_SCOPE_DENIED", "Current permit differs from the immutable Case", 403
                )
            if not read_only:
                permit.validate(external_write=external_write)
        return permit

    def snapshot(self, run_id: UUID) -> Snapshot:
        from mgar_v3.repair.source import verify_snapshot

        m = self.manifest(run_id)
        snapshot = Snapshot.model_validate_json(self.engine.artifacts.read(m["snapshot_hash"]))
        verify_snapshot(snapshot)
        if (
            snapshot.base_sha != m["source_ref"]
            or snapshot.base_tree != m["source_tree"]
            or hashlib.sha256(snapshot.files[m["source_path"]].encode()).hexdigest()
            != m["source_content_hash"]
        ):
            raise DomainError("SOURCE_CHANGED", "Code differs from the registered snapshot")
        return snapshot

    def no_unknown(self) -> None:
        with self.engine.transaction() as conn:
            pending = conn.execute(
                "SELECT 1 FROM app.usage_calls u JOIN app.runs r USING(run_id) "
                "JOIN app.tickets t USING(ticket_id) JOIN app.threads h USING(thread_id) "
                "WHERE h.repository=%s AND u.status='unknown' UNION ALL "
                "SELECT 1 FROM app.actions WHERE payload->>'repository'=%s "
                "AND status='outcome_unknown'",
                (self.engine.settings.repository.lower(), self.engine.settings.repository.lower()),
            ).fetchone()
        if pending:
            raise DomainError(
                "OUTCOME_UNKNOWN", "Reconcile existing requests before starting another Case"
            )

    def knowledge_scope(self, manifest: dict[str, Any]) -> None:
        documents: set[str] = set()
        for value in manifest["knowledge_snapshot_ids"]:
            snapshot, chunks, _ = self.engine.knowledge.eligible(
                UUID(value), self.engine.settings.local_actor
            )
            if snapshot["manifest_hash"] != manifest["knowledge_snapshot_hash"]:
                raise DomainError("KNOWLEDGE_SOURCE_CHANGED", "Knowledge snapshot changed")
            documents.update(str(c["document_id"]) for c in chunks)
        if not set(manifest["knowledge_document_ids"]) <= documents:
            raise DomainError("KNOWLEDGE_REVOKED", "Required policy access was revoked", 403)

    def existing_request(self, key: str, payload: dict[str, Any]) -> "Row | None":
        s = self.engine.settings
        with self.engine.transaction() as conn:
            old = conn.execute(
                "SELECT * FROM app.commerce_requests WHERE project_id=%s "
                "AND actor=%s AND request_key=%s",
                (s.project_id, s.local_actor.lower(), key),
            ).fetchone()
        if old and old["request_hash"] != digest(payload):
            raise DomainError("EVENT_ID_REUSED", "Request key has different immutable content")
        if old:
            self.case(old["case_id"])
        return old

    def current_result(self, case_id: UUID, stage: str) -> "Row | None":
        case = self.case(case_id)
        with self.engine.transaction() as conn:
            return conn.execute(
                "SELECT b.* FROM app.commerce_current_results p JOIN "
                "app.business_results b USING(result_id) "
                "WHERE p.case_id=%s AND p.case_revision=%s AND p.stage=%s",
                (case_id, case["current_revision"], stage),
            ).fetchone()

    def detail(self, case_id: UUID) -> dict[str, Any]:
        case = self.case(case_id)
        # Case metadata has no source text; source access is separately projected.
        with self.engine.transaction() as conn:
            links = conn.execute(
                "SELECT * FROM app.commerce_case_runs WHERE case_id=%s ORDER BY revision,stage",
                (case_id,),
            ).fetchall()
        return {
            "case_id": str(case_id),
            "case_revision": case["current_revision"],
            "spec_hash": case["spec_hash"],
            "spec": case["spec"],
            "runs": links,
        }

    def list_cases(self) -> list[dict[str, Any]]:
        s = self.engine.settings
        with self.engine.transaction() as conn:
            rows = conn.execute(
                "SELECT case_id FROM app.commerce_cases WHERE project_id=%s AND "
                "repository=%s AND actor=%s ORDER BY created_at DESC LIMIT 100",
                (s.project_id, s.repository.lower(), s.local_actor.lower()),
            ).fetchall()
        return [
            {"case_id": str(r["case_id"]), "view": self.result_view(r["case_id"])["view"]}
            for r in rows
        ]

    def result_view(self, case_id: UUID) -> dict[str, Any]:
        from mgar_v3.commerce.presentation import result_view

        return result_view(self, case_id)

    def artifact_bytes(self, case_id: UUID, content_hash: str) -> bytes:
        self.case(case_id)
        with self.engine.transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM app.business_results WHERE case_id=%s", (case_id,)
            ).fetchall()
        permitted = set()
        for row in rows:
            self.knowledge_scope(self.manifest(row["run_id"]))
            permitted.update(
                [
                    row["result_hash"],
                    row["payload"].get("artifact_hash"),
                    row["payload"].get("numeric_receipt_hash"),
                ]
            )
            permitted.update(q["content_hash"] for q in row["payload"]["quote_refs"])
        if content_hash not in permitted:
            raise DomainError("NOT_FOUND", "Artifact does not belong to this case", 404)
        data = self.engine.artifacts.read(content_hash)
        self.case(case_id)
        for row in rows:
            self.knowledge_scope(self.manifest(row["run_id"]))
        return data

    def message(self, case_id: UUID, body: CaseMessage) -> dict[str, Any]:
        case = self.case(case_id)
        raw = {"case_id": str(case_id), **body.model_dump(mode="json")}
        key = "message:" + body.client_request_id
        old = self.existing_request(key, raw)
        if old:
            return {"case_id": str(case_id), "run_id": str(old["run_id"]), "duplicate": True}
        if case["current_revision"] != body.expected_case_revision:
            raise DomainError("CASE_SCOPE_CHANGED", "Case revision changed")
        with self.engine.transaction() as conn:
            link = conn.execute(
                "SELECT * FROM app.commerce_case_runs WHERE case_id=%s AND "
                "revision=%s ORDER BY stage DESC LIMIT 1",
                (case_id, case["current_revision"]),
            ).fetchone()
        assert link is not None
        run = self.engine.get_run(link["run_id"])
        m = self.manifest(run["run_id"])
        self.knowledge_scope(m)
        permit = self.permit(m, "investigation")
        new_stage = bool(
            body.changes
            or link["stage"] == "repair_proposal"
            or run["status"] in {"succeeded", "failed", "cancelled"}
        )
        spec = dict(case["spec"])
        if new_stage:
            spec["case_revision"] += 1
            if body.changes:
                spec.update(body.changes.model_dump(mode="json", exclude_none=True))
                from mgar_v3.commerce.domain import Window

                if permit and Window.model_validate(spec["window"]) != Window.model_validate(
                    m["policy"]["window"]
                ):
                    raise DomainError(
                        "COMMERCE_SCOPE_DENIED",
                        "Changed window needs separately reviewed registration",
                        403,
                    )
            if spec["goal"] == "draft_pr":
                repair = self.permit(m, "repair")
                spec["authorization_refs"] = {
                    **spec["authorization_refs"],
                    "repair_profile_hash": repair.profile.profile_hash
                    if repair
                    else digest({"fixture": case["template_hash"], "stage": "repair"}),
                }
            encoded = json.dumps(
                spec, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode()
            spec_hash = self.engine.artifacts.put(encoded)
        with self.engine.transaction() as conn:
            old = self.existing_request(key, raw)
            if old:
                return {"case_id": str(case_id), "run_id": str(old["run_id"]), "duplicate": True}
            if self.case(case_id)["current_revision"] != body.expected_case_revision:
                raise DomainError("CASE_SCOPE_CHANGED", "Case revision changed")
            if conn.execute(
                "SELECT 1 FROM app.usage_calls WHERE run_id=%s AND status IN "
                "('reserved','unknown') UNION ALL SELECT 1 FROM app.actions WHERE "
                "run_id=%s AND status IN ('dispatching','outcome_unknown')",
                (run["run_id"], run["run_id"]),
            ).fetchone():
                raise DomainError(
                    "OUTCOME_UNKNOWN", "Reconcile issued requests before changing scope"
                )
            if new_stage:
                if run["status"] not in {"succeeded", "failed", "cancelled"}:
                    self.engine.cancel(run["run_id"])
                event = TriggerInput(
                    source="web",
                    external_event_id="commerce-message:" + digest(raw),
                    project_id=case["project_id"],
                    repository=case["repository"],
                    issue_number=case["issue_number"],
                    actor=case["actor"],
                    body="/mgar investigate " + (permit.profile.question if permit else body.body),
                    execution_mode=m["execution_mode"],
                )
                receipt = self.engine.ingest(event, permit)
                if receipt.route != "created" or not receipt.run_id:
                    raise DomainError("CASE_STAGE_BUSY", "A new investigation needs a free primary")
                conn.execute(
                    "INSERT INTO app.artifacts(content_hash,byte_count) VALUES "
                    "(%s,%s) ON CONFLICT DO NOTHING",
                    (spec_hash, len(encoded)),
                )
                conn.execute(
                    "INSERT INTO app.commerce_case_specs VALUES (%s,%s,%s,%s,%s)",
                    (case_id, spec["case_revision"], spec_hash, case["template_hash"], Jsonb(spec)),
                )
                conn.execute(
                    "UPDATE app.commerce_cases SET current_revision=%s WHERE case_id=%s",
                    (spec["case_revision"], case_id),
                )
                current = self.engine._run(conn, receipt.run_id)
                conn.execute(
                    "INSERT INTO "
                    "app.commerce_case_runs(case_id,revision,stage,run_id,input_revis"
                    "ion) VALUES (%s,%s,'investigation',%s,%s)",
                    (case_id, spec["case_revision"], receipt.run_id, current["thread_revision"]),
                )
                conn.execute(
                    "UPDATE app.runs SET "
                    "graph_version='r4-experts-v1',r4_requested_snapshots=%s WHERE "
                    "run_id=%s",
                    (Jsonb(m["knowledge_snapshot_ids"]), receipt.run_id),
                )
                conn.execute(
                    "UPDATE app.mandates SET scope=scope||%s WHERE mandate_id=%s",
                    (Jsonb({"read_only": True}), current["mandate_id"]),
                )
            else:
                receipt = self.engine.follow_up(
                    run["run_id"], "commerce-message:" + digest(raw), body.body, permit
                )
            assert receipt.run_id is not None
            self.bind_live(conn, receipt.run_id)
            conn.execute(
                "INSERT INTO app.commerce_requests VALUES (%s,%s,%s,%s,%s,%s)",
                (case["project_id"], case["actor"], key, digest(raw), case_id, receipt.run_id),
            )
        return {"case_id": str(case_id), "run_id": str(receipt.run_id), "duplicate": False}

    def require_result(
        self, run_id: UUID, artifact_hash: str | None = None, *, action_id: UUID | None = None
    ) -> "Row":
        from mgar_v3.commerce.review import fresh

        link = self.binding(run_id)
        if not link:
            raise DomainError("COMMERCE_RUN_REQUIRED", "No case binding")
        result = self.current_result(link["case_id"], link["stage"])
        run = self.engine.get_run(run_id)
        if (
            not result
            or result["payload"]["business_status"] != "passed"
            or result["run_id"] != run_id
            or result["spec_hash"] != link["spec_hash"]
            or link["current_revision"] != link["revision"]
            or result["input_revision"] != run["thread_revision"]
            or result["input_revision"] != run["input_revision"]
            or result["execution_epoch"] != run["execution_epoch"]
            or not fresh(self, result, action_id, now=datetime.now(UTC))
            or (artifact_hash is not None and result["artifact_hash"] != artifact_hash)
        ):
            raise DomainError(
                "BUSINESS_RESULT_REQUIRED", "A current passed business result is required"
            )
        self.knowledge_scope(self.manifest(run_id))
        return result

    def start_repair(self, case_id: UUID, body: RepairRequest) -> dict[str, Any]:
        from mgar_v3.repair.domain import RepairInput

        case = self.case(case_id)
        raw = {"case_id": str(case_id), **body.model_dump(mode="json")}
        key = "repair:" + body.client_request_id
        old = self.existing_request(key, raw)
        if old:
            return {"case_id": str(case_id), "run_id": str(old["run_id"]), "duplicate": True}
        if (
            case["spec"]["goal"] != "draft_pr"
            or body.expected_case_revision != case["current_revision"]
        ):
            raise DomainError(
                "CASE_SCOPE_CHANGED", "Repair needs the current draft_pr specification"
            )
        parent = self.current_result(case_id, "investigation")
        if not parent or parent["result_hash"] != body.investigation_result_hash:
            raise DomainError("BUSINESS_RESULT_REQUIRED", "Select the exact investigation result")
        self.require_result(parent["run_id"])
        if self.engine.get_run(parent["run_id"])["status"] != "succeeded":
            raise DomainError("CASE_STAGE_BUSY", "Investigation must finish before repair")
        m = self.manifest(parent["run_id"])
        reader = self.reader_for_run(parent["run_id"])
        if reader is None:
            raise DomainError(
                "DATA_SOURCE_NOT_CONFIGURED", "Repair requires the original data source"
            )
        reader.query(f"SELECT count(*) FROM {reader.profile['schema']}.orders", [])
        permit = self.permit(m, "repair")
        if (
            permit
            and permit.profile.profile_hash
            != case["spec"]["authorization_refs"]["repair_profile_hash"]
        ):
            raise DomainError(
                "COMMERCE_SCOPE_DENIED", "Repair permit changed after investigation", 403
            )
        request = RepairInput(
            project_id=case["project_id"],
            issue_number=case["issue_number"],
            question=permit.profile.question
            if permit
            else "Fix PaidRevenueCents: sum integer cents once per paid order ID; "
            "exclude every other status. "
            "Follow the registered commerce policy. Investigation result: " + parent["result_hash"],
            client_request_id="commerce-repair:" + digest(raw),
            execution_mode=m["execution_mode"],
        )
        prepared = self.engine.repairs.prepare_create(
            request, permit, self.snapshot(parent["run_id"])
        )
        with self.engine.transaction() as conn:
            old = self.existing_request(key, raw)
            if old:
                return {"case_id": str(case_id), "run_id": str(old["run_id"]), "duplicate": True}
            self.require_result(parent["run_id"])
            if conn.execute(
                "SELECT 1 FROM app.commerce_case_runs WHERE case_id=%s AND revision=%s "
                "AND stage='repair_proposal'",
                (case_id, case["current_revision"]),
            ).fetchone():
                raise DomainError("CASE_STAGE_EXISTS", "This case already has its repair stage")
            receipt = self.engine.repairs.commit_create(request, prepared, permit)
            run_id = UUID(receipt["run_id"])
            run = self.engine._run(conn, run_id)
            conn.execute(
                "INSERT INTO "
                "app.commerce_case_runs(case_id,revision,stage,run_id,input_revis"
                "ion,preceding_result_id) "
                "VALUES (%s,%s,'repair_proposal',%s,%s,%s)",
                (
                    case_id,
                    case["current_revision"],
                    run_id,
                    run["thread_revision"],
                    parent["result_id"],
                ),
            )
            conn.execute(
                "INSERT INTO app.commerce_requests VALUES (%s,%s,%s,%s,%s,%s)",
                (case["project_id"], case["actor"], key, digest(raw), case_id, run_id),
            )
            self.engine._event(
                conn,
                run_id,
                "commerce_repair_requested",
                case_id=str(case_id),
                parent_result_hash=parent["result_hash"],
            )
            self.bind_live(conn, run_id)
        return {"case_id": str(case_id), "run_id": str(run_id), "duplicate": False}

    def create(self, body: CaseInput) -> dict[str, Any]:
        raw = body.model_dump(mode="json")
        key = "create:" + body.client_request_id
        old = self.existing_request(key, raw)
        if old:
            return {"case_id": str(old["case_id"]), "run_id": str(old["run_id"]), "duplicate": True}
        template = self.template(body.template_id)
        m, s = template["manifest"], self.engine.settings
        if not template["enabled"] or m["execution_mode"] != body.execution_mode:
            raise DomainError("COMMERCE_MODE_DENIED", "Template mode or activation differs")
        permit = self.permit(m, "investigation")
        repair = self.permit(m, "repair") if body.goal == "draft_pr" else None
        if any(p and p.profile.issue_number != body.issue_number for p in (permit, repair)):
            raise DomainError(
                "COMMERCE_SCOPE_DENIED", "Issue differs from the reviewed stage permits", 403
            )
        if permit and body.window.model_dump(mode="json") != m["policy"]["window"]:
            from mgar_v3.commerce.domain import Window

            if body.window != Window.model_validate(m["policy"]["window"]):
                raise DomainError(
                    "COMMERCE_SCOPE_DENIED", "Live window differs from the frozen registration", 403
                )
        case_id = uuid4()
        spec = {
            "schema": "commerce-case-v1",
            "case_id": str(case_id),
            "case_revision": 1,
            "task_kind": body.template_id,
            "project_id": s.project_id,
            "repository": s.repository.lower(),
            "issue_number": body.issue_number,
            "actor": s.local_actor.lower(),
            "goal": body.goal,
            "window": body.window.model_dump(mode="json"),
            "policy_version": "commerce-revenue-v1",
            "source_refs": {
                k: m[k]
                for k in [
                    "knowledge_snapshot_hash",
                    "source_ref",
                    "source_tree",
                    "source_path",
                    "source_content_hash",
                ]
            },
            "acceptance_profile_hash": m["acceptance_profile_hash"],
            "authorization_refs": {
                "investigation_profile_hash": permit.profile.profile_hash
                if permit
                else digest({"fixture": template["manifest_hash"], "stage": "investigation"}),
                "repair_profile_hash": repair.profile.profile_hash
                if repair
                else (
                    digest({"fixture": template["manifest_hash"], "stage": "repair"})
                    if body.goal == "draft_pr"
                    else None
                ),
            },
        }
        spec["source_refs"]["data_fixture_hash"] = m["data_profile"]["fixture_hash"]
        encoded = json.dumps(
            spec, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
        spec_hash = self.engine.artifacts.put(encoded)
        question = (
            permit.profile.question
            if permit
            else (
                f"Investigate {body.template_id}: paid order revenue, "
                "duplicates and status filtering. "
                f"Report window {body.window.start.isoformat()} to {body.window.end.isoformat()}."
            )
        )
        event = TriggerInput(
            source="web",
            external_event_id="commerce:" + digest({"actor": s.local_actor, "key": key}),
            project_id=s.project_id,
            repository=s.repository,
            issue_number=body.issue_number,
            actor=s.local_actor,
            body="/mgar investigate " + question,
            execution_mode=body.execution_mode,
        )
        with self.engine.transaction() as conn:
            old = self.existing_request(key, raw)
            if old:
                return {
                    "case_id": str(old["case_id"]),
                    "run_id": str(old["run_id"]),
                    "duplicate": True,
                }
            self.no_unknown()
            receipt = self.engine.ingest(event, permit)
            if receipt.route != "created" or not receipt.run_id:
                raise DomainError("CASE_STAGE_BUSY", "The Issue already has an active primary")
            conn.execute(
                "INSERT INTO app.artifacts(content_hash,byte_count) VALUES "
                "(%s,%s) ON CONFLICT DO NOTHING",
                (spec_hash, len(encoded)),
            )
            conn.execute(
                "INSERT INTO "
                "app.commerce_cases(case_id,project_id,repository,issue_number,th"
                "read_id,actor,template_id) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (
                    case_id,
                    s.project_id,
                    s.repository.lower(),
                    body.issue_number,
                    receipt.thread_id,
                    s.local_actor.lower(),
                    body.template_id,
                ),
            )
            conn.execute(
                "INSERT INTO "
                "app.commerce_case_specs(case_id,revision,spec_hash,template_hash"
                ",payload) VALUES (%s,1,%s,%s,%s)",
                (case_id, spec_hash, template["manifest_hash"], Jsonb(spec)),
            )
            run = self.engine._run(conn, receipt.run_id)
            conn.execute(
                "INSERT INTO "
                "app.commerce_case_runs(case_id,revision,stage,run_id,input_revis"
                "ion) VALUES (%s,1,'investigation',%s,%s)",
                (case_id, receipt.run_id, run["thread_revision"]),
            )
            conn.execute(
                "UPDATE app.runs SET graph_version='r4-experts-v1' WHERE run_id=%s",
                (receipt.run_id,),
            )
            conn.execute(
                "UPDATE app.mandates SET scope=scope||%s WHERE mandate_id=%s",
                (Jsonb({"read_only": True}), run["mandate_id"]),
            )
            conn.execute(
                "UPDATE app.runs SET r4_requested_snapshots=%s WHERE run_id=%s",
                (Jsonb(m["knowledge_snapshot_ids"]), receipt.run_id),
            )
            conn.execute(
                "INSERT INTO app.commerce_requests VALUES (%s,%s,%s,%s,%s,%s)",
                (s.project_id, s.local_actor.lower(), key, digest(raw), case_id, receipt.run_id),
            )
            self.engine._event(
                conn,
                receipt.run_id,
                "commerce_case_created",
                case_id=str(case_id),
                spec_hash=spec_hash,
            )
            self.bind_live(conn, receipt.run_id)
        return {"case_id": str(case_id), "run_id": str(receipt.run_id), "duplicate": False}

    def bind_context(self, run_id: UUID, epoch: int, revision: int) -> "Row":
        with self.engine.transaction() as conn:
            old = conn.execute(
                "SELECT * FROM app.r4_context WHERE run_id=%s AND input_revision=%s",
                (run_id, revision),
            ).fetchone()
        if old:
            return old
        m = self.manifest(run_id)
        reader = self.reader_for_run(run_id)
        profile = {
            "actor": self.engine.settings.local_actor,
            "project_id": m["project_id"],
            "repository": m["repository"],
            "max_tool_calls": 36,
            "max_rounds": 2,
            "deadline": (datetime.now(UTC) + timedelta(seconds=90)).isoformat(),
            "repo_mode": m["execution_mode"],
            "sql_profile": {k: v for k, v in reader.profile.items() if k != "database_url"}
            if reader
            else None,
            "knowledge_modes": [],
            "repository_hash": m["source_content_hash"],
        }
        with self.engine.transaction() as conn:
            self.engine._run(conn, run_id, epoch)
            for value in m["knowledge_snapshot_ids"]:
                snapshot, _, _ = self.engine.knowledge.eligible(
                    UUID(value), self.engine.settings.local_actor
                )
                profile["knowledge_modes"].append(snapshot["config"])
            row = conn.execute(
                "INSERT INTO "
                "app.r4_context(run_id,input_revision,knowledge_snapshot_ids,prof"
                "ile,profile_hash) VALUES (%s,%s,%s,%s,%s) ON "
                "CONFLICT(run_id,input_revision) DO UPDATE SET "
                "profile=app.r4_context.profile RETURNING *",
                (
                    run_id,
                    revision,
                    Jsonb(m["knowledge_snapshot_ids"]),
                    Jsonb(profile),
                    digest(profile),
                ),
            ).fetchone()
            assert row is not None
            return row

    def read_repository(self, grant: "Row") -> dict[str, Any]:
        m = self.manifest(grant["run_id"])
        source = self.snapshot(grant["run_id"])
        content = source.files[m["source_path"]].encode()
        return self.engine.tools.record(
            grant,
            content,
            ("fixture-git://" if source.origin == "fixture_git" else "github://")
            + source.repository
            + "/"
            + m["source_path"],
            source.base_sha,
            {"kind": "repo", "source_hash": hashlib.sha256(content).hexdigest()},
        )

    def audit_read(self, run_id: UUID, revision: int, rows: str, aggregate: str) -> None:
        with self.engine.transaction() as conn:
            run = self.engine._run(conn, run_id)
            if run["status"] != "running" or run["input_revision"] != revision:
                raise DomainError("FENCED", "Rows observation no longer belongs to this execution")
            self.engine._event(
                conn,
                run_id,
                "commerce_rows_observed",
                input_revision=revision,
                rows_evidence_id=rows,
                aggregate_evidence_id=aggregate,
            )

    def execute(self, owner: Any, run_id: UUID) -> UUID | None:
        from mgar_v3.commerce.runtime import execute

        return execute(self, owner, run_id)

    def commit_investigation(
        self, run_id: UUID, epoch: int, state: Any, report: bytes, resolution: Any, verdict: Any
    ) -> None:
        from mgar_v3.commerce.results import commit_investigation

        return commit_investigation(self, run_id, epoch, state, report, resolution, verdict)
