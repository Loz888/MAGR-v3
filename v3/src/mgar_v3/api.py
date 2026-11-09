import asyncio
import hashlib
import hmac
import json
import logging
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Annotated, Any, cast
from uuid import UUID, uuid4

import psycopg
from fastapi import Depends, FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from mgar_v3.commerce.api import register as register_commerce
from mgar_v3.coordination.engine import COMMAND, DUPLEX_VERSIONS, Engine
from mgar_v3.domain import ApprovalInput, DomainError, MessageInput, RunInput, TriggerInput
from mgar_v3.effects.dispatcher import Dispatcher, Transport
from mgar_v3.effects.github import GitHubMCPTransport
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.graph.live_investigator import LiveInvestigator
from mgar_v3.graph.runner import Runner
from mgar_v3.knowledge.api import register
from mgar_v3.live import load_permit
from mgar_v3.mcp.r4 import server
from mgar_v3.proactivity.api import register as register_proactivity
from mgar_v3.recovery.api import register as register_recovery
from mgar_v3.repair.domain import ApprovalDecision, EditDecision, RepairInput
from mgar_v3.repair.graph import acknowledge_approval
from mgar_v3.repair.live import repair_permit
from mgar_v3.repair.publication import LiveDraftMCPTransport
from mgar_v3.repository_analysis.domain import (
    REPOSITORY_ANALYSIS_GRAPH,
    CreateAnalysisInput,
    FollowUpInput,
)
from mgar_v3.repository_analysis.service import RepositoryAnalysisService
from mgar_v3.routing.domain import CorrectionInput, EventInput
from mgar_v3.routing.live import load_router_permit
from mgar_v3.routing.model import DeepSeekRouter
from mgar_v3.routing.worker import FixtureRouter, RouterProvider, RouterWorker
from mgar_v3.settings import ROOT, Settings


class BodyLimitMiddleware:
    def __init__(self, app: ASGIApp, max_bytes: int):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body.extend(message.get("body", b""))
            if len(body) > self.max_bytes:
                response = JSONResponse({"code": "BODY_TOO_LARGE"}, status_code=413)
                await response(scope, receive, send)
                return
            if not message.get("more_body", False):
                break
        replayed = False

        async def replay() -> Message:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


def create_app(
    settings: Settings | None = None,
    engine: Engine | None = None,
    run_background: bool = True,
    transport: Transport | None = None,
    draft_transport: Transport | None = None,
) -> FastAPI:
    configuration = settings or Settings()
    provided = engine
    live_permit = None
    if configuration.live_profile_path and configuration.live_authorization_path:
        live_permit = load_permit(
            configuration.live_profile_path, configuration.live_authorization_path, configuration
        )
    selected_transport = transport
    selected_draft = draft_transport
    live_repair_permit = repair_permit(configuration) if configuration.repair_profile_path else None
    router_provider: RouterProvider | None = None
    if (
        selected_transport is None
        and live_permit is not None
        and configuration.github_write_token is not None
        and configuration.allow_external_write
    ):
        selected_transport = GitHubMCPTransport(configuration, live_permit)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        nonlocal selected_draft
        authority = provided or Engine(configuration, ArtifactStore(configuration.artifact_root))
        if provided is None:
            authority.recover()
        from mgar_v3.repair.sandbox import Sandbox

        # Startup owns the Engine lock; no Run worker is active during orphan cleanup.
        for run in authority.list_runs():
            if run["graph_version"] == "r5-repair-v1":
                try:
                    Sandbox(str(run["run_id"])).cleanup()
                except DomainError:
                    with authority.transaction() as conn:
                        authority._event(conn, run["run_id"], "sandbox_cleanup_unavailable")
        app.state.engine = authority
        repo_service = app.state.repo_service = RepositoryAnalysisService(
            authority, configuration
        )
        if (
            selected_draft is None
            and live_repair_permit is not None
            and live_repair_permit.authorization.allow_external_write
            and configuration.allow_external_write
        ):
            selected_draft = LiveDraftMCPTransport(authority, live_repair_permit)
        stopped = asyncio.Event()
        workers: dict[UUID, asyncio.Task[None]] = {}
        effect: asyncio.Task[Any] | None = None
        router_task: asyncio.Task[Any] | None = None
        index_task: asyncio.Task[Any] | None = None
        nonlocal router_provider
        if configuration.router_mode == "fixture":
            router_provider = FixtureRouter()
        elif (
            configuration.router_mode == "live"
            and configuration.router_profile_path
            and configuration.router_authorization_path
        ):
            router_provider = DeepSeekRouter(
                authority,
                load_router_permit(
                    configuration.router_profile_path,
                    configuration.router_authorization_path,
                    configuration,
                ),
            )

        async def execute_run(run: dict[str, Any]) -> None:
            if run["graph_version"] == REPOSITORY_ANALYSIS_GRAPH:
                await asyncio.to_thread(repo_service.execute, run["run_id"])
                return
            runner = Runner(authority)
            try:
                if (
                    run["execution_mode"] == "live"
                    and run["graph_version"] != "r5-repair-v1"
                    and not authority.commerce.binding(run["run_id"])
                ):
                    if live_permit is None:
                        raise DomainError("LIVE_PROFILE_REQUIRED", "Live profile is missing")
                    runner = Runner(
                        authority, LiveInvestigator(authority, run["run_id"], live_permit)
                    )
                await asyncio.to_thread(runner.execute, run["run_id"])
            except DomainError as exc:
                if exc.code not in {"FENCED", "RUN_NOT_QUEUED"}:
                    authority.fail_run(
                        run["run_id"], exc.code, getattr(runner, "execution_epoch", None)
                    )
            except (OSError, ValueError):
                authority.fail_run(
                    run["run_id"], "EXECUTION_FAILED", getattr(runner, "execution_epoch", None)
                )
            except psycopg.Error:
                raise
            except Exception:
                authority.fail_run(
                    run["run_id"], "EXECUTION_FAILED", getattr(runner, "execution_epoch", None)
                )

        async def supervise() -> None:
            nonlocal effect, router_task, index_task
            while not stopped.is_set():
                for run_id, worker in list(workers.items()):
                    if worker.done():
                        worker.result()
                        del workers[run_id]
                if effect is not None and effect.done():
                    effect.result()
                    effect = None
                if router_task is not None and router_task.done():
                    router_task.result()
                    router_task = None
                if index_task is not None and index_task.done():
                    index_task.result()
                    index_task = None
                if index_task is None:
                    pending = [j for j in authority.knowledge.jobs() if j["status"] == "queued"]
                    if pending:

                        async def index_one(job_id: UUID) -> None:
                            try:
                                await asyncio.to_thread(authority.knowledge.index, job_id)
                            except DomainError:
                                pass  # Durable failed job retains its reason.

                        index_task = asyncio.create_task(index_one(pending[-1]["job_id"]))
                for run in authority.list_runs():
                    if (
                        run["status"] != "queued"
                        or run["run_id"] in workers
                        or len(workers) >= configuration.max_concurrent_runs
                    ):
                        continue
                    workers[run["run_id"]] = asyncio.create_task(execute_run(run))
                if router_provider is not None and router_task is None:
                    router_task = asyncio.create_task(
                        asyncio.to_thread(
                            RouterWorker(authority, router_provider, live_permit).tick
                        )
                    )
                if (
                    selected_transport is not None
                    or selected_draft is not None
                    or configuration.allow_external_write
                ) and effect is None:

                    def dispatch_one() -> None:
                        for outgoing in (
                            selected_transport,
                            selected_draft,
                            *authority.commerce.ready_transports(),
                        ):
                            if (
                                outgoing is not None
                                and Dispatcher(authority, outgoing).tick() is not None
                            ):
                                return

                    effect = asyncio.create_task(asyncio.to_thread(dispatch_one))
                try:
                    await asyncio.wait_for(stopped.wait(), timeout=0.25)
                except TimeoutError:
                    pass

        task = asyncio.create_task(supervise()) if run_background else None
        app.state.supervisor = task
        try:
            async with AsyncExitStack() as stack:
                for mcp_server in mcp_servers:
                    await stack.enter_async_context(mcp_server.session_manager.run())
                yield
        finally:
            stopped.set()
            if task is not None:
                await task
                for run_id in workers:
                    run = authority.get_run(run_id)
                    if run["graph_version"] in DUPLEX_VERSIONS and run["status"] == "running":
                        authority.pause(run_id)
                await asyncio.gather(*workers.values())
                if effect is not None:
                    await effect
                if router_task is not None:
                    await router_task
                if index_task is not None:
                    await index_task
            if provided is None:
                authority.close()

    app = FastAPI(title="MGAR V3.1 R4", lifespan=lifespan)
    mcp_servers = [server(role, lambda: app.state.engine) for role in ("knowledge", "data")]
    for role, mcp_server in zip(("knowledge", "data"), mcp_servers, strict=True):
        app.mount("/mcp/" + role, mcp_server.streamable_http_app())
    app.mount("/static", StaticFiles(directory=ROOT / "web"), name="static")
    app.add_middleware(BodyLimitMiddleware, max_bytes=configuration.max_body_bytes)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]"])

    @app.exception_handler(DomainError)
    async def domain_error(request: Request, exc: DomainError) -> JSONResponse:
        return JSONResponse(
            {
                "code": exc.code,
                "message": str(exc),
                "retryable": False,
                "request_id": str(uuid4()),
                "details": {},
            },
            status_code=exc.status,
        )

    @app.exception_handler(RequestValidationError)
    async def invalid_input(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            {
                "code": "INVALID_INPUT",
                "message": "Request schema is invalid",
                "retryable": False,
                "request_id": str(uuid4()),
                "details": {},
            },
            status_code=422,
        )

    def authority(request: Request) -> Engine:
        return cast(Engine, request.app.state.engine)

    def repository_service(request: Request) -> RepositoryAnalysisService:
        return cast(RepositoryAnalysisService, request.app.state.repo_service)

    def authenticated(request: Request) -> None:
        supplied = request.headers.get("X-MGAR-Token", "")
        if not hmac.compare_digest(
            supplied.encode(), configuration.local_token.get_secret_value().encode()
        ):
            raise DomainError("AUTH_REQUIRED", "Local authentication is required", 401)
        origin = request.headers.get("origin")
        if origin and origin not in {
            "http://127.0.0.1:18080",
            "http://localhost:18080",
            "http://127.0.0.1:18081",
            "http://localhost:18081",
        }:
            raise DomainError("ORIGIN_DENIED", "Origin is not allowed", 403)

    @app.get("/api/v3/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "alive", "release_status": "ENGINEERING_INCOMPLETE"}

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(
            ROOT / "web/index.html",
            headers={
                "Content-Security-Policy": (
                    "default-src 'self'; script-src 'self'; style-src 'self'; "
                    "connect-src 'self'; frame-ancestors 'none'"
                ),
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.get("/api/v3/readyz", dependencies=[Depends(authenticated)])
    def readyz(request: Request, service: Annotated[Engine, Depends(authority)]) -> dict[str, str]:
        supervisor = request.app.state.supervisor
        if supervisor is not None and supervisor.done():
            raise DomainError("SUPERVISOR_UNAVAILABLE", "Execution supervisor is unavailable", 503)
        with service.transaction() as conn:
            migrations = conn.execute("SELECT count(*) AS n FROM app.schema_migrations").fetchone()
            if migrations is None or migrations["n"] < 11:
                raise DomainError("MIGRATIONS_PENDING", "Database migrations are incomplete", 503)
        return {"database": "ready", "release_status": "ENGINEERING_INCOMPLETE"}

    @app.post("/api/v3/events", status_code=202, dependencies=[Depends(authenticated)])
    def accept_event(body: EventInput, service: Annotated[Engine, Depends(authority)]) -> Any:
        text = body.body
        if body.intent == "investigate" and not text.startswith(("@mgar ", COMMAND)):
            text = "@mgar " + text
        event = TriggerInput(
            source=body.source,
            external_event_id=body.client_request_id,
            project_id=configuration.project_id,
            repository=configuration.repository,
            issue_number=body.issue_number,
            actor=configuration.local_actor,
            body=text,
            execution_mode=body.execution_mode,
        )
        return service.ingest(event, live_permit if body.execution_mode == "live" else None)

    @app.get("/api/v3/tickets", dependencies=[Depends(authenticated)])
    def tickets(service: Annotated[Engine, Depends(authority)], limit: int = 50) -> Any:
        return jsonable_encoder(service.routing.tickets(limit))

    @app.get("/api/v3/tickets/{ticket_id}", dependencies=[Depends(authenticated)])
    def ticket(ticket_id: UUID, service: Annotated[Engine, Depends(authority)]) -> Any:
        return jsonable_encoder(service.routing.ticket(ticket_id))

    @app.post("/api/v3/tickets/{ticket_id}/{control}", dependencies=[Depends(authenticated)])
    def ticket_control(
        ticket_id: UUID, control: str, service: Annotated[Engine, Depends(authority)]
    ) -> Any:
        if control not in {"pause", "resume", "cancel"}:
            raise DomainError("CONTROL_UNSUPPORTED", "Ticket control is unsupported", 400)
        current = service.routing.owned_ticket(ticket_id)
        result = getattr(service, control)(current["run_id"])
        return jsonable_encoder(
            {"ticket_id": ticket_id, "run_id": result} if control == "resume" else result
        )

    @app.get("/repo", include_in_schema=False)
    def repository_analysis_page() -> FileResponse:
        return FileResponse(
            ROOT / "web/repo.html",
            headers={
                "Content-Security-Policy": (
                    "default-src 'self'; script-src 'self'; style-src 'self'; "
                    "connect-src 'self'; frame-ancestors 'none'"
                ),
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.get(
        "/api/v3/repository-analyses/profile", dependencies=[Depends(authenticated)]
    )
    def repository_analysis_profile(
        service: Annotated[RepositoryAnalysisService, Depends(repository_service)],
    ) -> Any:
        return jsonable_encoder(service.profile())

    @app.post(
        "/api/v3/repository-analyses",
        status_code=202,
        dependencies=[Depends(authenticated)],
    )
    def create_repository_analysis(
        body: CreateAnalysisInput,
        service: Annotated[RepositoryAnalysisService, Depends(repository_service)],
    ) -> Any:
        return jsonable_encoder(service.create(body, actor=configuration.local_actor))

    @app.get("/api/v3/repository-analyses", dependencies=[Depends(authenticated)])
    def list_repository_analyses(
        service: Annotated[RepositoryAnalysisService, Depends(repository_service)],
        limit: int = 30,
    ) -> Any:
        return jsonable_encoder(service.list(limit))

    @app.get(
        "/api/v3/repository-analyses/{run_id}", dependencies=[Depends(authenticated)]
    )
    def repository_analysis_detail(
        run_id: UUID,
        service: Annotated[RepositoryAnalysisService, Depends(repository_service)],
    ) -> Any:
        return jsonable_encoder(service.detail(run_id))

    @app.get(
        "/api/v3/repository-analyses/{run_id}/report",
        dependencies=[Depends(authenticated)],
    )
    def repository_analysis_report(
        run_id: UUID,
        service: Annotated[RepositoryAnalysisService, Depends(repository_service)],
    ) -> Response:
        return Response(
            service.report_bytes(run_id),
            media_type="text/markdown",
            headers={"Cache-Control": "no-store"},
        )

    @app.post(
        "/api/v3/repository-analyses/{run_id}/messages",
        status_code=202,
        dependencies=[Depends(authenticated)],
    )
    def repository_analysis_message(
        run_id: UUID,
        body: FollowUpInput,
        service: Annotated[RepositoryAnalysisService, Depends(repository_service)],
    ) -> Any:
        return jsonable_encoder(service.follow_up(run_id, body))

    @app.post(
        "/api/v3/repository-analyses/{run_id}/pause",
        dependencies=[Depends(authenticated)],
    )
    def pause_repository_analysis(
        run_id: UUID,
        service: Annotated[RepositoryAnalysisService, Depends(repository_service)],
    ) -> dict[str, str]:
        service.pause(run_id)
        return {"run_id": str(run_id), "status": "paused"}

    @app.post(
        "/api/v3/repository-analyses/{run_id}/cancel",
        dependencies=[Depends(authenticated)],
    )
    def cancel_repository_analysis(
        run_id: UUID,
        service: Annotated[RepositoryAnalysisService, Depends(repository_service)],
    ) -> dict[str, str]:
        service.cancel(run_id)
        return {"run_id": str(run_id), "status": "cancelled"}

    @app.post(
        "/api/v3/repository-analyses/{run_id}/resume",
        dependencies=[Depends(authenticated)],
    )
    def resume_repository_analysis(
        run_id: UUID,
        service: Annotated[RepositoryAnalysisService, Depends(repository_service)],
    ) -> dict[str, str]:
        return {"run_id": str(service.resume(run_id))}

    @app.get("/api/v3/router/status", dependencies=[Depends(authenticated)])
    def router_status(service: Annotated[Engine, Depends(authority)]) -> Any:
        return service.routing.metrics()

    @app.post("/api/v3/router/corrections", dependencies=[Depends(authenticated)])
    def correct_route(body: CorrectionInput, service: Annotated[Engine, Depends(authority)]) -> Any:
        return {
            "correction_id": str(
                service.routing.correct(body.event_ids, body.expected_thread_revision, body.reason)
            )
        }

    @app.post("/api/v3/runs", status_code=202, dependencies=[Depends(authenticated)])
    def create_run(body: RunInput, service: Annotated[Engine, Depends(authority)]) -> Any:
        event = TriggerInput(
            source="cli",
            external_event_id=body.client_request_id,
            project_id=body.project_id,
            repository=configuration.repository,
            issue_number=body.issue_number,
            actor=configuration.local_actor,
            body=COMMAND + body.question,
            execution_mode=body.execution_mode,
        )
        with service.transaction():
            receipt = service.ingest(event, live_permit if body.execution_mode == "live" else None)
            if receipt.run_id is not None:
                service.tools.requested_snapshots(
                    receipt.run_id, body.knowledge_snapshot_ids, receipt.duplicate
                )
            return receipt

    @app.get("/api/v3/runs", dependencies=[Depends(authenticated)])
    def list_runs(service: Annotated[Engine, Depends(authority)]) -> Any:
        return jsonable_encoder(service.list_runs())

    @app.post("/api/v3/repairs", status_code=202, dependencies=[Depends(authenticated)])
    def create_repair(body: RepairInput, service: Annotated[Engine, Depends(authority)]) -> Any:
        return jsonable_encoder(service.repairs.create(body, live_repair_permit))

    @app.get("/api/v3/repairs/profile", dependencies=[Depends(authenticated)])
    def repair_configuration(service: Annotated[Engine, Depends(authority)]) -> Any:
        if live_repair_permit is None:
            return {"available": False, "reason_code": "R5_PROFILE_REQUIRED"}
        p = live_repair_permit.profile
        code = None
        try:
            live_repair_permit.validate()
        except DomainError as exc:
            code = exc.code
        with service.transaction() as conn:
            budget = conn.execute(
                "SELECT used_calls,settled_micros,reserved_micros "
                "FROM app.authorization_budgets WHERE profile_hash=%s",
                (p.profile_hash,),
            ).fetchone()
        return {
            "available": code is None,
            "reason_code": code,
            "profile_hash": p.profile_hash,
            "repository": p.repository,
            "project_id": p.project_id,
            "issue_number": p.issue_number,
            "question": p.question,
            "base_sha": p.source_ref,
            "source_path": p.source_path,
            "model_id": p.model_id,
            "max_model_calls": p.max_model_calls,
            "max_cost_micros": p.max_cost_micros,
            "used_calls": budget["used_calls"] if budget else 0,
        }

    @app.get("/api/v3/runs/{run_id}/repair", dependencies=[Depends(authenticated)])
    def repair_detail(run_id: UUID, service: Annotated[Engine, Depends(authority)]) -> Any:
        return jsonable_encoder(service.repairs.detail(run_id))

    @app.get(
        "/api/v3/runs/{run_id}/repair/artifacts/{content_hash}",
        dependencies=[Depends(authenticated)],
    )
    def repair_artifact(
        run_id: UUID, content_hash: str, service: Annotated[Engine, Depends(authority)]
    ) -> Response:
        return Response(
            service.repairs.artifact_bytes(run_id, content_hash),
            media_type="application/octet-stream",
        )

    @app.post(
        "/api/v3/runs/{run_id}/actions/{action_id}/reject", dependencies=[Depends(authenticated)]
    )
    def reject_repair(
        run_id: UUID,
        action_id: UUID,
        body: ApprovalDecision,
        service: Annotated[Engine, Depends(authority)],
    ) -> Any:
        if not any(a["action_id"] == action_id for a in service.actions(run_id)):
            raise DomainError("ACTION_RUN_MISMATCH", "Action does not belong to this Run", 404)
        return jsonable_encoder(
            service.repairs.decision(
                action_id, body.payload_hash, configuration.local_actor, "reject", body.reason
            )
        )

    @app.post(
        "/api/v3/runs/{run_id}/actions/{action_id}/revise",
        status_code=202,
        dependencies=[Depends(authenticated)],
    )
    def revise_repair(
        run_id: UUID,
        action_id: UUID,
        body: EditDecision,
        service: Annotated[Engine, Depends(authority)],
    ) -> Any:
        if not any(a["action_id"] == action_id for a in service.actions(run_id)):
            raise DomainError("ACTION_RUN_MISMATCH", "Action does not belong to this Run", 404)
        service.repairs.revise(action_id, body.payload_hash, body.diff, configuration.local_actor)
        return {"status": "queued_for_retesting"}

    @app.post(
        "/api/v3/runs/{run_id}/actions/{action_id}/continue", dependencies=[Depends(authenticated)]
    )
    def continue_draft(
        run_id: UUID,
        action_id: UUID,
        body: ApprovalDecision,
        service: Annotated[Engine, Depends(authority)],
    ) -> Any:
        if not any(a["action_id"] == action_id for a in service.actions(run_id)):
            raise DomainError("ACTION_RUN_MISMATCH", "Action does not belong to this Run", 404)
        return jsonable_encoder(
            service.repairs.continue_publication(
                action_id, body.payload_hash, configuration.local_actor
            )
        )

    @app.get("/api/v3/runs/{run_id}", dependencies=[Depends(authenticated)])
    def get_run(run_id: UUID, service: Annotated[Engine, Depends(authority)]) -> Any:
        return jsonable_encoder(service.get_run(run_id))

    @app.post(
        "/api/v3/runs/{run_id}/messages", status_code=202, dependencies=[Depends(authenticated)]
    )
    def message(
        run_id: UUID,
        body: MessageInput,
        service: Annotated[Engine, Depends(authority)],
        repo: Annotated[RepositoryAnalysisService, Depends(repository_service)],
    ) -> Any:
        if service.get_run(run_id)["graph_version"] == REPOSITORY_ANALYSIS_GRAPH:
            return jsonable_encoder(
                repo.follow_up(
                    run_id, FollowUpInput(client_request_id=body.client_request_id, body=body.body)
                )
            )
        if service.commerce.binding(run_id):
            raise DomainError(
                "COMMERCE_CASE_MESSAGE_REQUIRED",
                "Send this message through the versioned Case endpoint",
            )
        selected = (
            live_repair_permit
            if service.get_run(run_id)["graph_version"] == "r5-repair-v1"
            else live_permit
        )
        return service.follow_up(run_id, body.client_request_id, body.body, selected)

    @app.get("/api/v3/runs/{run_id}/report", dependencies=[Depends(authenticated)])
    def report(run_id: UUID, service: Annotated[Engine, Depends(authority)]) -> Response:
        run = service.get_run(run_id)
        if not run["report_hash"]:
            raise DomainError("REPORT_NOT_READY", "Report is not available", 404)
        link = service.commerce.binding(run_id)
        content = (
            service.commerce.artifact_bytes(link["case_id"], run["report_hash"])
            if link
            else service.artifact_bytes(run["report_hash"])
        )
        return Response(content, media_type="text/markdown", headers={"Cache-Control": "no-store"})

    @app.get("/api/v3/runs/{run_id}/actions", dependencies=[Depends(authenticated)])
    def actions(run_id: UUID, service: Annotated[Engine, Depends(authority)]) -> Any:
        return jsonable_encoder(service.actions(run_id))

    @app.get("/api/v3/evidence/{evidence_id}", dependencies=[Depends(authenticated)])
    def evidence(evidence_id: UUID, service: Annotated[Engine, Depends(authority)]) -> Any:
        return jsonable_encoder(service.evidence_by_id(evidence_id))

    @app.get("/api/v3/artifacts/{content_hash}", dependencies=[Depends(authenticated)])
    def artifact(content_hash: str, service: Annotated[Engine, Depends(authority)]) -> Response:
        return Response(service.artifact_bytes(content_hash), media_type="application/octet-stream")

    @app.post("/api/v3/runs/{run_id}/approvals", dependencies=[Depends(authenticated)])
    def approve(
        run_id: UUID, body: ApprovalInput, service: Annotated[Engine, Depends(authority)]
    ) -> Any:
        if not any(action["action_id"] == body.action_id for action in service.actions(run_id)):
            raise DomainError("ACTION_RUN_MISMATCH", "Action does not belong to this Run", 404)
        result = service.approve(body.action_id, body.payload_hash, configuration.local_actor)
        acknowledge_approval(service, run_id, body.payload_hash)
        return jsonable_encoder(result)

    @app.post("/api/v3/runs/{run_id}/cancel", dependencies=[Depends(authenticated)])
    def cancel(run_id: UUID, service: Annotated[Engine, Depends(authority)]) -> Any:
        return jsonable_encoder(service.cancel(run_id))

    @app.post("/api/v3/runs/{run_id}/pause", dependencies=[Depends(authenticated)])
    def pause(run_id: UUID, service: Annotated[Engine, Depends(authority)]) -> Any:
        return jsonable_encoder(service.pause(run_id))

    @app.post("/api/v3/runs/{run_id}/resume", dependencies=[Depends(authenticated)])
    def resume(run_id: UUID, service: Annotated[Engine, Depends(authority)]) -> dict[str, str]:
        return {"run_id": str(service.resume(run_id))}

    @app.post(
        "/api/v3/runs/{run_id}/actions/{action_id}/reconcile", dependencies=[Depends(authenticated)]
    )
    def reconcile(
        run_id: UUID, action_id: UUID, service: Annotated[Engine, Depends(authority)]
    ) -> dict[str, bool]:
        action = next(
            (item for item in service.actions(run_id) if item["action_id"] == action_id), None
        )
        if action is None:
            raise DomainError("ACTION_RUN_MISMATCH", "Action does not belong to this Run", 404)
        outgoing = selected_draft if action["action_type"] == "draft_pr" else selected_transport
        if service.commerce.binding(run_id) and service.get_run(run_id)["execution_mode"] == "live":
            permit = service.commerce.bound_permit(run_id, read_only=True)
            assert permit is not None
            outgoing = LiveDraftMCPTransport(service, permit)
        if outgoing is None:
            raise DomainError(
                "TRANSPORT_NOT_CONFIGURED", "Reconciliation transport is unavailable", 503
            )
        return {"reconciled": Dispatcher(service, outgoing).reconcile(action_id)}

    @app.get("/api/v3/runs/{run_id}/events", dependencies=[Depends(authenticated)])
    async def events(
        run_id: UUID,
        request: Request,
        service: Annotated[Engine, Depends(authority)],
        follow: bool = False,
    ) -> StreamingResponse:
        service.get_run(run_id)
        try:
            cursor = int(request.headers.get("Last-Event-ID", "0"))
        except ValueError as exc:
            raise DomainError("INVALID_CURSOR", "Last-Event-ID must be an integer", 400) from exc
        if cursor < 0:
            raise DomainError("INVALID_CURSOR", "Last-Event-ID cannot be negative", 400)

        async def stream() -> AsyncIterator[str]:
            nonlocal cursor
            while True:
                for event in service.events(run_id, cursor):
                    cursor = event["event_id"]
                    data = json.dumps(jsonable_encoder(event), ensure_ascii=False)
                    yield f"id: {cursor}\nevent: {event['kind']}\ndata: {data}\n\n"
                if not follow or service.get_run(run_id)["status"] in {
                    "succeeded",
                    "failed",
                    "cancelled",
                }:
                    return
                if await request.is_disconnected():
                    return
                await asyncio.sleep(0.25)

        return StreamingResponse(
            stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"}
        )

    @app.post("/api/v3/webhooks/github", status_code=202)
    async def webhook(request: Request, service: Annotated[Engine, Depends(authority)]) -> Any:
        secret = configuration.github_webhook_secret.get_secret_value()
        if not secret:
            raise DomainError(
                "WEBHOOK_NOT_CONFIGURED", "Webhook verification is not configured", 503
            )
        raw = await request.body()
        expected = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(
            expected.encode(), request.headers.get("X-Hub-Signature-256", "").encode()
        ):
            raise DomainError("SIGNATURE_INVALID", "Webhook signature is invalid", 401)
        delivery = request.headers.get("X-GitHub-Delivery")
        if not delivery:
            raise DomainError(
                "DELIVERY_ID_REQUIRED", "Webhook delivery identifier is required", 400
            )
        try:
            payload = json.loads(raw)
            if (
                request.headers.get("X-GitHub-Event") == "workflow_run"
                and payload.get("action") == "completed"
                and payload.get("workflow_run", {}).get("conclusion") in {"failure", "timed_out"}
            ):
                from mgar_v3.proactivity.domain import ProactiveEvent

                workflow = payload["workflow_run"]
                pulls = workflow.get("pull_requests", [])
                observations = service.proactivity.observe(
                    ProactiveEvent(
                        external_event_id=delivery,
                        repository=payload["repository"]["full_name"],
                        actor=payload["sender"]["login"],
                        kind="ci_failure",
                        issue_number=pulls[0]["number"] if pulls else 1,
                        source_ref=workflow["head_sha"],
                        body=str(workflow.get("name", ""))[:8000],
                    ),
                    live_permit,
                )
                return jsonable_encoder({"status": "observed", "observations": observations})
            if (
                request.headers.get("X-GitHub-Event") != "issue_comment"
                or payload.get("action") != "created"
            ):
                return {"status": "ignored", "reason": "unsupported_event"}
            if "pull_request" in payload["issue"]:
                return {"status": "ignored", "reason": "r1_issue_comments_only"}
            event = TriggerInput(
                source="github",
                external_event_id=delivery,
                project_id=configuration.project_id,
                repository=payload["repository"]["full_name"],
                issue_number=payload["issue"]["number"],
                actor=payload["sender"]["login"],
                body=payload["comment"]["body"],
                execution_mode="live" if live_permit is not None else "fixture",
            )
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise DomainError("INVALID_WEBHOOK", "Webhook payload is invalid", 422) from exc
        if service.is_own_result(event.repository, event.issue_number, event.body):
            return {"status": "ignored", "reason": "published_result"}
        return service.ingest(event, live_permit)

    @app.exception_handler(psycopg.Error)
    async def database_unavailable(request: Request, exc: psycopg.Error) -> JSONResponse:
        logging.getLogger("mgar_v3.api").exception(
            "database error while handling %s %s", request.method, request.url.path
        )
        return JSONResponse(
            {
                "code": "STORAGE_UNAVAILABLE",
                "message": "Database operation failed",
                "retryable": False,
                "request_id": str(uuid4()),
                "details": {},
            },
            status_code=503,
        )

    register(app, authority, authenticated)
    register_commerce(app, authority, authenticated)
    register_recovery(app, configuration, authority, authenticated)
    register_proactivity(app, authority, authenticated, live_permit)
    return app
