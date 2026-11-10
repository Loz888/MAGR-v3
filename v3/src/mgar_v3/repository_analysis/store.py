"""PostgreSQL persistence for repository analysis runs.

The store reuses the existing run/mandate/budget/usage/evidence/artifact tables
and adds only the analysis-specific ledger tables from migration 0020.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from mgar_v3.coordination.engine import Engine
from mgar_v3.domain import DomainError, Verification, digest
from mgar_v3.repository_analysis.domain import (
    REPOSITORY_ANALYSIS_GRAPH,
    AnalysisAuthorization,
    AnalysisLimits,
    AnalysisModelConfig,
    AnalysisSpec,
    CreateAnalysisInput,
    FollowUpInput,
    mandate_scope,
    request_digest,
    validate_commit,
)
from mgar_v3.repository_analysis.report import EvidenceRecord, ReportVerification
from mgar_v3.repository_analysis.tools import ToolOutcome


@dataclass(frozen=True)
class ToolAttempt:
    call_id: UUID
    step_key: str
    reused_result: bytes | None = None
    evidence_id: UUID | None = None
    source_version: str | None = None
    reference: str | None = None


def _profile_hash(
    *,
    run_id: UUID,
    repository: str,
    commit: str,
    task: str,
    limits: AnalysisLimits,
    authorization_hash: str,
    parent_run_id: UUID | None,
) -> str:
    return digest(
        {
            "run_id": str(run_id),
            "repository": repository,
            "commit": commit,
            "task": task,
            "limits": limits.model_dump(mode="json"),
            "authorization_hash": authorization_hash,
            "parent_run_id": str(parent_run_id) if parent_run_id else None,
        }
    )


class RepositoryAnalysisStore:
    accounts_usage = True

    def __init__(self, engine: Engine):
        self.engine = engine

    def _event(self, conn: Any, run_id: UUID, kind: str, **payload: Any) -> None:
        self.engine._event(conn, run_id, kind, **payload)

    def create(
        self,
        request: CreateAnalysisInput,
        *,
        repository: str,
        commit: str,
        execution_mode: str,
        authorization_hash: str,
        model: AnalysisModelConfig,
        actor: str,
    ) -> UUID:
        identity_digest = request_digest(request)
        thread_id, ticket_id, mandate_id, run_id = uuid4(), uuid4(), uuid4(), uuid4()
        profile_hash = _profile_hash(
            run_id=run_id,
            repository=repository,
            commit=commit,
            task=request.task,
            limits=request.limits,
            authorization_hash=authorization_hash,
            parent_run_id=None,
        )
        request_key = "repo-analysis:" + request.client_request_id
        with self.engine.transaction() as conn:
            existing = conn.execute(
                "SELECT run_id,request_digest FROM app.repo_analysis_runs WHERE request_key=%s",
                (request_key,),
            ).fetchone()
            if existing is not None:
                if existing["request_digest"] != identity_digest:
                    raise DomainError(
                        "IDEMPOTENCY_CONFLICT", "Client request id was reused with new input", 409
                    )
                return UUID(str(existing["run_id"]))
            conn.execute(
                "INSERT INTO app.threads(thread_id,project_id,repository,external_key,revision,"
                "latest_seq) VALUES (%s,%s,%s,%s,1,0)",
                (
                    thread_id,
                    self.engine.settings.project_id,
                    repository,
                    f"{repository}#repository-analysis:{ticket_id}",
                ),
            )
            conn.execute(
                "INSERT INTO app.mandates(mandate_id,project_id,actor,scope,policy_version,"
                "execution_mode) VALUES (%s,%s,%s,%s,%s,%s)",
                (
                    mandate_id,
                    self.engine.settings.project_id,
                    actor,
                    Jsonb(
                        mandate_scope(
                            repository=repository,
                            commit=commit,
                            task=request.task,
                            limits=request.limits,
                            model=model,
                            authorization_hash=authorization_hash,
                            profile_hash=profile_hash,
                        )
                    ),
                    "repository-analysis-readonly-v1",
                    execution_mode,
                ),
            )
            conn.execute(
                "INSERT INTO app.budgets(mandate_id,limit_micros) VALUES (%s,%s)",
                (mandate_id, request.limits.max_cost_micros),
            )
            conn.execute(
                "INSERT INTO app.authorization_budgets(profile_hash,limit_micros,used_calls,"
                "call_limit) VALUES (%s,%s,0,%s)",
                (profile_hash, request.limits.max_cost_micros, request.limits.max_model_calls),
            )
            conn.execute(
                "INSERT INTO app.tickets(ticket_id,mandate_id,thread_id,status) "
                "VALUES (%s,%s,%s,'open')",
                (ticket_id, mandate_id, thread_id),
            )
            conn.execute(
                "INSERT INTO app.runs(run_id,ticket_id,attempt_no,graph_version,execution_mode,"
                "status,question,input_revision) VALUES (%s,%s,1,%s,%s,'queued',%s,1)",
                (run_id, ticket_id, REPOSITORY_ANALYSIS_GRAPH, execution_mode, request.task),
            )
            deadline = datetime.now(UTC) + timedelta(seconds=request.limits.max_seconds)
            conn.execute(
                "INSERT INTO app.repo_analysis_runs(run_id,request_key,request_digest,repository,"
                "requested_ref,resolved_commit,task,execution_mode,limits,model,deadline_at,"
                "authorization_hash,profile_hash) VALUES "
                "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    run_id,
                    request_key,
                    identity_digest,
                    repository,
                    request.ref,
                    commit,
                    request.task,
                    execution_mode,
                    Jsonb(request.limits.model_dump(mode="json")),
                    Jsonb(model.model_dump(mode="json")),
                    deadline,
                    authorization_hash,
                    profile_hash,
                ),
            )
            conn.execute(
                "INSERT INTO app.subscriptions(thread_id,run_id,role,active) "
                "VALUES (%s,%s,'primary',true)",
                (thread_id, run_id),
            )
            self._event(
                conn,
                run_id,
                "repository_analysis_accepted",
                repository=repository,
                commit=commit,
                execution_mode=execution_mode,
                limits=request.limits.model_dump(mode="json"),
                task_digest=hashlib.sha256(request.task.encode()).hexdigest(),
            )
            return run_id

    def follow_up(
        self,
        parent_run_id: UUID,
        request: FollowUpInput,
        *,
        authorization: AnalysisAuthorization | None = None,
    ) -> UUID:
        with self.engine.transaction() as conn:
            parent = conn.execute(
                "SELECT s.*,r.ticket_id,r.execution_mode,r.report_hash AS run_report_hash,"
                "r.status AS run_status,t.thread_id,th.revision,"
                "(SELECT m.actor FROM app.mandates m JOIN app.tickets t2 "
                "ON t2.mandate_id=m.mandate_id WHERE t2.ticket_id=r.ticket_id) AS actor "
                "FROM app.repo_analysis_runs s JOIN app.runs r USING(run_id) "
                "JOIN app.tickets t USING(ticket_id) JOIN app.threads th USING(thread_id) "
                "WHERE s.run_id=%s FOR UPDATE",
                (parent_run_id,),
            ).fetchone()
            if parent is None:
                raise DomainError("NOT_FOUND", "Repository analysis run was not found", 404)
            limits = AnalysisLimits.model_validate(parent["limits"])
            model = AnalysisModelConfig.model_validate(parent["model"])
            authorization_hash = parent["authorization_hash"]
            actor = parent["actor"] or "repository-analysis-followup"
            if parent["execution_mode"] == "live":
                if authorization is None:
                    raise DomainError(
                        "REPO_ANALYSIS_PROFILE_REQUIRED", "追问需要当前有效的分析使用许可。", 403
                    )
                try:
                    authorization.validate_scope(parent["repository"], authorization.limits)
                except ValueError as exc:
                    raise DomainError(
                        "REPO_ANALYSIS_SCOPE_DENIED",
                        "当前使用许可不允许分析此仓库，请检查模型与使用设置。",
                        403,
                    ) from exc
                limits, model = authorization.limits, authorization.model
                authorization_hash = authorization.authorization_hash
                actor = authorization.approved_by
            if parent["run_status"] in {"running", "queued", "paused", "cancelled"}:
                raise DomainError(
                    "RUN_NOT_SETTLED", "Follow-up requires a settled repository analysis", 409
                )
            request_key = "repo-followup:" + str(parent_run_id) + ":" + request.client_request_id
            body_digest = hashlib.sha256(request.body.encode()).hexdigest()
            existing = conn.execute(
                "SELECT run_id,request_digest FROM app.repo_analysis_runs WHERE request_key=%s",
                (request_key,),
            ).fetchone()
            if existing is not None:
                if existing["request_digest"] != body_digest:
                    raise DomainError(
                        "IDEMPOTENCY_CONFLICT", "Follow-up id was reused with new text", 409
                    )
                return UUID(str(existing["run_id"]))
            ticket_id = UUID(str(parent["ticket_id"]))
            thread_id = UUID(str(parent["thread_id"]))
            mandate_id, run_id = uuid4(), uuid4()
            profile_hash = _profile_hash(
                run_id=run_id,
                repository=parent["repository"],
                commit=parent["resolved_commit"],
                task=request.body,
                limits=limits,
                authorization_hash=authorization_hash,
                parent_run_id=parent_run_id,
            )
            revision = int(parent["revision"]) + 1
            attempt_row = conn.execute(
                "SELECT max(attempt_no) AS attempt FROM app.runs WHERE ticket_id=%s",
                (ticket_id,),
            ).fetchone()
            assert attempt_row is not None
            attempt = int(attempt_row["attempt"]) + 1
            conn.execute(
                "UPDATE app.threads SET revision=%s WHERE thread_id=%s", (revision, thread_id)
            )
            conn.execute(
                "UPDATE app.tickets SET status='open',outcome=NULL WHERE ticket_id=%s",
                (ticket_id,),
            )
            conn.execute(
                "UPDATE app.subscriptions SET active=false WHERE thread_id=%s AND active",
                (thread_id,),
            )
            conn.execute(
                "INSERT INTO app.mandates(mandate_id,project_id,actor,scope,policy_version,"
                "execution_mode) VALUES (%s,%s,%s,%s,%s,%s)",
                (
                    mandate_id,
                    self.engine.settings.project_id,
                    actor,
                    Jsonb(
                        mandate_scope(
                            repository=parent["repository"],
                            commit=parent["resolved_commit"],
                            task=request.body,
                            limits=limits,
                            model=model,
                            authorization_hash=authorization_hash,
                            profile_hash=profile_hash,
                        )
                    ),
                    "repository-analysis-readonly-v1",
                    parent["execution_mode"],
                ),
            )
            conn.execute(
                "UPDATE app.tickets SET mandate_id=%s WHERE ticket_id=%s",
                (mandate_id, ticket_id),
            )
            conn.execute(
                "INSERT INTO app.budgets(mandate_id,limit_micros) VALUES (%s,%s)",
                (mandate_id, limits.max_cost_micros),
            )
            conn.execute(
                "INSERT INTO app.authorization_budgets(profile_hash,limit_micros,used_calls,"
                "call_limit) VALUES (%s,%s,0,%s)",
                (profile_hash, limits.max_cost_micros, limits.max_model_calls),
            )
            conn.execute(
                "INSERT INTO app.runs(run_id,ticket_id,attempt_no,graph_version,execution_mode,"
                "status,question,input_revision) VALUES (%s,%s,%s,%s,%s,'queued',%s,%s)",
                (
                    run_id,
                    ticket_id,
                    attempt,
                    REPOSITORY_ANALYSIS_GRAPH,
                    parent["execution_mode"],
                    request.body,
                    revision,
                ),
            )
            mapping: dict[str, str] = {}
            rows = conn.execute(
                "SELECT * FROM app.evidence WHERE run_id=%s ORDER BY observed_at",
                (parent_run_id,),
            ).fetchall()
            for row in rows:
                new_id = uuid4()
                mapping[str(row["evidence_id"])] = str(new_id)
                conn.execute(
                    "INSERT INTO app.evidence(evidence_id,run_id,project_id,source_uri,"
                    "source_version,content_hash,start_line,end_line,tool_call_id) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        new_id,
                        run_id,
                        self.engine.settings.project_id,
                        row["source_uri"],
                        row["source_version"],
                        row["content_hash"],
                        row["start_line"],
                        row["end_line"],
                        uuid4(),
                    ),
                )
            deadline = datetime.now(UTC) + timedelta(seconds=limits.max_seconds)
            conn.execute(
                "INSERT INTO app.repo_analysis_runs(run_id,request_key,request_digest,repository,"
                "requested_ref,resolved_commit,task,execution_mode,limits,model,deadline_at,"
                "authorization_hash,profile_hash,parent_run_id,prior_report_hash,"
                "prior_evidence_map) VALUES "
                "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    run_id,
                    request_key,
                    body_digest,
                    parent["repository"],
                    parent["requested_ref"],
                    parent["resolved_commit"],
                    request.body,
                    parent["execution_mode"],
                    Jsonb(limits.model_dump(mode="json")),
                    Jsonb(model.model_dump(mode="json")),
                    deadline,
                    authorization_hash,
                    profile_hash,
                    parent_run_id,
                    parent["run_report_hash"],
                    Jsonb(mapping),
                ),
            )
            conn.execute(
                "INSERT INTO app.subscriptions(thread_id,run_id,role,active) "
                "VALUES (%s,%s,'primary',true)",
                (thread_id, run_id),
            )
            self._event(
                conn,
                run_id,
                "repository_analysis_followup_accepted",
                parent_run_id=str(parent_run_id),
                copied_evidence=len(mapping),
                authorization_hash=authorization_hash,
            )
            return run_id

    def spec(self, run_id: UUID) -> AnalysisSpec:
        with self.engine.transaction() as conn:
            row = conn.execute(
                "SELECT s.*,r.status AS run_status,r.reason_code AS run_reason_code,"
                "r.outcome,r.execution_epoch,r.report_hash AS run_report_hash,"
                "t.thread_id,t.ticket_id FROM app.repo_analysis_runs s "
                "JOIN app.runs r USING(run_id) JOIN app.tickets t USING(ticket_id) "
                "WHERE s.run_id=%s",
                (run_id,),
            ).fetchone()
        if row is None:
            raise DomainError("NOT_FOUND", "Repository analysis run was not found", 404)
        return AnalysisSpec(
            run_id=UUID(str(row["run_id"])),
            thread_id=UUID(str(row["thread_id"])),
            ticket_id=UUID(str(row["ticket_id"])),
            repository=row["repository"],
            requested_ref=row["requested_ref"],
            commit=validate_commit(row["resolved_commit"]),
            task=row["task"],
            execution_mode=row["execution_mode"],
            limits=AnalysisLimits.model_validate(row["limits"]),
            model=AnalysisModelConfig.model_validate(row["model"]),
            deadline_at=row["deadline_at"],
            model_calls_used=row["model_calls_used"],
            tool_calls_used=row["tool_calls_used"],
            input_tokens_used=row["input_tokens_used"],
            output_tokens_used=row["output_tokens_used"],
            reserved_tokens=row["reserved_tokens"],
            stop_reason=row["stop_reason"],
            report_hash=row["run_report_hash"] or row["report_hash"],
            authorization_hash=row["authorization_hash"],
            profile_hash=row["profile_hash"],
            parent_run_id=UUID(str(row["parent_run_id"])) if row["parent_run_id"] else None,
            prior_report_hash=row["prior_report_hash"],
            prior_evidence_map=dict(row["prior_evidence_map"] or {}),
            execution_epoch=row["execution_epoch"],
            run_status=row["run_status"],
            run_reason_code=row["run_reason_code"],
            outcome=row["outcome"],
        )

    def list_runs(self, limit: int = 30) -> list[AnalysisSpec]:
        with self.engine.transaction() as conn:
            rows = conn.execute(
                "SELECT run_id FROM app.repo_analysis_runs ORDER BY created_at DESC LIMIT %s",
                (max(1, min(limit, 100)),),
            ).fetchall()
        return [self.spec(UUID(str(row["run_id"]))) for row in rows]

    def run_detail(self, run_id: UUID) -> dict[str, Any]:
        spec = self.spec(run_id)
        with self.engine.transaction() as conn:
            events = conn.execute(
                "SELECT event_id,kind,safe_payload,created_at FROM app.run_events WHERE run_id=%s "
                "ORDER BY created_at,event_id",
                (run_id,),
            ).fetchall()
            tools = conn.execute(
                "SELECT step_key,tool,status,result_uri,result_version,byte_count,evidence_id,"
                "error_code,created_at,finished_at FROM app.repo_tool_calls WHERE run_id=%s "
                "ORDER BY created_at",
                (run_id,),
            ).fetchall()
            claims = conn.execute(
                "SELECT section,statement,evidence_id,quote,start_line,end_line "
                "FROM app.repo_analysis_claims WHERE run_id=%s ORDER BY created_at",
                (run_id,),
            ).fetchall()
        report = None
        if spec.report_hash:
            try:
                report = self.artifact_bytes(spec.report_hash, run_id).decode("utf-8", "replace")
            except DomainError:
                report = None
        draft: dict[str, str] = {}
        for event in events:
            payload = event["safe_payload"]
            if event["kind"] == "model_stream_started" and payload.get("phase") == "analysis":
                draft = {}
            elif event["kind"] == "answer_delta" and payload.get("field") in {
                "conclusion",
                "structure",
            }:
                field = payload["field"]
                previous = draft.get(field, "")
                draft[field] = (
                    previous[: payload.get("offset", len(previous))] + payload.get("text", "")
                )[:12000]
            elif event["kind"] == "draft_rejected":
                draft = {}
        return {
            "run": spec.model_dump(mode="json"),
            "events": [
                {
                    "event_id": row["event_id"],
                    "kind": row["kind"],
                    "payload": row["safe_payload"],
                    "created_at": row["created_at"].isoformat(),
                }
                for row in events
            ],
            "tool_calls": [
                {
                    "step_key": row["step_key"],
                    "tool": row["tool"],
                    "status": row["status"],
                    "result_uri": row["result_uri"],
                    "result_version": row["result_version"],
                    "byte_count": row["byte_count"],
                    "evidence_id": str(row["evidence_id"]) if row["evidence_id"] else None,
                    "error_code": row["error_code"],
                    "created_at": row["created_at"].isoformat(),
                    "finished_at": (row["finished_at"].isoformat() if row["finished_at"] else None),
                }
                for row in tools
            ],
            "claims": [
                {
                    "section": row["section"],
                    "statement": row["statement"],
                    "evidence_id": str(row["evidence_id"]),
                    "quote": row["quote"],
                    "start_line": row["start_line"],
                    "end_line": row["end_line"],
                }
                for row in claims
            ],
            "report": report,
            "draft": draft if not spec.report_hash else {},
            "last_event_id": events[-1]["event_id"] if events else 0,
        }

    def stream_event(self, run_id: UUID, epoch: int, kind: str, **payload: Any) -> bool:
        with self.engine.transaction() as conn:
            try:
                run = self.engine._run(conn, run_id, epoch)
            except DomainError as exc:
                if exc.code == "FENCED":
                    return False
                raise
            if run["status"] != "running":
                return False
            self._event(conn, run_id, kind, **payload)
        return True

    def stream_batch(self, run_id: UUID, after: int) -> tuple[str, list[dict[str, Any]]]:
        with self.engine.transaction() as conn:
            # Read status before events: a terminal status must not race away the final deltas.
            run = self.engine._run(conn, run_id)
            if run["graph_version"] != REPOSITORY_ANALYSIS_GRAPH:
                raise DomainError("NOT_FOUND", "Repository analysis was not found", 404)
            rows = conn.execute(
                "SELECT event_id,kind,safe_payload FROM app.run_events "
                "WHERE run_id=%s AND event_id>%s ORDER BY event_id LIMIT 200",
                (run_id, after),
            ).fetchall()
        return run["status"], [
            {"event_id": row["event_id"], "kind": row["kind"], "payload": row["safe_payload"]}
            for row in rows
        ]

    def artifact_bytes(self, content_hash: str, run_id: UUID | None = None) -> bytes:
        """Read CAS content under the repository-analysis project scope.

        ``Engine.artifact_bytes`` routes through ``ToolAuthority``, whose ticket
        ownership query is bound to the configured e-commerce repository and
        policy.  Repository analysis threads use the requested public repository
        and a different policy, so this module performs the equivalent,
        project-scoped check locally instead of reusing that authority path.
        """

        with self.engine.transaction() as conn:
            if run_id is None:
                authorized = conn.execute(
                    "SELECT 1 FROM app.evidence e JOIN app.runs r USING(run_id) "
                    "JOIN app.tickets t USING(ticket_id) JOIN app.threads h USING(thread_id) "
                    "WHERE h.project_id=%s AND e.content_hash=%s",
                    (self.engine.settings.project_id, content_hash),
                ).fetchone()
            else:
                authorized = conn.execute(
                    "SELECT 1 FROM app.evidence WHERE run_id=%s AND content_hash=%s",
                    (run_id, content_hash),
                ).fetchone()
                if authorized is None:
                    authorized = conn.execute(
                        "SELECT 1 FROM app.runs WHERE run_id=%s AND report_hash=%s",
                        (run_id, content_hash),
                    ).fetchone()
        if authorized is None:
            raise DomainError("NOT_FOUND", "Artifact was not found in this analysis scope", 404)
        return self.engine.artifacts.read(content_hash)

    def begin_run(self, run_id: UUID) -> Any:
        return self.engine.begin_run(run_id)

    def pause(self, run_id: UUID) -> None:
        self.engine.pause(run_id)

    def cancel(self, run_id: UUID) -> None:
        self.engine.cancel(run_id)

    def resume(self, run_id: UUID) -> UUID | None:
        return self.engine.resume(run_id)

    def deadline_check(self, spec: AnalysisSpec) -> None:
        if datetime.now(UTC) >= spec.deadline_at:
            raise DomainError(
                "DEADLINE_EXCEEDED", "Repository analysis deadline has been reached", 403
            )

    def _spec_row(self, conn: Any, run_id: UUID, epoch: int) -> Any:
        run = self.engine._run(conn, run_id, epoch)
        if run["graph_version"] != REPOSITORY_ANALYSIS_GRAPH:
            raise DomainError("MODEL_SCOPE_DENIED", "Run is not a repository analysis", 403)
        row = conn.execute(
            "SELECT * FROM app.repo_analysis_runs WHERE run_id=%s FOR UPDATE", (run_id,)
        ).fetchone()
        if row is None:
            raise DomainError("NOT_FOUND", "Repository analysis specification is missing", 404)
        return run, row

    def reserve_repository_call(
        self,
        run_id: UUID,
        epoch: int,
        step_key: str,
        profile_hash: str,
        reservation_micros: int,
        call_limit: int | None,
        token_reservation: int = 0,
    ) -> UUID:
        with self.engine.transaction() as conn:
            run, spec = self._spec_row(conn, run_id, epoch)
            if run["status"] != "running":
                raise DomainError("FENCED", "Model call requires a running epoch")
            scope = conn.execute(
                "SELECT scope FROM app.mandates WHERE mandate_id=%s", (run["mandate_id"],)
            ).fetchone()
            if scope is None or scope["scope"].get("profile_hash") != profile_hash:
                raise DomainError("MODEL_SCOPE_DENIED", "Model call differs from its mandate", 403)
            limits = AnalysisLimits.model_validate(spec["limits"])
            if (
                limits.max_model_calls is not None
                and int(spec["model_calls_used"]) >= limits.max_model_calls
            ):
                raise DomainError(
                    "BUDGET_EXHAUSTED", "Repository analysis model-call budget is exhausted", 403
                )
            if datetime.now(UTC) >= spec["deadline_at"]:
                raise DomainError(
                    "DEADLINE_EXCEEDED", "Repository analysis deadline has been reached", 403
                )
            if conn.execute(
                "SELECT 1 FROM app.usage_calls WHERE run_id=%s AND step_key=%s", (run_id, step_key)
            ).fetchone():
                raise DomainError("MODEL_CALL_ALREADY_RECORDED", "Model step cannot be resent")
            tokens = max(2, token_reservation) if limits.max_total_tokens is not None else 0
            if (
                limits.max_total_tokens is not None
                and conn.execute(
                    "UPDATE app.repo_analysis_runs SET reserved_tokens=reserved_tokens+%s "
                    "WHERE run_id=%s "
                    "AND input_tokens_used+output_tokens_used+reserved_tokens+%s<=%s "
                    "RETURNING run_id",
                    (tokens, run_id, tokens, limits.max_total_tokens),
                ).fetchone()
                is None
            ):
                raise DomainError("TOKEN_BUDGET_EXHAUSTED", "Token reservation exceeds budget", 403)
            reserved = conn.execute(
                "UPDATE app.authorization_budgets SET reserved_micros=reserved_micros+%s,"
                "used_calls=used_calls+1 WHERE profile_hash=%s "
                "AND (call_limit IS NULL OR used_calls<call_limit) "
                "AND reserved_micros+settled_micros+%s<=limit_micros RETURNING profile_hash",
                (reservation_micros, profile_hash, reservation_micros),
            ).fetchone()
            local = conn.execute(
                "UPDATE app.budgets SET reserved_micros=reserved_micros+%s WHERE mandate_id=%s "
                "AND reserved_micros+settled_micros+%s<=limit_micros RETURNING mandate_id",
                (reservation_micros, run["mandate_id"], reservation_micros),
            ).fetchone()
            if reserved is None or local is None:
                raise DomainError(
                    "BUDGET_EXHAUSTED", "Model call exceeds its remaining budget", 403
                )
            call_id = uuid4()
            conn.execute(
                "INSERT INTO app.usage_calls(call_id,run_id,step_key,status,reserved_micros,"
                "profile_hash,token_reservation) VALUES (%s,%s,%s,'reserved',%s,%s,%s)",
                (call_id, run_id, step_key, reservation_micros, profile_hash, tokens),
            )
            conn.execute(
                "UPDATE app.repo_analysis_runs SET model_calls_used=model_calls_used+1 "
                "WHERE run_id=%s",
                (run_id,),
            )
            self._event(
                conn,
                run_id,
                "model_reserved",
                call_id=str(call_id),
                reserved_micros=reservation_micros,
            )
            return call_id

    def settle_call(
        self,
        call_id: UUID,
        cost_micros: int | None,
        usage: dict[str, Any] | None = None,
        result: Any | None = None,
    ) -> None:
        with self.engine.transaction() as conn:
            previous = conn.execute(
                "SELECT run_id,status,token_reservation FROM app.usage_calls "
                "WHERE call_id=%s FOR UPDATE",
                (call_id,),
            ).fetchone()
            self.engine.settle_call(call_id, cost_micros, usage, result)
            settled = conn.execute(
                "SELECT status FROM app.usage_calls WHERE call_id=%s",
                (call_id,),
            ).fetchone()
            if (
                previous
                and previous["status"] != "known"
                and settled
                and settled["status"] == "known"
            ):
                receipt = usage or {}
                conn.execute(
                    "UPDATE app.repo_analysis_runs SET input_tokens_used=input_tokens_used+%s,"
                    "output_tokens_used=output_tokens_used+%s,reserved_tokens=reserved_tokens-%s "
                    "WHERE run_id=%s",
                    (
                        receipt.get("prompt_tokens", 0),
                        receipt.get("completion_tokens", 0),
                        previous["token_reservation"],
                        previous["run_id"],
                    ),
                )

    def record_model_input(
        self, run_id: UUID, epoch: int, step_key: str, body: dict[str, Any]
    ) -> None:
        self.engine.record_model_input(run_id, epoch, step_key, body)

    def record_model_response(self, call_id: UUID, status: int, body: bytes) -> None:
        self.engine.record_model_response(call_id, status, body)

    def repository_model_result(
        self,
        run_id: UUID,
        epoch: int,
        step_key: str,
        profile_hash: str,
        schema: Any,
    ) -> Any | None:
        with self.engine.transaction() as conn:
            run = self.engine._run(conn, run_id, epoch)
            if run["status"] != "running":
                raise DomainError("FENCED", "Model result requires a running epoch")
            call = conn.execute(
                "SELECT * FROM app.usage_calls WHERE run_id=%s AND step_key=%s",
                (run_id, step_key),
            ).fetchone()
            if call is None:
                return None
            if call["profile_hash"] != profile_hash:
                raise DomainError("MODEL_SCOPE_DENIED", "Recorded call belongs to another profile")
            if call["status"] != "known":
                raise DomainError("USAGE_UNKNOWN", "Recorded model call cannot be resent")
            if call["result"] is None:
                raise DomainError(
                    "MODEL_RESULT_UNAVAILABLE", "Known failed model step cannot be resent"
                )
            return schema.model_validate(call["result"])

    def note_input_tokens(self, run_id: UUID, tokens: int) -> None:
        with self.engine.transaction() as conn:
            conn.execute(
                "UPDATE app.repo_analysis_runs SET input_tokens_used=input_tokens_used+%s "
                "WHERE run_id=%s",
                (max(0, tokens), run_id),
            )

    def start_tool(
        self,
        run_id: UUID,
        epoch: int,
        step_key: str,
        tool: str,
        args_hash: str,
    ) -> ToolAttempt:
        with self.engine.transaction() as conn:
            run, spec = self._spec_row(conn, run_id, epoch)
            if run["status"] != "running":
                raise DomainError("FENCED", "Tool call requires a running epoch")
            limits = AnalysisLimits.model_validate(spec["limits"])
            if datetime.now(UTC) >= spec["deadline_at"]:
                raise DomainError(
                    "DEADLINE_EXCEEDED", "Repository analysis deadline has been reached", 403
                )
            existing = conn.execute(
                "SELECT * FROM app.repo_tool_calls WHERE run_id=%s AND step_key=%s",
                (run_id, step_key),
            ).fetchone()
            if existing is not None:
                if existing["status"] == "succeeded":
                    reused = (
                        self.engine.artifacts.read(existing["result_hash"])
                        if existing["result_hash"]
                        else b""
                    )
                    return ToolAttempt(
                        call_id=UUID(str(existing["call_id"])),
                        step_key=step_key,
                        reused_result=reused,
                        evidence_id=UUID(str(existing["evidence_id"]))
                        if existing["evidence_id"]
                        else None,
                        source_version=existing["result_version"],
                        reference=existing["result_uri"],
                    )
                if existing["status"] == "failed":
                    raise DomainError(
                        "TOOL_CALL_ALREADY_FAILED",
                        "Failed read-only tool step cannot be resent",
                        409,
                    )
                conn.execute(
                    "UPDATE app.repo_tool_calls SET status='unknown' WHERE call_id=%s",
                    (existing["call_id"],),
                )
                self._event(conn, run_id, "tool_result_unknown", call_id=str(existing["call_id"]))
                raise DomainError(
                    "TOOL_RESULT_UNKNOWN", "Prior tool outcome is unknown and was not retried", 502
                )
            if (
                limits.max_tool_calls is not None
                and int(spec["tool_calls_used"]) >= limits.max_tool_calls
            ):
                raise DomainError(
                    "BUDGET_EXHAUSTED", "Repository analysis tool-call budget is exhausted", 403
                )
            call_id = uuid4()
            conn.execute(
                "INSERT INTO app.repo_tool_calls(call_id,run_id,step_key,tool,args_hash,status) "
                "VALUES (%s,%s,%s,%s,%s,'running')",
                (call_id, run_id, step_key, tool, args_hash),
            )
            conn.execute(
                "UPDATE app.repo_analysis_runs SET tool_calls_used=tool_calls_used+1 "
                "WHERE run_id=%s",
                (run_id,),
            )
            self._event(
                conn, run_id, "tool_reserved", call_id=str(call_id), tool=tool, step_key=step_key
            )
            return ToolAttempt(call_id=call_id, step_key=step_key)

    def finish_tool(
        self,
        call_id: UUID,
        epoch: int,
        *,
        outcome: ToolOutcome | None,
        error_code: str | None = None,
    ) -> UUID | None:
        with self.engine.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM app.repo_tool_calls WHERE call_id=%s FOR UPDATE", (call_id,)
            ).fetchone()
            if row is None:
                raise DomainError("CALL_NOT_FOUND", "Tool call was not found", 404)
            self.engine._run(conn, row["run_id"], epoch)
            if row["status"] != "running":
                return UUID(str(row["evidence_id"])) if row["evidence_id"] else None
            if outcome is None:
                conn.execute(
                    "UPDATE app.repo_tool_calls SET status='failed',error_code=%s,"
                    "finished_at=now() WHERE call_id=%s",
                    (error_code or "TOOL_FAILED", call_id),
                )
                self._event(
                    conn,
                    row["run_id"],
                    "tool_failed",
                    call_id=str(call_id),
                    error_code=error_code or "TOOL_FAILED",
                )
                return None
            data = outcome.text.encode("utf-8")
            content_hash = self.engine.artifacts.put(data)
            conn.execute(
                "INSERT INTO app.artifacts(content_hash,byte_count) VALUES (%s,%s) "
                "ON CONFLICT DO NOTHING",
                (content_hash, len(data)),
            )
            evidence_id: UUID | None = None
            if outcome.content is not None and outcome.evidence_kind is not None:
                evidence_id = uuid4()
                lines = max(1, outcome.content.count(b"\n") + 1)
                source_hash = self.engine.artifacts.put(outcome.content)
                conn.execute(
                    "INSERT INTO app.artifacts(content_hash,byte_count) VALUES (%s,%s) "
                    "ON CONFLICT DO NOTHING",
                    (source_hash, len(outcome.content)),
                )
                conn.execute(
                    "INSERT INTO app.evidence(evidence_id,run_id,project_id,source_uri,"
                    "source_version,content_hash,start_line,end_line,tool_call_id) "
                    "VALUES (%s,%s,%s,%s,%s,%s,1,%s,%s)",
                    (
                        evidence_id,
                        row["run_id"],
                        self.engine.settings.project_id,
                        outcome.reference,
                        outcome.source_version or hashlib.sha256(outcome.content).hexdigest(),
                        source_hash,
                        lines,
                        call_id,
                    ),
                )
            conn.execute(
                "UPDATE app.repo_tool_calls SET status='succeeded',result_hash=%s,"
                "result_uri=%s,result_version=%s,byte_count=%s,evidence_kind=%s,evidence_id=%s,"
                "finished_at=now() WHERE call_id=%s",
                (
                    content_hash,
                    outcome.reference,
                    outcome.source_version,
                    len(data),
                    outcome.evidence_kind,
                    evidence_id,
                    call_id,
                ),
            )
            self._event(
                conn,
                row["run_id"],
                "tool_observed",
                call_id=str(call_id),
                tool=row["tool"],
                byte_count=len(data),
                evidence_id=str(evidence_id) if evidence_id else None,
            )
            return evidence_id

    def evidence_rows(self, run_id: UUID) -> list[EvidenceRecord]:
        with self.engine.transaction() as conn:
            rows = conn.execute(
                "SELECT evidence_id,source_uri,source_version,content_hash,start_line,end_line "
                "FROM app.evidence WHERE run_id=%s ORDER BY observed_at",
                (run_id,),
            ).fetchall()
        return [
            EvidenceRecord(
                evidence_id=UUID(str(row["evidence_id"])),
                source_uri=row["source_uri"],
                source_version=row["source_version"],
                content_hash=row["content_hash"],
                start_line=row["start_line"],
                end_line=row["end_line"],
            )
            for row in rows
        ]

    def persist_claims(self, run_id: UUID, verification: ReportVerification) -> None:
        with self.engine.transaction() as conn:
            conn.execute("DELETE FROM app.repo_analysis_claims WHERE run_id=%s", (run_id,))
            for binding in verification.bindings:
                conn.execute(
                    "INSERT INTO app.repo_analysis_claims(claim_id,run_id,section,statement,"
                    "evidence_id,quote,start_line,end_line) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        uuid4(),
                        run_id,
                        binding.section,
                        binding.statement,
                        binding.evidence_id,
                        binding.quote,
                        binding.start_line,
                        binding.end_line,
                    ),
                )

    def finish_success(
        self,
        run_id: UUID,
        epoch: int,
        report: bytes,
        verification: ReportVerification,
    ) -> str | None:
        if verification.passed:
            self.persist_claims(run_id, verification)
        verdict = Verification(
            passed=verification.passed,
            checks=verification.checks,
            reasons=verification.failures,
        )
        self.engine.commit_report(run_id, epoch, report, verdict)
        spec = self.spec(run_id)
        with self.engine.transaction() as conn:
            conn.execute(
                "UPDATE app.repo_analysis_runs SET stop_reason=%s,report_hash=%s WHERE run_id=%s",
                (
                    "complete" if verification.passed else "EVIDENCE_REJECTED",
                    spec.report_hash,
                    run_id,
                ),
            )
        return spec.report_hash

    def finish_partial(
        self,
        run_id: UUID,
        epoch: int,
        report: bytes,
        reason_code: str,
    ) -> str:
        content_hash = self.engine.artifacts.put(report)
        with self.engine.transaction() as conn:
            run = self.engine._run(conn, run_id, epoch)
            if run["status"] != "running":
                raise DomainError("FENCED", "Partial report requires a running epoch")
            conn.execute(
                "INSERT INTO app.artifacts(content_hash,byte_count) VALUES (%s,%s) "
                "ON CONFLICT DO NOTHING",
                (content_hash, len(report)),
            )
            conn.execute(
                "INSERT INTO app.verdicts(verdict_id,run_id,report_hash,passed,details) "
                "VALUES (%s,%s,%s,false,%s) ON CONFLICT(run_id,report_hash) DO NOTHING",
                (
                    uuid4(),
                    run_id,
                    content_hash,
                    Jsonb(
                        {
                            "checks": ["partial_report"],
                            "reasons": [reason_code],
                            "scope": "repository_analysis_partial",
                        }
                    ),
                ),
            )
            conn.execute(
                "UPDATE app.runs SET status='waiting_input',outcome='partial_report',"
                "reason_code=%s,report_hash=%s WHERE run_id=%s",
                (reason_code, content_hash, run_id),
            )
            conn.execute(
                "UPDATE app.repo_analysis_runs SET stop_reason=%s,report_hash=%s WHERE run_id=%s",
                (reason_code, content_hash, run_id),
            )
            self._event(
                conn,
                run_id,
                "repository_analysis_partial",
                reason_code=reason_code,
                report_hash=content_hash,
            )
        return content_hash

    def fail(self, run_id: UUID, reason_code: str, epoch: int | None = None) -> None:
        self.engine.fail_run(run_id, reason_code, epoch)
        with self.engine.transaction() as conn:
            conn.execute(
                "UPDATE app.repo_analysis_runs SET stop_reason=%s WHERE run_id=%s",
                (reason_code, run_id),
            )
