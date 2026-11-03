from collections.abc import Callable
from typing import Any

from fastapi import Depends, FastAPI, Request

from mgar_v3.coordination.engine import Engine
from mgar_v3.live import LivePermit
from mgar_v3.proactivity.domain import Activation, Policy, ProactiveEvent


def register(
    app: FastAPI,
    authority: Callable[[Request], Engine],
    admin: Callable[[Request], None],
    permit: LivePermit | None,
) -> None:
    @app.get("/api/v3/proactive/policies", dependencies=[Depends(admin)])
    def policies(request: Request) -> Any:
        return authority(request).proactivity.policies()

    @app.post("/api/v3/proactive/policies", dependencies=[Depends(admin)])
    def create(request: Request, body: Policy) -> Any:
        engine = authority(request)
        return engine.proactivity.create(body, engine.settings.local_actor, permit)

    @app.post("/api/v3/proactive/policies/{key}/activation", dependencies=[Depends(admin)])
    def activate(request: Request, key: str, body: Activation) -> Any:
        from mgar_v3.domain import DomainError

        if key != body.policy_hash:
            raise DomainError("POLICY_HASH_MISMATCH", "Approve the full policy hash")
        engine = authority(request)
        return engine.proactivity.activate(body, engine.settings.local_actor, permit)

    @app.post("/api/v3/proactive/events", dependencies=[Depends(admin)])
    def event(request: Request, body: ProactiveEvent) -> Any:
        return authority(request).proactivity.observe(body, permit)
