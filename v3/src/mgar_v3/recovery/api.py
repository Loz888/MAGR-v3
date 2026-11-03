import hmac
from collections.abc import Callable
from typing import Any
from uuid import UUID

from fastapi import Depends, FastAPI, Request

from mgar_v3.coordination.engine import Engine
from mgar_v3.domain import DomainError
from mgar_v3.recovery.domain import Decision, RecoveryInput, Snapshot
from mgar_v3.settings import Settings


def register(
    app: FastAPI,
    settings: Settings,
    authority: Callable[[Request], Engine],
    admin: Callable[[Request], None],
) -> None:
    def observer(request: Request) -> None:
        token = settings.watchdog_token
        supplied = request.headers.get("X-MGAR-Watchdog-Token", "")
        if token is None or not hmac.compare_digest(
            supplied.encode(), token.get_secret_value().encode()
        ):
            raise DomainError(
                "WATCHDOG_AUTH_REQUIRED", "Limited watchdog authentication required", 401
            )

    @app.get("/api/v3/watchdog/snapshot", dependencies=[Depends(observer)])
    def snapshot(request: Request) -> Snapshot:
        return authority(request).recovery.snapshot()

    @app.get("/api/v3/watchdog/health", dependencies=[Depends(observer)])
    def health(request: Request) -> dict[str, str]:
        from mgar_v3.recovery.health import probe

        return probe(authority(request))

    @app.post("/api/v3/watchdog/recovery-requests", dependencies=[Depends(observer)])
    def submit(request: Request, body: RecoveryInput) -> dict[str, Any]:
        row = authority(request).recovery.submit(body)
        return {k: row[k] for k in ("request_id", "status", "created")}

    @app.get("/api/v3/recovery-requests", dependencies=[Depends(admin)])
    def requests(request: Request) -> list[dict[str, Any]]:
        return authority(request).recovery.list()

    @app.post("/api/v3/recovery-requests/{request_id}/decision", dependencies=[Depends(admin)])
    def decide(request: Request, request_id: UUID, body: Decision) -> dict[str, Any]:
        return authority(request).recovery.decide(request_id, body, settings.local_actor)
