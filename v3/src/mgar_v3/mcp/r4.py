"""Real HTTP MCP endpoints; task identity comes only from Engine-issued tokens."""

import asyncio
from collections.abc import Callable
from typing import Any

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.fastmcp import Context, FastMCP

from mgar_v3.coordination.engine import Engine
from mgar_v3.domain import DomainError


def server(role: str, authority: Callable[[], Engine]) -> FastMCP:
    app = FastMCP("MGAR " + role, stateless_http=True, json_response=True, streamable_http_path="/")

    def invoke(ctx: Context[Any, Any, Any], tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        request = ctx.request_context.request
        header = request.headers.get("authorization", "") if request is not None else ""
        if not header.startswith("Bearer "):
            raise DomainError("TOOL_AUTH_REQUIRED", "Engine task capability is required", 401)
        return authority().tools.call(header[7:], tool, arguments, role)

    if role == "knowledge":

        @app.tool()
        def search_knowledge(
            query: str, snapshot_id: str, ctx: Context[Any, Any, Any], top_k: int = 6
        ) -> dict[str, Any]:
            return invoke(
                ctx,
                "search_knowledge",
                {"query": query, "snapshot_id": snapshot_id, "top_k": top_k},
            )

        @app.tool()
        def read_knowledge(
            chunk_id: str, snapshot_id: str, ctx: Context[Any, Any, Any]
        ) -> dict[str, Any]:
            return invoke(ctx, "read_knowledge", {"chunk_id": chunk_id, "snapshot_id": snapshot_id})

        @app.tool()
        def recall_memory(ctx: Context[Any, Any, Any]) -> dict[str, Any]:
            return invoke(ctx, "recall_memory", {})
    elif role == "data":

        @app.tool()
        def describe_tables(ctx: Context[Any, Any, Any]) -> dict[str, Any]:
            return invoke(ctx, "describe_tables", {})

        @app.tool()
        def query_readonly(
            sql: str, parameters: list[Any], ctx: Context[Any, Any, Any]
        ) -> dict[str, Any]:
            return invoke(ctx, "query_readonly", {"sql": sql, "parameters": parameters})

    return app


class TaskTools:
    def __init__(self, engine: Engine, token: str, role: str):
        self.engine, self.token, self.role = engine, token, role

    def call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        base = self.engine.settings.r4_tool_base_url
        if base and self.role in {"knowledge", "data"}:

            async def request() -> dict[str, Any]:
                async with httpx.AsyncClient(
                    headers={"Authorization": "Bearer " + self.token}, trust_env=False
                ) as http:
                    return await exchange(http)

            async def exchange(http: httpx.AsyncClient) -> dict[str, Any]:
                async with streamable_http_client(
                    base.rstrip("/") + "/mcp/" + self.role + "/", http_client=http
                ) as (read, write, _):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        result = await session.call_tool(tool, arguments)
                        if result.isError or result.structuredContent is None:
                            raise DomainError("MCP_TOOL_FAILED", "Readonly MCP operation failed")
                        return dict(result.structuredContent)

            try:
                return asyncio.run(request())
            except (ExceptionGroup, httpx.HTTPError) as exc:
                raise DomainError(
                    "MCP_TRANSPORT_FAILED", "Readonly MCP transport did not complete"
                ) from exc
        return self.engine.tools.call(self.token, tool, arguments, self.role)
