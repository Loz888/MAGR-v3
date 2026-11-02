import asyncio
import hashlib
import hmac
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any, cast
from uuid import UUID, uuid4

import psycopg
from fastapi import Depends, FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from mgar_v3.coordination.engine import COMMAND, Engine
from mgar_v3.domain import ApprovalInput, DomainError, MessageInput, RunInput, TriggerInput
from mgar_v3.effects.dispatcher import Dispatcher, Transport
from mgar_v3.effects.github import GitHubMCPTransport
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.graph.live_investigator import LiveInvestigator
from mgar_v3.graph.runner import Runner
from mgar_v3.live import load_permit
from mgar_v3.settings import Settings


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
) -> FastAPI:
    configuration = settings or Settings()
    provided = engine
    live_permit = None
    if configuration.live_profile_path and configuration.live_authorization_path:
        live_permit = load_permit(
            configuration.live_profile_path, configuration.live_authorization_path, configuration
        )
    selected_transport = transport
    if (
        selected_transport is None
        and live_permit is not None
        and configuration.github_write_token is not None
        and configuration.allow_external_write
    ):
        selected_transport = GitHubMCPTransport(configuration, live_permit)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        authority = provided or Engine(configuration, ArtifactStore(configuration.artifact_root))
        if provided is None:
            authority.recover()
        app.state.engine = authority
        stopped = asyncio.Event()
        workers: dict[UUID, asyncio.Task[None]] = {}
        effect: asyncio.Task[Any] | None = None

        async def execute_run(run: dict[str, Any]) -> None:
            runner = Runner(authority)
            try:
                if run["execution_mode"] == "live":
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
            nonlocal effect
            while not stopped.is_set():
                for run_id, worker in list(workers.items()):
                    if worker.done():
                        worker.result()
                        del workers[run_id]
                if effect is not None and effect.done():
                    effect.result()
                    effect = None
                for run in authority.list_runs():
                    if run["status"] != "queued" or run["run_id"] in workers or len(workers) >= 2:
                        continue
                    workers[run["run_id"]] = asyncio.create_task(execute_run(run))
                if selected_transport is not None and effect is None:
                    effect = asyncio.create_task(
                        asyncio.to_thread(Dispatcher(authority, selected_transport).tick)
                    )
                try:
                    await asyncio.wait_for(stopped.wait(), timeout=0.25)
                except TimeoutError:
                    pass

        task = asyncio.create_task(supervise()) if run_background else None
        app.state.supervisor = task
        try:
            yield
        finally:
            stopped.set()
            if task is not None:
                await task
                for run_id in workers:
                    run = authority.get_run(run_id)
                    if run["graph_version"] == "r2-mailbox-v1" and run["status"] == "running":
                        authority.pause(run_id)
                await asyncio.gather(*workers.values())
                if effect is not None:
                    await effect
            if provided is None:
                authority.close()

    app = FastAPI(title="MGAR V3.1 R2", lifespan=lifespan)
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

    def authenticated(request: Request) -> None:
        supplied = request.headers.get("X-MGAR-Token", "")
        if not hmac.compare_digest(
            supplied.encode(), configuration.local_token.get_secret_value().encode()
        ):
            raise DomainError("AUTH_REQUIRED", "Local authentication is required", 401)
        origin = request.headers.get("origin")
        if origin and origin not in {"http://127.0.0.1:18080", "http://localhost:18080"}:
            raise DomainError("ORIGIN_DENIED", "Origin is not allowed", 403)

    @app.get("/api/v3/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "alive", "release_status": "ENGINEERING_INCOMPLETE"}

    @app.get("/api/v3/readyz", dependencies=[Depends(authenticated)])
    def readyz(request: Request, service: Annotated[Engine, Depends(authority)]) -> dict[str, str]:
        supervisor = request.app.state.supervisor
        if supervisor is not None and supervisor.done():
            raise DomainError("SUPERVISOR_UNAVAILABLE", "Execution supervisor is unavailable", 503)
        with service.transaction() as conn:
            migrations = conn.execute("SELECT count(*) AS n FROM app.schema_migrations").fetchone()
            if migrations is None or migrations["n"] < 6:
                raise DomainError("MIGRATIONS_PENDING", "Database migrations are incomplete", 503)
        return {"database": "ready", "release_status": "ENGINEERING_INCOMPLETE"}

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
        return service.ingest(event, live_permit if body.execution_mode == "live" else None)

    @app.get("/api/v3/runs", dependencies=[Depends(authenticated)])
    def list_runs(service: Annotated[Engine, Depends(authority)]) -> Any:
        return jsonable_encoder(service.list_runs())

    @app.get("/api/v3/runs/{run_id}", dependencies=[Depends(authenticated)])
    def get_run(run_id: UUID, service: Annotated[Engine, Depends(authority)]) -> Any:
        return jsonable_encoder(service.get_run(run_id))

    @app.post(
        "/api/v3/runs/{run_id}/messages", status_code=202, dependencies=[Depends(authenticated)]
    )
    def message(
        run_id: UUID, body: MessageInput, service: Annotated[Engine, Depends(authority)]
    ) -> Any:
        return service.follow_up(run_id, body.client_request_id, body.body, live_permit)

    @app.get("/api/v3/runs/{run_id}/report", dependencies=[Depends(authenticated)])
    def report(run_id: UUID, service: Annotated[Engine, Depends(authority)]) -> Response:
        run = service.get_run(run_id)
        if not run["report_hash"]:
            raise DomainError("REPORT_NOT_READY", "Report is not available", 404)
        return Response(service.artifacts.read(run["report_hash"]), media_type="text/markdown")

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
        return jsonable_encoder(
            service.approve(body.action_id, body.payload_hash, configuration.local_actor)
        )

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
        if selected_transport is None:
            raise DomainError(
                "TRANSPORT_NOT_CONFIGURED", "Reconciliation transport is unavailable", 503
            )
        if not any(item["action_id"] == action_id for item in service.actions(run_id)):
            raise DomainError("ACTION_RUN_MISMATCH", "Action does not belong to this Run", 404)
        return {"reconciled": Dispatcher(service, selected_transport).reconcile(action_id)}

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

    return app
