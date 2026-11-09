"""Generic Draft PR delivery for reviewed repairs (V3.4 R05).

R05 turns a verified repair proposal into a reviewed publication bundle:

* the complete diff and every changed file are reviewed before approval;
* an approval binds repository, base commit, tested tree and payload hash;
* the publisher uses a separate read transport and write transport, so a
  read-only credential can never perform a GitHub write;
* without write permission the full patch is exported instead of writing;
* the base commit is checked again before the first remote write;
* branch, commit and Draft PR are written and receipted one step at a time;
* an unknown remote result is reconciled through read-only lookups and never
  replayed; and
* every outcome is recorded as an Episodic Memory episode.

Only Draft PRs are created.  There is no merge operation anywhere in this
module.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Annotated, Any, Literal, Protocol, Self

from pydantic import Field, field_validator, model_validator

from mgar_v3.domain import DomainError, Model, digest
from mgar_v3.mcp.client import ReadOnlyGitHubMCP
from mgar_v3.repair.workspace import blob

DeliveryStatus = Literal[
    "published",
    "write_permission_required",
    "base_changed",
    "outcome_unknown",
]
StepKind = Literal["branch", "commit", "pull_request"]
StepStatus = Literal["succeeded", "already_present"]

_BRANCH_PATTERN = r"^[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+){0,3}$"


class PublicationFile(Model):
    path: Annotated[str, Field(min_length=1, max_length=300)]
    content: Annotated[str, Field(min_length=1, max_length=262144)]

    @field_validator("path")
    @classmethod
    def repository_relative(cls, value: str) -> str:
        if value.startswith(("/", "~")) or "\\" in value or ":" in value:
            raise ValueError("publication paths must be repository-relative")
        parts = value.split("/")
        if any(part in {"", ".", ".."} for part in parts):
            raise ValueError("publication paths must not traverse")
        if any(part.startswith(".") for part in parts):
            raise ValueError("hidden paths are not publishable")
        return value


class ReviewBundle(Model):
    """The exact object a human reviews and approves."""

    repository: Annotated[str, Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")]
    base_branch: Annotated[str, Field(pattern=_BRANCH_PATTERN, max_length=200)] = "main"
    base_sha: Annotated[str, Field(pattern=r"^[a-f0-9]{40}$")]
    tree_hash: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    proposal_id: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    branch: Annotated[str, Field(pattern=_BRANCH_PATTERN, max_length=200)]
    title: Annotated[str, Field(min_length=1, max_length=200)]
    body: Annotated[str, Field(min_length=1, max_length=60000)]
    diff: Annotated[str, Field(min_length=1, max_length=131072)]
    files: Annotated[list[PublicationFile], Field(min_length=1, max_length=16)]
    payload_hash: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]

    @field_validator("repository")
    @classmethod
    def normalize_repository(cls, value: str) -> str:
        return value.lower()

    @model_validator(mode="after")
    def reviewed_shape(self) -> Self:
        if self.branch == self.base_branch:
            raise ValueError("the publication branch must differ from the base branch")
        paths = [item.path for item in self.files]
        if len(set(paths)) != len(paths):
            raise ValueError("publication files must have distinct paths")
        if self.payload_hash != digest(_bundle_identity(self)):
            raise ValueError("payload_hash does not match the reviewed bundle")
        return self


class ReviewReceipt(Model):
    repository: str
    base_sha: str
    tree_hash: str
    proposal_id: str
    payload_hash: str
    diff_sha256: str
    files: list[str]
    bytes: Annotated[int, Field(ge=0)]
    lines_added: Annotated[int, Field(ge=0)]
    lines_removed: Annotated[int, Field(ge=0)]
    complete: Literal[True] = True


class PublicationApproval(Model):
    approval_id: str
    actor: Annotated[str, Field(min_length=1, max_length=100)]
    repository: str
    base_sha: str
    tree_hash: str
    payload_hash: str
    diff_sha256: str
    approved_at: Annotated[str, Field(min_length=1, max_length=40)]

    @classmethod
    def grant(
        cls,
        bundle: ReviewBundle,
        *,
        actor: str,
        approved_at: str,
    ) -> Self:
        diff_sha256 = hashlib.sha256(bundle.diff.encode()).hexdigest()
        return cls(
            approval_id=_approval_identity(
                actor=actor,
                repository=bundle.repository,
                base_sha=bundle.base_sha,
                tree_hash=bundle.tree_hash,
                payload_hash=bundle.payload_hash,
                diff_sha256=diff_sha256,
                approved_at=approved_at,
            ),
            actor=actor,
            repository=bundle.repository,
            base_sha=bundle.base_sha,
            tree_hash=bundle.tree_hash,
            payload_hash=bundle.payload_hash,
            diff_sha256=diff_sha256,
            approved_at=approved_at,
        )


class PublicationStep(Model):
    ordinal: Annotated[int, Field(ge=0, le=2)]
    kind: StepKind
    status: StepStatus
    detail: str = ""


class DraftPullRequest(Model):
    number: Annotated[int, Field(gt=0)]
    draft: bool
    title: str
    body: str
    head_ref: str
    head_sha: str
    base_ref: str
    base_sha: str
    repository: str


class PublicationEpisode(Model):
    schema_version: Literal["v34-publication-episode-v1"] = "v34-publication-episode-v1"
    episode_id: str
    repository: str
    proposal_id: str
    branch: str
    base_sha: str
    tree_hash: str
    payload_hash: str
    status: DeliveryStatus
    reason: Annotated[str, Field(min_length=1, max_length=1000)]
    pull_request: int | None = None
    draft: bool | None = None


class PublicationOutcome(Model):
    schema_version: Literal["v34-publication-outcome-v1"] = "v34-publication-outcome-v1"
    status: DeliveryStatus
    reason: str
    repository: str
    proposal_id: str
    branch: str
    base_sha: str
    tree_hash: str
    payload_hash: str
    approval_id: str
    review: ReviewReceipt
    steps: list[PublicationStep] = Field(default_factory=list)
    pull_request: DraftPullRequest | None = None
    patch: str | None = None


class ReadTransport(Protocol):
    def branch_sha(self, repository: str, branch: str) -> str | None: ...

    def commit_blobs(self, repository: str, sha: str) -> dict[str, str] | None: ...

    def find_draft(self, repository: str, head: str, base: str) -> DraftPullRequest | None: ...


class WriteTransport(Protocol):
    scope: Literal["read", "write"]

    def create_branch(self, repository: str, branch: str, from_branch: str) -> None: ...

    def push_files(
        self,
        repository: str,
        branch: str,
        files: Mapping[str, str],
        message: str,
    ) -> str: ...

    def create_draft_pull_request(
        self,
        repository: str,
        base: str,
        head: str,
        title: str,
        body: str,
    ) -> int: ...


class DeliveryMemorySink(Protocol):
    def record(self, episode: PublicationEpisode) -> None: ...


class MemoryPublicationLog:
    """In-process Episodic Memory sink used by tests and local runs."""

    def __init__(self) -> None:
        self.episodes: list[PublicationEpisode] = []

    def record(self, episode: PublicationEpisode) -> None:
        self.episodes.append(episode)


class _UnknownResult(Exception):
    pass


def _bundle_identity(bundle: ReviewBundle) -> dict[str, Any]:
    return {
        "repository": bundle.repository,
        "base_branch": bundle.base_branch,
        "base_sha": bundle.base_sha,
        "tree_hash": bundle.tree_hash,
        "proposal_id": bundle.proposal_id,
        "branch": bundle.branch,
        "title": bundle.title,
        "body": bundle.body,
        "diff": bundle.diff,
        "files": [
            {"path": item.path, "content": item.content}
            for item in sorted(bundle.files, key=lambda value: value.path)
        ],
    }


def seal_bundle(
    *,
    repository: str,
    base_branch: str,
    base_sha: str,
    tree_hash: str,
    proposal_id: str,
    branch: str,
    title: str,
    body: str,
    diff: str,
    files: Sequence[PublicationFile],
) -> ReviewBundle:
    spread = {
        "repository": repository.lower(),
        "base_branch": base_branch,
        "base_sha": base_sha,
        "tree_hash": tree_hash,
        "proposal_id": proposal_id,
        "branch": branch,
        "title": title,
        "body": body,
        "diff": diff,
        "files": [
            {"path": item.path, "content": item.content}
            for item in sorted(files, key=lambda value: value.path)
        ],
    }
    return ReviewBundle(payload_hash=digest(spread), **spread)


def _approval_identity(
    *,
    actor: str,
    repository: str,
    base_sha: str,
    tree_hash: str,
    payload_hash: str,
    diff_sha256: str,
    approved_at: str,
) -> str:
    return digest(
        {
            "actor": actor,
            "repository": repository,
            "base_sha": base_sha,
            "tree_hash": tree_hash,
            "payload_hash": payload_hash,
            "diff_sha256": diff_sha256,
            "approved_at": approved_at,
        }
    )


class _MCPTransport:
    def __init__(self, client: ReadOnlyGitHubMCP) -> None:
        self.client = client

    def invoke(self, tool: str, arguments: dict[str, Any]) -> list[str]:
        async def run() -> list[str]:
            async with self.client.session() as session:
                tools = await session.list_tools()
                if tool not in {item.name for item in tools.tools}:
                    raise OSError("Required MCP tool is missing")
                result = await session.call_tool(tool, arguments=arguments)
            values = [block.text for block in result.content if block.type == "text"]
            if result.isError or sum(len(value.encode()) for value in values) > 262144:
                raise OSError("MCP result requires reconciliation")
            return values

        return asyncio.run(asyncio.wait_for(run(), timeout=15))

    def call(self, tool: str, arguments: dict[str, Any]) -> Any:
        values = self.invoke(tool, arguments)
        if len(values) != 1:
            raise OSError("MCP response shape is unsupported")
        return json.loads(values[0])

    def call_list(self, tool: str, arguments: dict[str, Any]) -> list[Any]:
        decoded = [json.loads(value) for value in self.invoke(tool, arguments)]
        if len(decoded) == 1 and isinstance(decoded[0], list):
            return decoded[0]
        return decoded


class MCPReadTransport(_MCPTransport):
    """Read-only GitHub MCP access.  It has no write methods by design."""

    def branch_sha(self, repository: str, branch: str) -> str | None:
        owner, repo = repository.split("/", 1)
        try:
            value = self.call(
                "get_commit",
                {
                    "owner": owner,
                    "repo": repo,
                    "sha": branch,
                    "perPage": 100,
                    "page": 1,
                    "detail": "stats",
                },
            )
        except (OSError, KeyError, ValueError, TypeError, TimeoutError):
            return None
        sha = value.get("sha") if isinstance(value, dict) else None
        return sha if isinstance(sha, str) else None

    def commit_blobs(self, repository: str, sha: str) -> dict[str, str] | None:
        owner, repo = repository.split("/", 1)
        value = self.call(
            "get_commit",
            {
                "owner": owner,
                "repo": repo,
                "sha": sha,
                "perPage": 100,
                "page": 1,
                "detail": "stats",
            },
        )
        entries = value.get("files") if isinstance(value, dict) else None
        if not isinstance(entries, list):
            return None
        blobs: dict[str, str] = {}
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            path, digest_value = entry.get("filename"), entry.get("sha")
            if isinstance(path, str) and isinstance(digest_value, str):
                blobs[path] = digest_value
        return blobs

    def find_draft(self, repository: str, head: str, base: str) -> DraftPullRequest | None:
        owner, repo = repository.split("/", 1)
        values = self.call_list(
            "list_pull_requests",
            {
                "owner": owner,
                "repo": repo,
                "head": owner + ":" + head,
                "base": base,
                "state": "all",
                "perPage": 100,
                "page": 1,
            },
        )
        if len(values) != 1 or not isinstance(values[0], dict):
            return None
        number = values[0].get("number")
        if type(number) is not int:
            return None
        value = self.call(
            "pull_request_read",
            {"owner": owner, "repo": repo, "method": "get", "pullNumber": number},
        )
        try:
            return DraftPullRequest(
                number=value["number"],
                draft=value["draft"],
                title=value["title"],
                body=value["body"],
                head_ref=value["head"]["ref"],
                head_sha=value["head"]["sha"],
                base_ref=value["base"]["ref"],
                base_sha=value["base"]["sha"],
                repository=value["head"]["repo"]["full_name"].lower(),
            )
        except (KeyError, TypeError, ValueError, IndexError):
            return None


class MCPWriteTransport(_MCPTransport):
    """Write-only GitHub MCP access with an explicitly separate credential.

    The constructor refuses to act as a writer unless the caller passes an
    explicit ``scope='write'``; a read-only client therefore cannot become a
    publisher by accident.
    """

    scope: Literal["read", "write"] = "write"

    def __init__(self, client: ReadOnlyGitHubMCP, *, scope: Literal["read", "write"] = "read"):
        if scope != "write":
            raise DomainError(
                "GITHUB_WRITE_SCOPE_REQUIRED",
                "Draft PR publication requires the separate GitHub write credential",
                403,
            )
        super().__init__(client)

    def create_branch(self, repository: str, branch: str, from_branch: str) -> None:
        owner, repo = repository.split("/", 1)
        self.call(
            "create_branch",
            {"owner": owner, "repo": repo, "branch": branch, "from_branch": from_branch},
        )

    def push_files(
        self,
        repository: str,
        branch: str,
        files: Mapping[str, str],
        message: str,
    ) -> str:
        owner, repo = repository.split("/", 1)
        value = self.call(
            "push_files",
            {
                "owner": owner,
                "repo": repo,
                "branch": branch,
                "files": [{"path": path, "content": content} for path, content in files.items()],
                "message": message,
            },
        )
        sha = value.get("object", {}).get("sha") if isinstance(value, dict) else None
        return sha if isinstance(sha, str) else ""

    def create_draft_pull_request(
        self,
        repository: str,
        base: str,
        head: str,
        title: str,
        body: str,
    ) -> int:
        owner, repo = repository.split("/", 1)
        value = self.call(
            "create_pull_request",
            {
                "owner": owner,
                "repo": repo,
                "base": base,
                "head": head,
                "title": title,
                "body": body,
                "draft": True,
                "maintainer_can_modify": False,
            },
        )
        number = value.get("number") if isinstance(value, dict) else None
        if type(number) is not int or number <= 0:
            raise OSError("Draft PR response has no number and requires reconciliation")
        return number


class DraftPRPublisher:
    def __init__(
        self,
        reader: ReadTransport,
        writer: WriteTransport,
        *,
        memory: DeliveryMemorySink | None = None,
    ) -> None:
        self.reader = reader
        self.writer = writer
        self.memory: DeliveryMemorySink = memory or MemoryPublicationLog()

    def review(self, bundle: ReviewBundle) -> ReviewReceipt:
        missing = [item.path for item in bundle.files if f"b/{item.path}" not in bundle.diff]
        if missing:
            raise DomainError(
                "DELIVERY_DIFF_INCOMPLETE",
                "The reviewed diff does not contain every changed file: " + ", ".join(missing),
                422,
            )
        added = removed = 0
        for line in bundle.diff.splitlines():
            if line.startswith("+") and not line.startswith("+++"):
                added += 1
            elif line.startswith("-") and not line.startswith("---"):
                removed += 1
        return ReviewReceipt(
            repository=bundle.repository,
            base_sha=bundle.base_sha,
            tree_hash=bundle.tree_hash,
            proposal_id=bundle.proposal_id,
            payload_hash=bundle.payload_hash,
            diff_sha256=hashlib.sha256(bundle.diff.encode()).hexdigest(),
            files=[item.path for item in bundle.files],
            bytes=sum(len(item.content.encode()) for item in bundle.files),
            lines_added=added,
            lines_removed=removed,
        )

    def publish(self, bundle: ReviewBundle, approval: PublicationApproval) -> PublicationOutcome:
        review = self.review(bundle)
        self._authorize(bundle, approval)
        if self.writer.scope != "write":
            return self._finish(
                bundle,
                approval,
                review,
                status="write_permission_required",
                reason="The configured credential cannot write; the reviewed patch is exported",
                patch=bundle.diff,
            )
        steps: list[PublicationStep] = []
        try:
            if self.reader.branch_sha(bundle.repository, bundle.base_branch) != bundle.base_sha:
                return self._finish(
                    bundle,
                    approval,
                    review,
                    status="base_changed",
                    reason=(
                        "Base branch moved before publication; "
                        "new tests and approval are required"
                    ),
                )
            head = self.reader.branch_sha(bundle.repository, bundle.branch)
            commit_present = False
            if head is None:
                self.writer.create_branch(bundle.repository, bundle.branch, bundle.base_branch)
                head = self.reader.branch_sha(bundle.repository, bundle.branch)
                if head != bundle.base_sha:
                    raise _UnknownResult("Created branch does not point at the approved base")
                steps.append(PublicationStep(ordinal=0, kind="branch", status="succeeded"))
            elif head == bundle.base_sha:
                steps.append(PublicationStep(ordinal=0, kind="branch", status="already_present"))
            elif self._commit_matches(bundle, head):
                steps.append(PublicationStep(ordinal=0, kind="branch", status="already_present"))
                steps.append(PublicationStep(ordinal=1, kind="commit", status="already_present"))
                commit_present = True
            else:
                return self._finish(
                    bundle,
                    approval,
                    review,
                    status="outcome_unknown",
                    reason="Branch exists at a commit that does not match the approved tested tree",
                    steps=steps,
                )
            if not commit_present:
                message = f"{bundle.title} [{bundle.proposal_id[:12]}]"
                self.writer.push_files(
                    bundle.repository,
                    bundle.branch,
                    {item.path: item.content for item in bundle.files},
                    message,
                )
                head = self.reader.branch_sha(bundle.repository, bundle.branch)
                if head is None or not self._commit_matches(bundle, head):
                    raise _UnknownResult("Published commit does not match the approved files")
                steps.append(PublicationStep(ordinal=1, kind="commit", status="succeeded"))
            pull = self.reader.find_draft(bundle.repository, bundle.branch, bundle.base_branch)
            if pull is not None:
                if not self._matches(bundle, pull, head):
                    return self._finish(
                        bundle,
                        approval,
                        review,
                        status="outcome_unknown",
                        reason="A pull request already exists but differs from the approved draft",
                        steps=steps,
                    )
                steps.append(
                    PublicationStep(ordinal=2, kind="pull_request", status="already_present")
                )
            else:
                number = self.writer.create_draft_pull_request(
                    bundle.repository,
                    bundle.base_branch,
                    bundle.branch,
                    bundle.title,
                    bundle.body,
                )
                pull = self.reader.find_draft(bundle.repository, bundle.branch, bundle.base_branch)
                if pull is None or pull.number != number or not self._matches(bundle, pull, head):
                    raise _UnknownResult("Created Draft PR could not be verified")
                steps.append(
                    PublicationStep(ordinal=2, kind="pull_request", status="succeeded")
                )
            return self._finish(
                bundle,
                approval,
                review,
                status="published",
                reason="Draft PR verified against the approved tree",
                steps=steps,
                pull=pull,
            )
        except Exception as exc:
            return self._finish(
                bundle,
                approval,
                review,
                status="outcome_unknown",
                reason=f"Remote result is unknown: {type(exc).__name__}: {exc}",
                steps=steps,
            )

    def reconcile(
        self, bundle: ReviewBundle, approval: PublicationApproval
    ) -> PublicationOutcome:
        review = self.review(bundle)
        self._authorize(bundle, approval)
        steps: list[PublicationStep] = []
        try:
            head = self.reader.branch_sha(bundle.repository, bundle.branch)
            if head is None:
                return self._finish(
                    bundle,
                    approval,
                    review,
                    status="outcome_unknown",
                    reason="No published branch exists; nothing is proven complete",
                )
            if head == bundle.base_sha:
                steps.append(PublicationStep(ordinal=0, kind="branch", status="already_present"))
                return self._finish(
                    bundle,
                    approval,
                    review,
                    status="outcome_unknown",
                    reason="Only the branch exists; the commit and Draft PR were not published",
                    steps=steps,
                )
            if not self._commit_matches(bundle, head):
                return self._finish(
                    bundle,
                    approval,
                    review,
                    status="outcome_unknown",
                    reason="Branch commit does not contain the approved files",
                )
            steps.append(PublicationStep(ordinal=0, kind="branch", status="already_present"))
            steps.append(PublicationStep(ordinal=1, kind="commit", status="already_present"))
            pull = self.reader.find_draft(bundle.repository, bundle.branch, bundle.base_branch)
            if pull is None or not self._matches(bundle, pull, head):
                return self._finish(
                    bundle,
                    approval,
                    review,
                    status="outcome_unknown",
                    reason="Commit exists but the exact Draft PR is missing",
                    steps=steps,
                )
            steps.append(PublicationStep(ordinal=2, kind="pull_request", status="already_present"))
            return self._finish(
                bundle,
                approval,
                review,
                status="published",
                reason="Reconciled an existing Draft PR without replaying any write",
                steps=steps,
                pull=pull,
            )
        except Exception as exc:
            return self._finish(
                bundle,
                approval,
                review,
                status="outcome_unknown",
                reason=(
                    "Reconciliation could not prove the remote state: "
                    f"{type(exc).__name__}: {exc}"
                ),
                steps=steps,
            )

    def _authorize(self, bundle: ReviewBundle, approval: PublicationApproval) -> None:
        current_diff = hashlib.sha256(bundle.diff.encode()).hexdigest()
        expected = _approval_identity(
            actor=approval.actor,
            repository=approval.repository,
            base_sha=approval.base_sha,
            tree_hash=approval.tree_hash,
            payload_hash=approval.payload_hash,
            diff_sha256=approval.diff_sha256,
            approved_at=approval.approved_at,
        )
        if (
            approval.approval_id != expected
            or approval.repository != bundle.repository
            or approval.base_sha != bundle.base_sha
            or approval.tree_hash != bundle.tree_hash
            or approval.payload_hash != bundle.payload_hash
            or approval.diff_sha256 != current_diff
        ):
            raise DomainError(
                "APPROVAL_STALE",
                "Approval does not match the reviewed patch, tested tree or base commit",
                409,
            )

    def _commit_matches(self, bundle: ReviewBundle, sha: str) -> bool:
        blobs = self.reader.commit_blobs(bundle.repository, sha)
        if blobs is None:
            return False
        return all(
            blobs.get(item.path) == blob(item.content.encode()) for item in bundle.files
        )

    def _matches(
        self, bundle: ReviewBundle, pull: DraftPullRequest, head: str
    ) -> bool:
        return (
            pull.draft is True
            and pull.title == bundle.title
            and pull.body == bundle.body
            and pull.head_ref == bundle.branch
            and pull.head_sha == head
            and pull.base_ref == bundle.base_branch
            and pull.base_sha == bundle.base_sha
            and pull.repository == bundle.repository
        )

    def _finish(
        self,
        bundle: ReviewBundle,
        approval: PublicationApproval,
        review: ReviewReceipt,
        *,
        status: DeliveryStatus,
        reason: str,
        steps: Sequence[PublicationStep] = (),
        pull: DraftPullRequest | None = None,
        patch: str | None = None,
    ) -> PublicationOutcome:
        reason = reason[:1000]
        self._remember(bundle, status, reason, pull)
        return PublicationOutcome(
            status=status,
            reason=reason,
            repository=bundle.repository,
            proposal_id=bundle.proposal_id,
            branch=bundle.branch,
            base_sha=bundle.base_sha,
            tree_hash=bundle.tree_hash,
            payload_hash=bundle.payload_hash,
            approval_id=approval.approval_id,
            review=review,
            steps=list(steps),
            pull_request=pull,
            patch=patch,
        )

    def _remember(
        self,
        bundle: ReviewBundle,
        status: DeliveryStatus,
        reason: str,
        pull: DraftPullRequest | None,
    ) -> None:
        unique = {
            "repository": bundle.repository,
            "proposal_id": bundle.proposal_id,
            "branch": bundle.branch,
            "base_sha": bundle.base_sha,
            "tree_hash": bundle.tree_hash,
            "payload_hash": bundle.payload_hash,
            "status": status,
            "reason": reason,
            "pull_request": pull.number if pull else None,
        }
        self.memory.record(
            PublicationEpisode(
                episode_id=digest(unique),
                repository=bundle.repository,
                proposal_id=bundle.proposal_id,
                branch=bundle.branch,
                base_sha=bundle.base_sha,
                tree_hash=bundle.tree_hash,
                payload_hash=bundle.payload_hash,
                status=status,
                reason=reason,
                pull_request=pull.number if pull else None,
                draft=pull.draft if pull else None,
            )
        )
