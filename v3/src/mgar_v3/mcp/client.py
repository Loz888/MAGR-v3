import base64
import hashlib
import json
import os
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlsplit

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from pydantic import SecretStr

from mgar_v3.domain import DomainError
from mgar_v3.rate_limit import acquire


@dataclass(frozen=True)
class SourceRead:
    content: bytes
    uri: str
    version: str


class ReadOnlyGitHubMCP:
    """Server configuration is trusted deployment input, never model-generated."""

    def __init__(
        self,
        repository: str,
        *,
        command: str | None = None,
        args: tuple[str, ...] = (),
        url: str = "https://api.githubcopilot.com/mcp/",
        token: SecretStr | None = None,
        max_bytes: int = 65536,
        redis_url: SecretStr | None = None,
    ):
        self.repository = repository.lower()
        self.command, self.args = command, args
        self.url, self.token = url, token
        self.max_bytes = max_bytes
        self.readonly = True
        self.redis_url = redis_url
        if not command and (
            urlsplit(url).hostname != "api.githubcopilot.com" or urlsplit(url).scheme != "https"
        ):
            raise DomainError("MCP_DESTINATION_DENIED", "MCP destination is not configured", 403)

    @asynccontextmanager
    async def session(self) -> AsyncIterator[ClientSession]:
        if self.command:
            if self.redis_url is not None:
                acquire(self.redis_url.get_secret_value(), "github-mcp:" + self.repository)
            elif self.token is not None:
                raise DomainError("RATE_LIMIT_UNAVAILABLE", "MCP rate limit is not configured", 503)
            # Only the explicitly configured GitHub credential reaches this trusted server.
            environment = {"PATH": os.environ.get("PATH", "")}
            for name in [
                "HTTP_PROXY",
                "HTTPS_PROXY",
                "NO_PROXY",
                "http_proxy",
                "https_proxy",
                "no_proxy",
                "SSL_CERT_FILE",
                "SSL_CERT_DIR",
            ]:
                if name in os.environ:
                    environment[name] = os.environ[name]
            if self.token is not None:
                environment["GITHUB_PERSONAL_ACCESS_TOKEN"] = self.token.get_secret_value()
            params = StdioServerParameters(
                command=self.command, args=list(self.args), env=environment
            )
            async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
                await session.initialize()
                yield session
        else:
            if self.redis_url is None:
                raise DomainError("RATE_LIMIT_UNAVAILABLE", "MCP rate limit is not configured", 503)
            acquire(self.redis_url.get_secret_value(), "github-mcp:" + self.repository)
            if self.token is None:
                raise DomainError(
                    "MCP_CREDENTIAL_REQUIRED", "GitHub read credential is missing", 403
                )
            headers = {"Authorization": "Bearer " + self.token.get_secret_value()}
            if self.readonly:
                headers["X-MCP-Readonly"] = "true"
            async with httpx.AsyncClient(headers=headers, timeout=30) as client:
                async with streamable_http_client(self.url, http_client=client) as (read, write, _):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        yield session

    async def read_file(self, path: str, ref: str) -> SourceRead:
        parsed = PurePosixPath(path)
        if parsed.is_absolute() or ".." in parsed.parts or "\\" in path or not path:
            raise DomainError(
                "SOURCE_PATH_DENIED", "Source path is outside the configured scope", 403
            )
        if len(ref) != 40 or any(c not in "0123456789abcdef" for c in ref):
            raise DomainError(
                "SOURCE_REF_REQUIRED", "An immutable 40-character commit is required", 400
            )
        owner, repository = self.repository.split("/", 1)
        async with self.session() as session:
            tools = await session.list_tools()
            if "get_file_contents" not in {tool.name for tool in tools.tools}:
                raise DomainError("MCP_TOOL_UNAVAILABLE", "Required read-only tool is unavailable")
            result = await session.call_tool(
                "get_file_contents",
                arguments={
                    "owner": owner,
                    "repo": repository,
                    "path": path,
                    "ref": ref,
                },
            )
        if result.isError:
            raise DomainError("MCP_TOOL_FAILED", "Read-only MCP call failed")
        texts = [block.text for block in result.content if block.type == "text"]
        if sum(len(text.encode()) for text in texts) > self.max_bytes:
            raise DomainError("MCP_RESULT_TOO_LARGE", "Tool response exceeds its byte budget")
        try:
            resources = [block.resource for block in result.content if block.type == "resource"]
            if resources:
                if len(resources) != 1 or len(texts) != 1 or not hasattr(resources[0], "text"):
                    raise ValueError("Ambiguous embedded source")
                source = resources[0]
                uri = str(source.uri)
                prefix, rest = uri.split("/sha/", 1)
                if (
                    prefix.lower() != "repo://" + self.repository
                    or rest != ref + "/contents/" + path
                ):
                    raise ValueError("Embedded resource scope mismatch")
                data = source.text.encode()
                match = re.fullmatch(
                    r"successfully downloaded text file \(SHA: ([a-f0-9]{40})\)", texts[0]
                )
                if match is None:
                    raise ValueError("Missing Git blob digest")
                version = match.group(1)
                actual = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
                if version != actual:
                    raise ValueError("Git blob bytes mismatch")
            else:
                payload: dict[str, Any] = json.loads("\n".join(texts))
                content = payload["content"]
                version = payload["sha"]
                if not isinstance(content, str) or not isinstance(version, str):
                    raise ValueError("unsupported read response")
                if payload.get("encoding") == "base64":
                    data = base64.b64decode("".join(content.split()), validate=True)
                else:
                    data = content.encode()
        except (ValueError, KeyError, TypeError) as exc:
            raise DomainError(
                "MCP_RESPONSE_UNSUPPORTED", "Tool response format is unsupported"
            ) from exc
        if len(data) > self.max_bytes:
            raise DomainError("MCP_RESULT_TOO_LARGE", "Source exceeds its byte budget")
        return SourceRead(data, f"github://{self.repository}/{path}?ref={ref}", version)

    async def read_issue(self, issue_number: int) -> SourceRead:
        owner, repository = self.repository.split("/", 1)
        async with self.session() as session:
            tools = await session.list_tools()
            if "issue_read" not in {tool.name for tool in tools.tools}:
                raise DomainError("MCP_TOOL_UNAVAILABLE", "Required Issue read tool is unavailable")
            result = await session.call_tool(
                "issue_read",
                arguments={
                    "owner": owner,
                    "repo": repository,
                    "issue_number": issue_number,
                    "method": "get",
                },
            )
        if result.isError:
            raise DomainError("MCP_TOOL_FAILED", "Issue read failed")
        text = "\n".join(block.text for block in result.content if block.type == "text")
        if len(text.encode()) > self.max_bytes:
            raise DomainError("MCP_RESULT_TOO_LARGE", "Issue response exceeds its byte limit")
        try:
            payload = json.loads(text)
            if not isinstance(payload["title"], str) or not isinstance(payload.get("body"), str):
                raise ValueError("unsupported issue response")
            content = f"# {payload['title']}\n\n{payload['body']}".encode()
        except (ValueError, KeyError, TypeError) as exc:
            raise DomainError(
                "MCP_RESPONSE_UNSUPPORTED", "Issue response format is unsupported"
            ) from exc
        if len(content) > self.max_bytes:
            raise DomainError("MCP_RESULT_TOO_LARGE", "Issue source exceeds its byte limit")
        return SourceRead(
            content,
            f"github://{self.repository}/issues/{issue_number}",
            hashlib.sha256(content).hexdigest(),
        )
