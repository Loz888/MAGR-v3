"""Three separately journalled MCP writes; all recovery lookups are read-only."""

import asyncio
import json
from typing import Any
from uuid import UUID

from mgar_v3.coordination.engine import Engine, Row
from mgar_v3.domain import DomainError
from mgar_v3.effects.dispatcher import DefinitiveFailure
from mgar_v3.live import LivePermit
from mgar_v3.mcp.client import ReadOnlyGitHubMCP
from mgar_v3.mcp.factory import github_client
from mgar_v3.repair.domain import publication_requests
from mgar_v3.repair.workspace import blob

READ_TOOLS = ("get_commit", "list_pull_requests", "pull_request_read")
WRITE_TOOLS = ("create_branch", "push_files", "create_pull_request")


class DraftMCPTransport:
    action_types = ("draft_pr",)
    mode = "fixture"

    def __init__(self, engine: Engine, writer: ReadOnlyGitHubMCP, reader: ReadOnlyGitHubMCP):
        # Only an explicit local stdio protocol fixture is accepted by this engineering profile.
        # R1/R4 permits and allow_external_write cannot authorize a real Draft PR.
        if not writer.command or not reader.command or writer.token or reader.token:
            raise DomainError(
                "R5_WRITE_PROFILE_REQUIRED",
                "Real Draft PR writes require a separate reviewed R5 profile",
                403,
            )
        self.engine = engine
        self.writer = writer
        self.reader = reader

    def call(self, tool: str, arguments: Row) -> Any:
        if tool not in READ_TOOLS + WRITE_TOOLS:
            raise DomainError("MCP_TOOL_DENIED", "Tool is outside the Draft PR scope")
        client = self.writer if tool in WRITE_TOOLS else self.reader

        async def invoke() -> Any:
            async with client.session() as session:
                tools = await session.list_tools()
                if tool not in {t.name for t in tools.tools}:
                    raise OSError("Required tool is missing")
                result = await session.call_tool(tool, arguments=arguments)
            values = [b.text for b in result.content if b.type == "text"]
            if result.isError or sum(len(v.encode()) for v in values) > 262144:
                raise OSError("MCP result requires reconciliation")
            if tool == "list_pull_requests":
                decoded = [json.loads(v) for v in values]
                if len(decoded) == 1 and isinstance(decoded[0], list):
                    return decoded[0]
                return decoded
            if len(values) != 1:
                raise OSError("MCP response shape is unsupported")
            return json.loads(values[0])

        return asyncio.run(asyncio.wait_for(invoke(), timeout=15))

    def scope(self, action: Row) -> Row:
        payload = action["payload"]
        if (
            action["action_type"] != "draft_pr"
            or payload["source_origin"] != "fixture_git"
            or payload["repository"] != self.engine.settings.repository.lower()
        ):
            raise DefinitiveFailure("Action is outside this controlled publication profile")
        owner, repo = payload["repository"].split("/", 1)
        return {"owner": owner, "repo": repo}

    def current(self, scope: Row, ref: str) -> Row:
        value = self.call(
            "get_commit", {**scope, "sha": ref, "perPage": 100, "page": 1, "detail": "stats"}
        )
        if not isinstance(value, dict):
            raise OSError("Unsupported commit result")
        return value

    def verify_head(self, action: Row, scope: Row) -> Row:
        p = action["payload"]
        head = self.current(scope, p["branch"])
        changed = {f["filename"]: f["sha"] for f in head.get("files", [])}
        if (
            [parent.get("sha") for parent in head.get("parents", [])] != [p["base_sha"]]
            or head.get("commit", {}).get("tree", {}).get("sha") != p["tree_sha"]
            or changed != {f["path"]: blob(f["content"].encode()) for f in p["files"]}
        ):
            raise OSError("Published commit differs from the approved tested tree")
        return head

    def send(self, action: Row) -> str:
        scope = self.scope(action)
        p = action["payload"]
        identifier = UUID(str(action["action_id"]))
        lease = UUID(str(action["lease_token"]))
        if self.current(scope, p["base_branch"]).get("sha") != p["base_sha"]:
            raise DefinitiveFailure("Base changed: new tests and approval are required")
        requests = publication_requests(action)
        for ordinal, (kind, tool, request) in enumerate(requests):
            if self.engine.repairs.completed_step(identifier, ordinal, request):
                continue
            # A target/base check and a fresh Engine fence precede every remote write.
            if self.current(scope, p["base_branch"]).get("sha") != p["base_sha"]:
                raise OSError("Base changed after a partial publication; review existing objects")
            if ordinal == 1 and self.current(scope, p["branch"]).get("sha") != p["base_sha"]:
                raise OSError("Branch no longer points at the approved base")
            if ordinal == 2:
                self.verify_head(action, scope)
            old = self.engine.repairs.publication_step(identifier, lease, ordinal, kind, request)
            if old:
                continue
            response = self.call(tool, request)
            if ordinal == 0:
                if self.current(scope, p["branch"]).get("sha") != p["base_sha"]:
                    raise OSError("Created branch has a different base")
            elif ordinal == 1:
                response = self.verify_head(action, scope)
            else:
                number = response.get("number")
                if type(number) is not int:
                    values = self.call(
                        "list_pull_requests",
                        {
                            **scope,
                            "head": scope["owner"] + ":" + p["branch"],
                            "base": p["base_branch"],
                            "state": "all",
                            "perPage": 100,
                            "page": 1,
                        },
                    )
                    if not isinstance(values, list) or len(values) != 1:
                        raise OSError("Created PR requires exact read-only reconciliation")
                    number = values[0]["number"]
                response = self.call(
                    "pull_request_read", {**scope, "method": "get", "pullNumber": number}
                )
                self.verify_pr(action, scope, response)
            self.engine.repairs.publication_receipt(identifier, lease, ordinal, response)
        found = self.find(action)
        if found is None:
            raise OSError("Draft PR receipt could not be reconciled")
        return found

    def verify_pr(self, action: Row, scope: Row, pr: Row) -> None:
        p = action["payload"]
        head = self.verify_head(action, scope)
        if (
            type(pr.get("number")) is not int
            or pr["number"] <= 0
            or pr.get("draft") is not True
            or pr.get("body") != p["body"]
            or pr.get("title") != p["title"]
            or pr.get("head", {}).get("ref") != p["branch"]
            or pr["head"].get("sha") != head["sha"]
            or pr.get("base", {}).get("ref") != p["base_branch"]
            or pr["base"].get("sha") != p["base_sha"]
            or pr.get("head", {}).get("repo", {}).get("full_name", "").lower() != p["repository"]
        ):
            raise OSError("PR differs from the approved draft, branch or commit")

    def find(self, action: Row) -> str | None:
        scope = self.scope(action)
        p = action["payload"]
        try:
            self.engine.repairs.validate_action(action, self.engine.get_run(action["run_id"]))
            values = self.call(
                "list_pull_requests",
                {
                    **scope,
                    "head": scope["owner"] + ":" + p["branch"],
                    "base": p["base_branch"],
                    "state": "all",
                    "perPage": 100,
                    "page": 1,
                },
            )
            if not isinstance(values, list) or len(values) != 1:
                return None
            pr = self.call(
                "pull_request_read", {**scope, "method": "get", "pullNumber": values[0]["number"]}
            )
            self.verify_pr(action, scope, pr)
            head = self.verify_head(action, scope)
            self.engine.repairs.reconciled(action["action_id"], [{"sha": p["base_sha"]}, head, pr])
            # Finding the exact complete object never replays a branch, commit, or PR write.
            return str(pr["number"])
        except (OSError, KeyError, ValueError, TypeError, TimeoutError):
            return None


class LiveDraftMCPTransport(DraftMCPTransport):
    mode = "live"

    def __init__(
        self,
        engine: Engine,
        permit: LivePermit,
        writer: ReadOnlyGitHubMCP | None = None,
        reader: ReadOnlyGitHubMCP | None = None,
    ):
        if permit.profile.investigation_version != "r5-repair-v1" or permit.profile.repair is None:
            raise DomainError("R5_WRITE_PROFILE_REQUIRED", "A reviewed R5 profile is required", 403)
        self.engine, self.permit = engine, permit
        self.profile_hash = permit.profile.profile_hash
        self.writer = writer or github_client(
            engine.settings, permit.profile.repository, readonly=False, tool_group="r5_write"
        )
        self.reader = reader or github_client(
            engine.settings, permit.profile.repository, tool_group="r5_read"
        )

    def scope(self, action: Row) -> Row:
        p = self.permit.profile
        scope = p.repair
        payload = action["payload"]
        assert scope is not None
        if (
            action["action_type"] != "draft_pr"
            or payload["source_origin"] != "github_commit"
            or payload["repository"] != p.repository.lower()
            or payload["issue_number"] != p.issue_number
            or payload["base_sha"] != p.source_ref
            or payload["base_tree"] != scope.base_tree
            or payload["base_branch"] != scope.base_branch
            or payload["draft"] is not True
        ):
            raise DefinitiveFailure("Draft action differs from the reviewed R5 publication scope")
        with self.engine.transaction() as conn:
            run = self.engine._run(conn, action["run_id"])
            mandate = conn.execute(
                "SELECT scope FROM app.mandates WHERE mandate_id=%s", (run["mandate_id"],)
            ).fetchone()
            if (
                run["execution_mode"] != "live"
                or not mandate
                or mandate["scope"].get("profile_hash") != p.profile_hash
            ):
                raise DefinitiveFailure("Draft publication belongs to another mandate")
        owner, repo = p.repository.split("/", 1)
        return {"owner": owner, "repo": repo}

    def current(self, scope: Row, ref: str) -> Row:
        from mgar_v3.repair.source import github_commit

        value = super().current(scope, ref)
        # Official v2.0.3 get_commit deliberately omits parent/tree/blob fields.
        # Resolve the ref with MCP, then verify those facts from the fetched Git object.
        metadata, _ = github_commit(scope["owner"] + "/" + scope["repo"], value["sha"])
        return metadata

    def find(self, action: Row) -> str | None:
        found = super().find(action)
        if found is not None:
            return found
        scope = self.scope(action)
        p = action["payload"]
        receipts: dict[int, Row] = {}
        try:
            head = self.current(scope, p["branch"])
            if head["sha"] == p["base_sha"]:
                receipts[0] = {"sha": p["base_sha"]}
            else:
                head = self.verify_head(action, scope)
                receipts = {0: {"sha": p["base_sha"]}, 1: head}
        except (OSError, DomainError, ValueError, KeyError):
            return None
        self.engine.repairs.reconciled(action["action_id"], receipts)
        return None

    def send(self, action: Row) -> str:
        try:
            self.permit.validate(external_write=True)
        except DomainError:
            raise DefinitiveFailure("R5 external publication is not authorized") from None
        try:
            return super().send(action)
        except DomainError as exc:
            raise OSError(
                "R5 publication stopped at an Engine fence; reconcile partial objects"
            ) from exc

    def call(self, tool: str, arguments: Row) -> Any:
        if tool in WRITE_TOOLS:
            try:
                self.permit.validate(external_write=True)
            except DomainError as exc:
                raise OSError("R5 authorization changed before the next remote write") from exc
        return super().call(tool, arguments)
