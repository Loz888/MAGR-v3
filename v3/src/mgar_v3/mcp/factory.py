import hashlib
import json
import os

from mgar_v3.domain import DomainError
from mgar_v3.mcp.client import ReadOnlyGitHubMCP
from mgar_v3.settings import ROOT, Settings


def github_client(
    settings: Settings, repository: str, *, readonly: bool = True, max_bytes: int = 65536
) -> ReadOnlyGitHubMCP:
    token = settings.github_read_token if readonly else settings.github_write_token
    if settings.github_mcp_binary is None:
        client = ReadOnlyGitHubMCP(
            repository, token=token, max_bytes=max_bytes, redis_url=settings.redis_url
        )
    else:
        binary = settings.github_mcp_binary.resolve()
        lock = json.loads((ROOT / "config/github-mcp-lock.json").read_text())
        if (
            not binary.is_file()
            or not os.access(binary, os.X_OK)
            or hashlib.sha256(binary.read_bytes()).hexdigest() != lock["binary_sha256"]
        ):
            raise DomainError(
                "MCP_BINARY_UNVERIFIED", "Official MCP server is missing or changed", 503
            )
        tools = "issue_read,get_file_contents" if readonly else "add_issue_comment"
        args: tuple[str, ...] = ("stdio", "--tools=" + tools)
        if readonly:
            args += ("--read-only",)
        client = ReadOnlyGitHubMCP(
            repository,
            command=str(binary),
            args=args,
            token=token,
            max_bytes=max_bytes,
            redis_url=settings.redis_url,
        )
    client.readonly = readonly
    return client
