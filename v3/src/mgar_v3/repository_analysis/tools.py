"""Dynamic, read-only GitHub MCP gateway scoped to one repository and commit."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from mgar_v3.domain import DomainError
from mgar_v3.mcp.factory import github_client
from mgar_v3.repository_analysis.domain import (
    ReadFileCall,
    SearchCodeCall,
    ToolCall,
    evidence_uri,
    validate_repo_path,
)
from mgar_v3.settings import Settings

ANALYSIS_TOOLS = (
    "get_commit,get_file_contents,search_code,list_branches,search_repositories,"
    "issue_read,list_issues,pull_request_read,list_pull_requests"
)
_ALLOWED = {
    "get_commit",
    "get_file_contents",
    "search_code",
    "list_branches",
    "search_repositories",
    "issue_read",
    "list_issues",
    "pull_request_read",
    "list_pull_requests",
}


@dataclass(frozen=True)
class ToolOutcome:
    tool: str
    text: str
    reference: str
    content: bytes | None = None
    source_version: str | None = None
    evidence_kind: str | None = None
    truncated: bool = False


def _error_code(texts: list[str], default: str) -> tuple[str, int]:
    joined = " ".join(texts).lower()
    if "rate limit" in joined or "429" in joined or "secondary rate" in joined:
        return "GITHUB_RATE_LIMITED", 429
    if (
        "not found" in joined
        or "404" in joined
        or "does not exist" in joined
        or "does not point to a file" in joined
    ):
        return "GITHUB_NOT_FOUND", 404
    if "forbidden" in joined or "403" in joined or "permission" in joined:
        return "GITHUB_FORBIDDEN", 403
    return default, 502


class RepositoryAnalysisTools:
    """The model proposes a tool name and bounded arguments; this class owns scope."""

    def __init__(
        self,
        settings: Settings,
        repository: str,
        commit: str,
        *,
        max_bytes: int = 262144,
    ):
        if len(commit) != 40 or any(character not in "0123456789abcdef" for character in commit):
            raise DomainError("SOURCE_REF_REQUIRED", "An immutable commit is required", 400)
        self.settings = settings
        self.repository = repository.lower()
        self.commit = commit
        self.max_bytes = max_bytes

    def _client(self):
        return github_client(self.settings, self.repository, tool_group="analysis")

    async def _call(
        self, name: str, arguments: dict[str, Any]
    ) -> tuple[list[str], Any, list[Any]]:
        if name not in _ALLOWED:
            raise DomainError("MCP_TOOL_DENIED", "Tool is outside the read-only allowlist", 403)
        client = self._client()
        async with client.session() as session:
            listed = await session.list_tools()
            available = {tool.name for tool in listed.tools}
            if name not in available:
                raise DomainError(
                    "MCP_TOOL_UNAVAILABLE", "Required read-only tool is unavailable", 503
                )
            result = await session.call_tool(name, arguments=arguments)
        texts = [block.text for block in result.content if block.type == "text"]
        if result.isError:
            joined = " ".join(texts).lower()
            if "matching files: []" in joined:
                code, status = "GITHUB_NOT_FOUND", 404
            else:
                code, status = _error_code(texts, "MCP_TOOL_FAILED")
            detail = " ".join(texts)[:200]
            raise DomainError(
                code, "Read-only GitHub tool rejected the request: " + detail, status
            )
        if sum(len(text.encode()) for text in texts) > self.max_bytes:
            raise DomainError("MCP_RESULT_TOO_LARGE", "Tool response exceeds its byte budget", 413)
        resources = [block.resource for block in result.content if block.type == "resource"]
        return texts, result.structuredContent, resources

    @staticmethod
    def _payload(texts: list[str], structured: Any) -> Any:
        if structured is not None:
            return structured
        if not texts:
            return None
        try:
            return json.loads("\n".join(texts))
        except ValueError:
            return None

    async def resolve_commit(self, requested_ref: str | None) -> str:
        metadata = await self.metadata()
        default_branch = str(metadata.get("default_branch") or "")
        branch = requested_ref or default_branch
        if branch:
            matches = await self._branches(branch)
            if matches:
                sha = matches[0].get("commit", {}).get("sha") if isinstance(matches[0], dict) else None
                if isinstance(sha, str) and re.fullmatch(r"[a-f0-9]{40}", sha):
                    return sha
            try:
                payload = await self._get_commit(branch)
                sha = payload.get("sha") if isinstance(payload, dict) else None
                if isinstance(sha, str) and re.fullmatch(r"[a-f0-9]{40}", sha):
                    return sha
            except DomainError as exc:
                if exc.code not in {"GITHUB_NOT_FOUND", "MCP_TOOL_UNAVAILABLE"}:
                    raise
        for candidate in ("main", "master"):
            try:
                payload = await self._get_commit(candidate)
            except DomainError:
                continue
            sha = payload.get("sha") if isinstance(payload, dict) else None
            if isinstance(sha, str) and re.fullmatch(r"[a-f0-9]{40}", sha):
                return sha
        raise DomainError("REF_NOT_FOUND", "Requested branch, tag or commit was not found", 404)

    async def metadata(self) -> dict[str, Any]:
        texts, structured, _ = await self._call(
            "search_repositories",
            {"query": f"{self.repository} in:full_name", "perPage": 5},
        )
        payload = self._payload(texts, structured) or {}
        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            raise DomainError("MCP_RESPONSE_UNSUPPORTED", "Repository metadata is unsupported", 502)
        for item in items:
            if not isinstance(item, dict):
                continue
            if str(item.get("full_name", "")).lower() != self.repository:
                continue
            if item.get("private"):
                raise DomainError(
                    "REPOSITORY_NOT_PUBLIC", "Private repositories are outside the read scope", 403
                )
            return item
        raise DomainError("REPOSITORY_NOT_FOUND", "Repository was not found or not public", 404)

    async def _branches(self, branch: str) -> list[dict[str, Any]]:
        owner, repository = self.repository.split("/", 1)
        texts, structured, _ = await self._call(
            "list_branches", {"owner": owner, "repo": repository, "page": 1}
        )
        payload = self._payload(texts, structured)
        items = payload if isinstance(payload, list) else (
            payload.get("items") if isinstance(payload, dict) else []
        )
        return [
            item
            for item in items
            if isinstance(item, dict) and str(item.get("name")) == branch
            and isinstance(item.get("commit"), dict)
        ]

    async def _get_commit(self, ref: str) -> dict[str, Any]:
        owner, repository = self.repository.split("/", 1)
        texts, structured, _ = await self._call(
            "get_commit", {"owner": owner, "repo": repository, "sha": ref}
        )
        payload = self._payload(texts, structured)
        return payload if isinstance(payload, dict) else {}

    async def execute(self, call: ToolCall) -> ToolOutcome:
        if isinstance(call, ReadFileCall):
            return await self.read_file(call.path, call.start_line, call.end_line)
        if call.tool == "list_directory":
            return await self.list_directory(call.path)
        if isinstance(call, SearchCodeCall):
            return await self.search_code(call.query, call.limit)
        if call.tool == "repository_metadata":
            return await self.metadata_outcome()
        if call.tool == "read_issue":
            return await self.read_issue(call.number)
        if call.tool == "read_pull_request":
            return await self.read_pull_request(call.number)
        raise DomainError("MCP_TOOL_DENIED", "Tool is outside the read-only allowlist", 403)

    async def metadata_outcome(self) -> ToolOutcome:
        metadata = await self.metadata()
        safe = {
            key: metadata.get(key)
            for key in (
                "full_name",
                "description",
                "default_branch",
                "private",
                "archived",
                "language",
                "stargazers_count",
                "forks_count",
                "open_issues_count",
                "license",
                "topics",
            )
            if key in metadata
        }
        if safe.get("full_name"):
            safe["commit"] = self.commit
        text = json.dumps(safe, ensure_ascii=False, indent=2)
        return ToolOutcome(
            tool="repository_metadata",
            text=text,
            reference=f"github://{self.repository}@{self.commit}",
            truncated=False,
        )

    async def list_directory(self, path: str) -> ToolOutcome:
        path = validate_repo_path(path, allow_empty=True)
        owner, repository = self.repository.split("/", 1)
        texts, structured, _ = await self._call(
            "get_file_contents",
            {"owner": owner, "repo": repository, "path": path, "sha": self.commit},
        )
        payload = self._payload(texts, structured)
        if isinstance(payload, dict):
            raise DomainError("TOOL_PATH_IS_FILE", "Path is a file, not a directory", 400)
        if not isinstance(payload, list):
            raise DomainError("MCP_RESPONSE_UNSUPPORTED", "Directory response is unsupported", 502)
        entries = []
        for item in payload[:200]:
            if not isinstance(item, dict):
                continue
            entries.append(
                {
                    "type": item.get("type"),
                    "name": item.get("name"),
                    "path": item.get("path"),
                    "sha": item.get("sha"),
                    "size": item.get("size"),
                }
            )
        text = json.dumps(
            {"path": path or "/", "commit": self.commit, "entries": entries},
            ensure_ascii=False,
            indent=2,
        )
        return ToolOutcome(
            tool="list_directory",
            text=text,
            reference=evidence_uri(self.repository, self.commit, path or ""),
        )

    async def read_file(
        self, path: str, start_line: int | None = None, end_line: int | None = None
    ) -> ToolOutcome:
        path = validate_repo_path(path)
        owner, repository = self.repository.split("/", 1)
        texts, structured, resources = await self._call(
            "get_file_contents",
            {"owner": owner, "repo": repository, "path": path, "sha": self.commit},
        )
        payload = self._payload(texts, structured)
        if isinstance(payload, list):
            raise DomainError("TOOL_PATH_IS_DIRECTORY", "Path is a directory, not a file", 400)
        if payload is None and texts and "matching files" in texts[0]:
            if "matching files: []" in texts[0]:
                raise DomainError("GITHUB_NOT_FOUND", texts[0][:300], 404)
            raise DomainError("TOOL_PATH_NOT_EXACT", texts[0][:300], 400)
        data: bytes
        version: str
        try:
            if resources:
                if len(resources) != 1 or len(texts) != 1 or not hasattr(resources[0], "text"):
                    raise ValueError("Ambiguous embedded source")
                source = resources[0]
                uri = str(source.uri)
                match = re.match(
                    r"^repo://([^/]+/[^/]+)/sha/([a-f0-9]{40})/contents/(.+)$", uri
                )
                if match is None:
                    raise ValueError("Embedded resource URI is unsupported")
                if (
                    match.group(1).lower() != self.repository
                    or match.group(2) != self.commit
                    or match.group(3) != path
                ):
                    raise ValueError("Embedded resource scope mismatch")
                data = source.text.encode()
                digest = re.fullmatch(
                    r"successfully downloaded text file \(SHA: ([a-f0-9]{40})\)", texts[0]
                )
                if digest is None:
                    raise ValueError("Missing Git blob digest")
                version = digest.group(1)
                actual = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
                if version != actual:
                    raise ValueError("Git blob bytes mismatch")
            else:
                payload = self._payload(texts, structured)
                if not isinstance(payload, dict):
                    raise ValueError("unsupported read response")
                content = payload["content"]
                version = payload["sha"]
                if not isinstance(content, str) or not isinstance(version, str):
                    raise ValueError("unsupported read response")
                if len(version) != 40 or any(
                    character not in "0123456789abcdef" for character in version
                ):
                    raise ValueError("unsupported blob digest")
                if payload.get("encoding") == "base64":
                    data = base64.b64decode("".join(content.split()), validate=True)
                else:
                    data = content.encode()
        except (ValueError, KeyError, TypeError) as exc:
            raise DomainError(
                "MCP_RESPONSE_UNSUPPORTED", "Tool response format is unsupported"
            ) from exc
        if len(data) > self.max_bytes:
            raise DomainError("MCP_RESULT_TOO_LARGE", "Source exceeds its byte budget", 413)
        text = data.decode("utf-8", "replace")
        if start_line is not None and end_line is not None:
            lines = text.splitlines(keepends=True)
            total = max(1, len(lines))
            if start_line > total:
                raise DomainError(
                    "TOOL_RANGE_OUTSIDE_FILE",
                    f"start_line {start_line} exceeds {total} lines",
                    400,
                )
            last = min(end_line, total)
            text = (
                f"(lines {start_line}-{last} of {total}; quote only from these lines)\n"
                + "".join(lines[start_line - 1 : last])
            )
        return ToolOutcome(
            tool="read_file",
            text=text,
            reference=evidence_uri(self.repository, self.commit, path),
            content=data,
            source_version=version,
            evidence_kind="file",
        )

    async def search_code(self, query: str, limit: int) -> ToolOutcome:
        if re.search(r"repo:[^\s]+", query) or "*" in query:
            raise DomainError("SEARCH_SCOPE_DENIED", "Search must use the run repository", 403)
        owner, repository = self.repository.split("/", 1)
        texts, structured, _ = await self._call(
            "search_code",
            {
                "query": f"repo:{self.repository} {query}",
                "perPage": max(1, min(limit, 30)),
            },
        )
        payload = self._payload(texts, structured) or {}
        raw_items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(raw_items, list):
            raise DomainError("MCP_RESPONSE_UNSUPPORTED", "Code search response is unsupported", 502)
        items = []
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            repo_info = item.get("repository")
            full_name = ""
            if isinstance(repo_info, dict):
                full_name = str(repo_info.get("full_name") or repo_info.get("name") or "")
                owner_info = repo_info.get("owner")
                if isinstance(owner_info, dict):
                    full_name = f"{owner_info.get('login', '')}/{full_name}"
            if full_name and full_name.lower() != self.repository:
                continue
            matches = item.get("text_matches")
            snippets = []
            if isinstance(matches, list):
                for match in matches[:3]:
                    if isinstance(match, dict) and isinstance(match.get("fragment"), str):
                        snippets.append(match["fragment"][:1500])
            items.append(
                {
                    "path": item.get("path"),
                    "sha": item.get("sha"),
                    "snippets": snippets,
                    "url": item.get("html_url"),
                }
            )
        text = json.dumps(
            {
                "query": query,
                "repository": self.repository,
                "commit_hint": self.commit,
                "items": items[:30],
                "note": "Search snippets are pointers; read_file is required for citable evidence.",
            },
            ensure_ascii=False,
            indent=2,
        )
        if len(text.encode()) > self.max_bytes:
            text = text[: self.max_bytes // 2] + "\n…(truncated)"
        return ToolOutcome(
            tool="search_code",
            text=text,
            reference=f"github://{self.repository}@{self.commit}?search={query[:200]}",
        )

    async def read_issue(self, number: int) -> ToolOutcome:
        owner, repository = self.repository.split("/", 1)
        texts, structured, _ = await self._call(
            "issue_read",
            {
                "owner": owner,
                "repo": repository,
                "issue_number": number,
                "method": "get",
            },
        )
        payload = self._payload(texts, structured)
        if not isinstance(payload, dict):
            raise DomainError("MCP_RESPONSE_UNSUPPORTED", "Issue response is unsupported", 502)
        body = payload.get("body")
        title = payload.get("title")
        if not isinstance(title, str):
            raise DomainError("MCP_RESPONSE_UNSUPPORTED", "Issue title is unavailable", 502)
        content = f"# {title}\n\n{body if isinstance(body, str) else ''}".encode()
        if len(content) > self.max_bytes:
            raise DomainError("MCP_RESULT_TOO_LARGE", "Issue source exceeds its byte budget", 413)
        reference = f"github://{self.repository}/issues/{number}"
        return ToolOutcome(
            tool="read_issue",
            text=content.decode("utf-8", "replace"),
            reference=reference,
            content=content,
            source_version=hashlib.sha256(content).hexdigest(),
            evidence_kind="issue",
        )

    async def read_pull_request(self, number: int) -> ToolOutcome:
        owner, repository = self.repository.split("/", 1)
        texts, structured, _ = await self._call(
            "pull_request_read",
            {
                "owner": owner,
                "repo": repository,
                "pull_number": number,
                "method": "get",
            },
        )
        payload = self._payload(texts, structured)
        if not isinstance(payload, dict):
            raise DomainError("MCP_RESPONSE_UNSUPPORTED", "Pull request response is unsupported", 502)
        title = payload.get("title")
        body = payload.get("body")
        if not isinstance(title, str):
            raise DomainError("MCP_RESPONSE_UNSUPPORTED", "Pull request title is unavailable", 502)
        state = payload.get("state")
        merged = payload.get("merged")
        content = (
            f"# {title}\n\nstate={state if isinstance(state, str) else 'unknown'} "
            f"merged={merged if isinstance(merged, bool) else 'unknown'}\n\n"
            f"{body if isinstance(body, str) else ''}"
        ).encode()
        if len(content) > self.max_bytes:
            raise DomainError("MCP_RESULT_TOO_LARGE", "Pull request source exceeds its budget", 413)
        reference = f"github://{self.repository}/pull/{number}"
        return ToolOutcome(
            tool="read_pull_request",
            text=content.decode("utf-8", "replace"),
            reference=reference,
            content=content,
            source_version=hashlib.sha256(content).hexdigest(),
            evidence_kind="pull_request",
        )


TOOL_CATALOG = """Available read-only tools (no other tool or repository is reachable):
- read_file {"tool":"read_file","path":"repo/relative/path","start_line":120,"end_line":200} — exact file bytes at the fixed commit. start_line/end_line are optional 1-based inclusive line numbers; use them to page through long files because observations are truncated.
- list_directory {"tool":"list_directory","path":"optional/repo/relative/dir"} — directory entries.
- search_code {"tool":"search_code","query":"symbol or phrase","limit":20} — code search inside the fixed repository. Snippets are pointers, not evidence.
- repository_metadata {"tool":"repository_metadata"} — public repository metadata and fixed commit.
- read_issue {"tool":"read_issue","number":1} — issue title/body from the fixed repository.
- read_pull_request {"tool":"read_pull_request","number":1} — pull request title/body from the fixed repository."""
