import asyncio
import json
from typing import Any

from mgar_v3.coordination.engine import Row
from mgar_v3.domain import DomainError
from mgar_v3.effects.dispatcher import DefinitiveFailure
from mgar_v3.live import LivePermit
from mgar_v3.mcp.client import ReadOnlyGitHubMCP
from mgar_v3.mcp.factory import github_client
from mgar_v3.settings import Settings


class GitHubMCPTransport:
    mode = "live"

    def __init__(
        self,
        settings: Settings,
        permit: LivePermit,
        writer: ReadOnlyGitHubMCP | None = None,
        reader: ReadOnlyGitHubMCP | None = None,
    ):
        self.permit = permit
        repository = permit.profile.repository
        self.writer = writer or github_client(settings, repository, readonly=False)
        self.writer.readonly = False
        self.reader = reader or github_client(settings, repository)

    async def _call(self, client: ReadOnlyGitHubMCP, tool: str, arguments: Row) -> Any:
        async with client.session() as session:
            tools = await session.list_tools()
            if tool not in {item.name for item in tools.tools}:
                raise DomainError("MCP_TOOL_UNAVAILABLE", "Required GitHub tool is unavailable")
            result = await session.call_tool(tool, arguments=arguments)
        if result.isError:
            raise OSError("Remote MCP result requires reconciliation")
        text = "\n".join(block.text for block in result.content if block.type == "text")
        if len(text.encode()) > 262144:
            raise OSError("Remote response exceeds its byte limit")
        values = [json.loads(block.text) for block in result.content if block.type == "text"]
        if arguments.get("method") == "get_comments":
            if len(values) == 1 and (
                isinstance(values[0], list)
                or isinstance(values[0], dict)
                and "comments" in values[0]
            ):
                return values[0]
            return values
        if len(values) != 1:
            raise OSError("Remote response shape is unsupported")
        return values[0]

    def send(self, action: Row) -> str:
        profile = self.permit.profile
        try:
            self.permit.validate(external_write=True)
        except DomainError:
            raise DefinitiveFailure("Publication was not authorized before dispatch") from None
        payload = action["payload"]
        if (
            payload["repository"].lower() != profile.repository.lower()
            or payload["issue_number"] != profile.issue_number
            or action["action_type"] != profile.action_type
        ):
            raise DefinitiveFailure("Action differs from the reviewed publication scope")
        owner, repository = profile.repository.split("/", 1)
        if profile.source_kind == "issue":
            try:
                current = asyncio.run(self.reader.read_issue(profile.issue_number))
                if current.version != profile.expected_source_hash:
                    raise ValueError("source changed")
            except Exception:
                raise DefinitiveFailure(
                    "Issue source could not be revalidated before publication"
                ) from None
        try:
            response = asyncio.run(
                self._call(
                    self.writer,
                    "add_issue_comment",
                    {
                        "owner": owner,
                        "repo": repository,
                        "issue_number": profile.issue_number,
                        "body": payload["body"],
                    },
                )
            )
            identifier = response["id"]
            if not isinstance(identifier, (str, int)) or isinstance(identifier, bool):
                raise ValueError("invalid remote identifier")
            return str(identifier)
        except Exception:
            raise OSError("GitHub result is unknown; reconciliation is required") from None

    def find(self, action: Row) -> str | None:
        profile = self.permit.profile
        owner, repository = profile.repository.split("/", 1)
        for page in range(1, 4):
            try:
                response = asyncio.run(
                    self._call(
                        self.reader,
                        "issue_read",
                        {
                            "owner": owner,
                            "repo": repository,
                            "issue_number": profile.issue_number,
                            "method": "get_comments",
                            "perPage": 100,
                            "page": page,
                        },
                    )
                )
                comments = response if isinstance(response, list) else response["comments"]
                for comment in comments:
                    if comment.get("body") == action["payload"]["body"]:
                        return str(comment["id"])
                if len(comments) < 100:
                    return None
            except Exception:
                return None
        return None
