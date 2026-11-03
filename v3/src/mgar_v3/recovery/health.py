"""Read-only dependency probes outside business transactions, recorded only by Engine."""

import asyncio
from typing import TYPE_CHECKING

from redis import Redis
from redis.exceptions import RedisError

from mgar_v3.mcp.factory import github_client

if TYPE_CHECKING:
    from mgar_v3.coordination.engine import Engine


def probe(engine: "Engine") -> dict[str, str]:
    with engine.transaction() as conn:
        conn.execute("SELECT 1")
    states = {"postgres": "ready", "redis": "unavailable", "mcp": "unconfigured"}
    try:
        with Redis.from_url(
            engine.settings.redis_url.get_secret_value(), socket_connect_timeout=2, socket_timeout=2
        ) as client:
            if client.ping():
                states["redis"] = "ready"
    except (RedisError, OSError):
        pass
    if engine.settings.github_mcp_binary is not None or engine.settings.github_read_token:
        states["mcp"] = "unavailable"

        async def inventory() -> bool:
            client = github_client(engine.settings, engine.settings.repository)
            async with client.session() as session:
                result = await session.list_tools()
            return {"issue_read", "get_file_contents"} <= {t.name for t in result.tools}

        try:
            if asyncio.run(asyncio.wait_for(inventory(), timeout=5)):
                states["mcp"] = "ready"
        except Exception:
            pass
    with engine.transaction() as conn:
        for dependency, state in states.items():
            conn.execute(
                "INSERT INTO app.dependency_health(project_id,repository,dependency,state) "
                "VALUES (%s,%s,%s,%s) ON CONFLICT(project_id,repository,dependency) DO UPDATE "
                "SET state=excluded.state,checked_at=now(),generation="
                "app.dependency_health.generation+CASE WHEN app.dependency_health.state="
                "excluded.state THEN 0 ELSE 1 END",
                (engine.settings.project_id, engine.settings.repository.lower(), dependency, state),
            )
    return states
