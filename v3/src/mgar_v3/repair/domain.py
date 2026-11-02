from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import Field

from mgar_v3.domain import Model


class RepairInput(Model):
    project_id: str
    issue_number: Annotated[int, Field(strict=True, gt=0)]
    question: Annotated[str, Field(min_length=1, max_length=4000)]
    client_request_id: Annotated[str, Field(min_length=1, max_length=200)]
    execution_mode: Literal["fixture", "live"] = "fixture"


class PatchReply(Model):
    diff: Annotated[str, Field(min_length=1, max_length=32768)]
    explanation: Annotated[str, Field(max_length=2000)]


class GitEntry(Model):
    mode: Literal["100644", "100755", "120000", "160000"]
    sha: Annotated[str, Field(pattern=r"^[a-f0-9]{40}$")]


class RepairScope(Model):
    snapshot_sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    base_tree: Annotated[str, Field(pattern=r"^[a-f0-9]{40}$")]
    base_branch: Annotated[str, Field(pattern=r"^[A-Za-z0-9_/-]{1,100}$")] = "main"
    workspace_prefix: Annotated[str, Field(min_length=1, max_length=160)]
    predecessor_budget_hashes: Annotated[list[str], Field(min_length=1, max_length=4)]
    cumulative_limit_micros: Annotated[int, Field(strict=True, gt=0, le=2000000)] = 2000000
    deadline_seconds: Annotated[int, Field(strict=True, ge=60, le=600)] = 300


class Snapshot(Model):
    repository: str
    base_branch: str = "main"
    base_sha: str
    base_tree: str
    origin: Literal["fixture_git", "github_commit"]
    files: dict[str, str]
    editable_paths: list[str]
    expected_tests: list[str]
    workspace_prefix: str = Field(default="", exclude_if=lambda value: not value)
    tree_entries: dict[str, GitEntry] = Field(
        default_factory=dict, exclude_if=lambda value: not value
    )


class TestReceipt(Model):
    receipt_id: UUID
    image_id: str
    workspace_hash: str
    exit_code: int
    passed: bool
    tests: list[str]
    skipped: int
    stop_reason: str | None
    log_hash: str
    policy: dict[str, Any]


class ApprovalDecision(Model):
    payload_hash: str
    reason: Annotated[str, Field(max_length=1000)] = ""


class EditDecision(ApprovalDecision):
    diff: Annotated[str, Field(min_length=1, max_length=32768)]


def publication_requests(action: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    p = action["payload"]
    owner, repo = p["repository"].split("/", 1)
    scope = {"owner": owner, "repo": repo}
    return [
        (
            "branch",
            "create_branch",
            {**scope, "branch": p["branch"], "from_branch": p["base_branch"]},
        ),
        (
            "commit",
            "push_files",
            {
                **scope,
                "branch": p["branch"],
                "files": p["files"],
                "message": p["title"] + " [mgar-action:" + str(action["action_id"]) + "]",
            },
        ),
        (
            "pull_request",
            "create_pull_request",
            {
                **scope,
                "base": p["base_branch"],
                "head": p["branch"],
                "title": p["title"],
                "body": p["body"],
                "draft": True,
                "maintainer_can_modify": False,
            },
        ),
    ]
