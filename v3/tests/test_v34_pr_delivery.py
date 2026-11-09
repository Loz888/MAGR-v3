"""V3.4 R05 reviewed Draft PR publication tests.

R05 reviews the complete diff, binds the approval to repository, base commit,
tested tree and payload hash, refuses to write with a read-only credential,
rechecks the base branch before the first write, writes branch, commit and
Draft PR as three separately receipted steps, and reconciles an unknown remote
result through read-only lookups without replaying a write.  The happy path
and the lost-response paths run against the real stdio MCP fixture server, so
the Draft PR is a real object in the test repository.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

import mgar_v3.repair.pr_delivery as pr_delivery
from mgar_v3.domain import DomainError
from mgar_v3.mcp.client import ReadOnlyGitHubMCP
from mgar_v3.repair.pr_delivery import (
    DraftPRPublisher,
    MCPReadTransport,
    MCPWriteTransport,
    MemoryPublicationLog,
    PublicationApproval,
    PublicationFile,
    ReviewBundle,
    seal_bundle,
)
from mgar_v3.repair.workspace import blob

REPO = "example/r05-fixture"
BASE_SHA = "a" * 40
MOVED_SHA = "f" * 40
TREE = "b" * 64
PROPOSAL = "d" * 64
BRANCH = "mgar/repair-r05"
CALC_BUG = "def add(a, b):\n    return a - b\n"
CALC_FIXED = "def add(a, b):\n    return a + b\n"
DIFF = (
    "diff --git a/src/calc.py b/src/calc.py\n"
    "index 1111111..2222222 100644\n"
    "--- a/src/calc.py\n"
    "+++ b/src/calc.py\n"
    "@@ -1,2 +1,2 @@\n"
    " def add(a, b):\n"
    "-    return a - b\n"
    "+    return a + b\n"
)


def _bundle(
    *,
    diff: str = DIFF,
    tree_hash: str = TREE,
    base_sha: str = BASE_SHA,
    branch: str = BRANCH,
) -> ReviewBundle:
    return seal_bundle(
        repository=REPO,
        base_branch="main",
        base_sha=base_sha,
        tree_hash=tree_hash,
        proposal_id=PROPOSAL,
        branch=branch,
        title="Fix the add function",
        body="Verified repair proposal " + PROPOSAL,
        diff=diff,
        files=[PublicationFile(path="src/calc.py", content=CALC_FIXED)],
    )


def _approval(bundle: ReviewBundle) -> PublicationApproval:
    return PublicationApproval.grant(
        bundle, actor="maintainer", approved_at="2026-11-09T00:00:00Z"
    )


def _remote_state(path: Path, *, lose_response: str | None = None) -> Path:
    path.write_text(
        json.dumps(
            {
                "repository": REPO,
                "branches": {"main": BASE_SHA},
                "commits": {
                    BASE_SHA: {
                        "sha": BASE_SHA,
                        "parents": [],
                        "commit": {"tree": {"sha": "c" * 40}},
                        "files": [],
                        "source_files": {
                            "src/calc.py": CALC_BUG,
                            "tests/test_calc.py": "from src.calc import add\n",
                        },
                    },
                    MOVED_SHA: {
                        "sha": MOVED_SHA,
                        "parents": [{"sha": BASE_SHA}],
                        "commit": {"tree": {"sha": "e" * 40}},
                        "files": [],
                        "source_files": {"src/calc.py": CALC_FIXED},
                    },
                },
                "prs": [],
                "writes": [],
                "lose_response": lose_response,
            }
        )
    )
    return path


def _client(path: Path) -> ReadOnlyGitHubMCP:
    return ReadOnlyGitHubMCP(
        REPO,
        command=sys.executable,
        args=("-m", "mgar_v3.repair.fixture_server", str(path)),
    )


def _publisher(path: Path, memory: MemoryPublicationLog | None = None) -> DraftPRPublisher:
    return DraftPRPublisher(
        MCPReadTransport(_client(path)),
        MCPWriteTransport(_client(path), scope="write"),
        memory=memory,
    )


def _state(path: Path) -> dict:
    return json.loads(path.read_text())


class _Remote:
    """In-memory read/write transport for the fast failure paths."""

    scope = "write"

    def __init__(
        self,
        *,
        branches: dict[str, str] | None = None,
        commits: dict[str, dict[str, str]] | None = None,
    ) -> None:
        self.branches = dict(branches if branches is not None else {"main": BASE_SHA})
        self.commits = dict(commits or {})
        self.writes: list[str] = []

    def branch_sha(self, repository: str, branch: str) -> str | None:
        assert repository == REPO
        return self.branches.get(branch)

    def commit_blobs(self, repository: str, sha: str) -> dict[str, str] | None:
        assert repository == REPO
        return self.commits.get(sha)

    def find_draft(self, repository: str, head: str, base: str) -> None:
        return None

    def create_branch(self, repository: str, branch: str, from_branch: str) -> None:
        self.writes.append("branch")
        self.branches[branch] = self.branches[from_branch]

    def push_files(
        self,
        repository: str,
        branch: str,
        files: dict[str, str],
        message: str,
    ) -> str:
        self.writes.append("commit")
        sha = "1" * 40
        self.branches[branch] = sha
        self.commits[sha] = {path: blob(content.encode()) for path, content in files.items()}
        return sha

    def create_draft_pull_request(
        self, repository: str, base: str, head: str, title: str, body: str
    ) -> int:
        self.writes.append("pull_request")
        return 1


class _ReadOnlyWriter:
    scope = "read"

    def __init__(self) -> None:
        self.calls: list[str] = []

    def create_branch(self, repository: str, branch: str, from_branch: str) -> None:
        self.calls.append("branch")

    def push_files(
        self, repository: str, branch: str, files: dict[str, str], message: str
    ) -> str:
        self.calls.append("commit")
        return ""

    def create_draft_pull_request(
        self, repository: str, base: str, head: str, title: str, body: str
    ) -> int:
        self.calls.append("pull_request")
        return 0


def test_approved_repair_creates_and_reconciles_a_real_draft_pr(tmp_path: Path) -> None:
    path = _remote_state(tmp_path / "remote.json")
    memory = MemoryPublicationLog()
    publisher = _publisher(path, memory)
    bundle = _bundle()
    approval = _approval(bundle)

    review = publisher.review(bundle)
    assert review.complete is True
    assert review.files == ["src/calc.py"]
    assert review.bytes == len(CALC_FIXED.encode())
    assert review.lines_added == 1
    assert review.lines_removed == 1
    assert review.diff_sha256 == hashlib.sha256(DIFF.encode()).hexdigest()

    outcome = publisher.publish(bundle, approval)
    assert outcome.status == "published"
    assert [(step.ordinal, step.kind, step.status) for step in outcome.steps] == [
        (0, "branch", "succeeded"),
        (1, "commit", "succeeded"),
        (2, "pull_request", "succeeded"),
    ]
    assert outcome.pull_request is not None
    assert outcome.pull_request.draft is True
    assert outcome.pull_request.head_ref == BRANCH
    assert outcome.pull_request.base_ref == "main"
    assert outcome.pull_request.base_sha == BASE_SHA
    assert outcome.patch is None

    state = _state(path)
    assert state["writes"] == ["branch", "commit", "pull_request"]
    assert state["prs"][0]["draft"] is True
    assert state["prs"][0]["head"]["sha"] == outcome.pull_request.head_sha

    again = publisher.reconcile(bundle, approval)
    assert again.status == "published"
    assert [step.status for step in again.steps] == [
        "already_present",
        "already_present",
        "already_present",
    ]
    assert _state(path)["writes"] == ["branch", "commit", "pull_request"]

    assert [episode.status for episode in memory.episodes] == ["published", "published"]
    assert memory.episodes[0].pull_request == outcome.pull_request.number
    assert memory.episodes[0].draft is True
    assert memory.episodes[0].payload_hash == bundle.payload_hash
    assert not any("merge" in name.lower() for name in dir(pr_delivery))


@pytest.mark.parametrize(
    "changed",
    [
        {"diff": DIFF + "+\n"},
        {"tree_hash": "e" * 64},
        {"base_sha": "0" * 40},
    ],
)
def test_any_change_after_approval_invalidates_the_approval(changed: dict) -> None:
    bundle = _bundle()
    approval = _approval(bundle)
    reader, writer = _Remote(), _Remote()

    with pytest.raises(DomainError) as caught:
        DraftPRPublisher(reader, writer).publish(_bundle(**changed), approval)
    assert caught.value.code == "APPROVAL_STALE"
    assert writer.writes == []
    assert reader.writes == []


def test_base_branch_is_rechecked_before_the_first_write() -> None:
    reader = _Remote(branches={"main": MOVED_SHA})
    writer = _Remote(branches={"main": MOVED_SHA})
    memory = MemoryPublicationLog()
    bundle = _bundle()

    outcome = DraftPRPublisher(reader, writer, memory=memory).publish(bundle, _approval(bundle))

    assert outcome.status == "base_changed"
    assert outcome.steps == []
    assert outcome.pull_request is None
    assert writer.writes == []
    assert memory.episodes[0].status == "base_changed"
    assert "approval" in memory.episodes[0].reason


def test_read_only_credential_exports_the_patch_instead_of_writing() -> None:
    reader, writer = _Remote(), _ReadOnlyWriter()
    memory = MemoryPublicationLog()
    bundle = _bundle()

    outcome = DraftPRPublisher(reader, writer, memory=memory).publish(bundle, _approval(bundle))

    assert outcome.status == "write_permission_required"
    assert outcome.patch == bundle.diff
    assert outcome.pull_request is None
    assert outcome.steps == []
    assert writer.calls == []
    assert reader.writes == []
    assert memory.episodes[0].status == "write_permission_required"


@pytest.mark.parametrize(
    ("lost", "expected_after", "published_steps", "writes"),
    [
        ("branch", "outcome_unknown", [], ["branch"]),
        ("pull_request", "published", [0, 1], ["branch", "commit", "pull_request"]),
    ],
)
def test_lost_response_is_reconciled_without_replaying_a_write(
    tmp_path: Path,
    lost: str,
    expected_after: str,
    published_steps: list[int],
    writes: list[str],
) -> None:
    path = _remote_state(tmp_path / "remote.json", lose_response=lost)
    memory = MemoryPublicationLog()
    publisher = _publisher(path, memory)
    bundle = _bundle()
    approval = _approval(bundle)

    first = publisher.publish(bundle, approval)
    assert first.status == "outcome_unknown"
    assert [step.ordinal for step in first.steps] == published_steps
    assert first.pull_request is None
    assert _state(path)["writes"] == writes

    second = publisher.reconcile(bundle, approval)
    assert second.status == expected_after
    assert _state(path)["writes"] == writes
    assert [episode.status for episode in memory.episodes] == ["outcome_unknown", expected_after]


def test_foreign_branch_at_a_different_commit_blocks_publication() -> None:
    reader = _Remote(
        branches={"main": BASE_SHA, BRANCH: MOVED_SHA},
        commits={MOVED_SHA: {"src/calc.py": blob(CALC_BUG.encode())}},
    )
    writer = _Remote()
    bundle = _bundle()

    outcome = DraftPRPublisher(reader, writer).publish(bundle, _approval(bundle))

    assert outcome.status == "outcome_unknown"
    assert outcome.steps == []
    assert writer.writes == []


def test_review_requires_the_complete_diff_and_rejects_unsafe_paths() -> None:
    incomplete = _bundle(diff="diff --git a/src/other.py b/src/other.py\n")
    with pytest.raises(DomainError) as caught:
        DraftPRPublisher(_Remote(), _Remote()).review(incomplete)
    assert caught.value.code == "DELIVERY_DIFF_INCOMPLETE"

    for bad in ["../calc.py", "/calc.py", ".git/config", "src\\calc.py", "x/../calc.py"]:
        with pytest.raises(ValidationError):
            PublicationFile(path=bad, content=CALC_FIXED)

    with pytest.raises(ValidationError):
        ReviewBundle(
            repository=REPO,
            base_sha=BASE_SHA,
            tree_hash=TREE,
            proposal_id=PROPOSAL,
            branch=BRANCH,
            title="Fix the add function",
            body="Verified repair proposal " + PROPOSAL,
            diff=DIFF,
            files=[PublicationFile(path="src/calc.py", content=CALC_FIXED)],
            payload_hash="0" * 64,
        )
    with pytest.raises(ValidationError):
        _bundle(branch="main")


def test_write_transport_requires_an_explicit_write_scope(tmp_path: Path) -> None:
    client = _client(tmp_path / "remote.json")
    with pytest.raises(DomainError) as caught:
        MCPWriteTransport(client)
    assert caught.value.code == "GITHUB_WRITE_SCOPE_REQUIRED"

    reader = MCPReadTransport(client)
    assert not hasattr(reader, "push_files")
    assert not hasattr(reader, "create_branch")
    assert not hasattr(reader, "create_draft_pull_request")
