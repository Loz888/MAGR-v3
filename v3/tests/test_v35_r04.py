"""V3.5 R04 acceptance: Workbench Draft PR publication through the Engine outbox.

The tests drive a real fixture finding to ``waiting_approval``, then exercise
the durable publication path: approval binds the reviewed bundle, the request
only enqueues, a leased dispatcher performs branch -> commit -> Draft PR with
per-step receipts, and unknown remote results are reconciled read-only.

Live GitHub writes are simulated with a recording in-process transport; no
network or GitHub credential is used, so ``live_github_write_gate`` stays
NOT_RUN.
"""

from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from mgar_v3.coordination.engine import Engine
from mgar_v3.domain import DomainError, digest
from mgar_v3.effects.dispatcher import PublicationDispatcher
from mgar_v3.repair.pr_delivery import DraftPullRequest, PublicationApproval, seal_bundle
from mgar_v3.repair.workspace import blob
from mgar_v3.repository_analysis.fixture import FIXTURE_FILES
from mgar_v3.settings import Settings
from mgar_v3.workbench.publication import (
    PublicationWriteScope,
    WorkbenchDraftTransport,
    WorkbenchPublicationLedger,
)
from mgar_v3.workbench.service import TEST_FILE, TEST_SOURCE, WorkbenchService

REPO = "octo/example"
DISPATCHER_RULE = "WB-DISPATCH-SILENT-UNKNOWN"


def _client(settings: Settings, engine: Engine) -> TestClient:
    from mgar_v3.api import create_app

    return TestClient(
        create_app(settings, engine, run_background=False),
        base_url="http://127.0.0.1",
        headers={"X-MGAR-Token": settings.local_token.get_secret_value()},
    )


def _create(client: TestClient) -> dict[str, Any]:
    response = client.post(
        "/api/v3/workbench/tasks",
        json={
            "repository_url": f"https://github.com/{REPO}",
            "mode": "audit",
            "task": "检查未知请求处理路径",
            "execution_mode": "fixture",
            "auto_start": True,
        },
    )
    assert response.status_code == 202, response.text
    payload: dict[str, Any] = response.json()
    return payload


def _dispatcher_finding(payload: dict[str, Any]) -> dict[str, Any]:
    finding: dict[str, Any] = next(
        item for item in payload["findings"] if item["rule_id"] == DISPATCHER_RULE
    )
    return finding


def _waiting_approval(client: TestClient) -> tuple[UUID, str, str]:
    created = _create(client)
    task_id = UUID(created["task_id"])
    finding_id = str(_dispatcher_finding(created)["finding_id"])
    assert (
        client.post(f"/api/v3/workbench/tasks/{task_id}/findings/{finding_id}/reproduce").status_code
        == 200
    )
    repaired = client.post(f"/api/v3/workbench/tasks/{task_id}/findings/{finding_id}/repair")
    assert repaired.status_code == 200, repaired.text
    assert repaired.json()["stage"] == "waiting_approval"
    review = client.get(f"/api/v3/workbench/tasks/{task_id}/findings/{finding_id}/diff")
    assert review.status_code == 200
    return task_id, finding_id, str(review.json()["payload_hash"])


def _live_clone(engine: Engine, task_id: UUID) -> UUID:
    """Clone a fixture task as a live task at ``waiting_approval``.

    ``workbench_tasks.execution_mode`` is immutable by database guard, so the
    live publication path is exercised on an equivalent live task row with the
    same snapshot, findings and pending approval.
    """
    task_id_live = uuid4()
    with engine.transaction() as conn:
        conn.execute(
            "INSERT INTO app.workbench_tasks("
            "task_id,thread_id,request_key,request_digest,mode,repository_url,repository,"
            "resolved_commit,task,issue_number,execution_mode,coverage,observed_coverage,"
            "limits,status,stage,phase,resume_phase,revision) "
            "SELECT %s,thread_id,%s,request_digest,mode,repository_url,repository,"
            "resolved_commit,task,issue_number,'live',coverage,observed_coverage,"
            "limits,'waiting_approval','waiting_approval','waiting_approval',"
            "'waiting_approval',revision "
            "FROM app.workbench_tasks WHERE task_id=%s",
            (task_id_live, f"r04-live-{task_id_live.hex}", task_id),
        )
        conn.execute(
            "INSERT INTO app.workbench_findings(task_id,finding_id,stage,risk_score,payload) "
            "SELECT %s,finding_id,stage,risk_score,payload "
            "FROM app.workbench_findings WHERE task_id=%s",
            (task_id_live, task_id),
        )
    return task_id_live


def _expires() -> str:
    return (datetime.now(UTC) + timedelta(hours=1)).isoformat()


def _base_files() -> dict[str, str]:
    files = dict(FIXTURE_FILES)
    files["src/__init__.py"] = ""
    files[TEST_FILE] = TEST_SOURCE
    return files


class RecordingRemote:
    """In-process GitHub stand-in with observable writes and failures."""

    scope: Literal["read", "write"] = "write"

    def __init__(
        self,
        *,
        base_sha: str,
        files: dict[str, str],
        moving_base: bool = False,
        read_only: bool = False,
        fail_pr_after_write: bool = False,
    ) -> None:
        self.repository = REPO
        self.base_sha = base_sha
        self.branches: dict[str, str] = {"main": ("b" * 40) if moving_base else base_sha}
        self.commits: dict[str, dict[str, str]] = {
            base_sha: {path: blob(content.encode()) for path, content in files.items()}
        }
        self.pulls: list[DraftPullRequest] = []
        self.writes: list[str] = []
        self.fail_pr_after_write = fail_pr_after_write
        if read_only:
            self.scope = "read"

    def branch_sha(self, repository: str, branch: str) -> str | None:
        return self.branches.get(branch)

    def commit_blobs(self, repository: str, sha: str) -> dict[str, str] | None:
        return self.commits.get(sha)

    def find_draft(self, repository: str, head: str, base: str) -> DraftPullRequest | None:
        for pull in self.pulls:
            if pull.head_ref == head and pull.base_ref == base:
                return pull
        return None

    def create_branch(self, repository: str, branch: str, from_branch: str) -> None:
        self.writes.append(f"branch:{branch}")
        self.branches[branch] = self.branches[from_branch]

    def push_files(
        self, repository: str, branch: str, files: dict[str, str] | Any, message: str
    ) -> str:
        self.writes.append(f"commit:{branch}")
        sha = hashlib.sha1(f"r04:{branch}:{digest(dict(files))}:{message}".encode()).hexdigest()
        self.branches[branch] = sha
        self.commits[sha] = {path: blob(content.encode()) for path, content in files.items()}
        return sha

    def create_draft_pull_request(
        self, repository: str, base: str, head: str, title: str, body: str
    ) -> int:
        self.writes.append("pull_request")
        number = len(self.pulls) + 1
        self.pulls.append(
            DraftPullRequest(
                number=number,
                draft=True,
                title=title,
                body=body,
                head_ref=head,
                head_sha=self.branches[head],
                base_ref=base,
                base_sha=self.branches[base],
                repository=self.repository,
            )
        )
        if self.fail_pr_after_write:
            raise TimeoutError("timed out after the remote created the Draft PR")
        return number


def _scope(remote: RecordingRemote) -> PublicationWriteScope:
    return PublicationWriteScope(
        actor="owner",
        repository=REPO,
        reader=remote,
        writer=remote,
        expires_at=_expires(),
        base_branches=("main",),
    )


# -- positive: fixture approval is dispatched through the outbox ---------
def test_fixture_approval_is_dispatched_through_the_engine_outbox(
    settings: Settings, engine: Engine
) -> None:
    with _client(settings, engine) as client:
        task_id, finding_id, payload_hash = _waiting_approval(client)
        approved = client.post(
            f"/api/v3/workbench/tasks/{task_id}/findings/{finding_id}/approve",
            json={"payload_hash": payload_hash},
        )
        assert approved.status_code == 200, approved.text
        payload = approved.json()

    assert payload["stage"] == "pr_created"
    assert payload["publication"]["status"] == "published"
    assert payload["publication"]["merged"] is False
    assert [step["kind"] for step in payload["publication"]["steps"]] == [
        "branch",
        "commit",
        "pull_request",
    ]
    ledger = WorkbenchPublicationLedger(engine)
    row = ledger.find(task_id, finding_id, payload_hash)
    assert row is not None
    assert row["status"] == "succeeded"
    assert row["execution_mode"] == "fixture"
    assert row["attempts"] == 1
    assert row["external_id"] == "1"
    steps = ledger.steps(row["publication_id"])
    assert [step["kind"] for step in steps] == ["branch", "commit", "pull_request"]
    assert all(step["status"] == "succeeded" and step["receipt"] for step in steps)


# -- exported when no independent write authorization exists -------------
def test_live_approval_without_write_authorization_exports_the_full_patch(
    settings: Settings, engine: Engine
) -> None:
    with _client(settings, engine) as client:
        task_id, finding_id, payload_hash = _waiting_approval(client)
        task_id = _live_clone(engine, task_id)
        approved = client.post(
            f"/api/v3/workbench/tasks/{task_id}/findings/{finding_id}/approve",
            json={"payload_hash": payload_hash},
        )
        assert approved.status_code == 200, approved.text
        payload = approved.json()

    assert payload["stage"] == "waiting_approval"
    assert payload["publication"]["status"] == "exported"
    assert payload["publication"]["pull_request"] is None
    assert payload["fix"]["diff"]
    ledger = WorkbenchPublicationLedger(engine)
    row = ledger.find(task_id, finding_id, payload_hash)
    assert row is not None
    assert row["status"] == "exported"
    assert row["execution_mode"] == "live"
    assert row["attempts"] == 0
    assert row["reason_code"] == "WRITE_PERMISSION_REQUIRED"
    task = WorkbenchService(engine, settings).store.task(task_id)
    assert task["status"] == "waiting_approval"
    assert WorkbenchService(engine, settings).view(task_id)["next_action"] == "approve"


# -- base advanced before dispatch: no write ----------------------------
def test_base_branch_move_is_refused_before_any_write(
    settings: Settings, engine: Engine
) -> None:
    with _client(settings, engine) as client:
        task_id, finding_id, payload_hash = _waiting_approval(client)
        task_id = _live_clone(engine, task_id)
    service = WorkbenchService(engine, settings)
    task = service.store.task(task_id)
    remote = RecordingRemote(base_sha=task["commit"], files=_base_files(), moving_base=True)
    service = WorkbenchService(engine, settings, publication_write_scope=_scope(remote))
    approved = service.approve(task_id, finding_id, payload_hash=payload_hash, actor="owner")
    assert approved["publication"]["status"] == "not_started"
    published = service.publish(task_id, finding_id)
    assert remote.writes == []
    assert published["stage"] == "waiting_approval"
    assert published["publication"]["status"] == "base_changed"
    row = service.publications.find(task_id, finding_id, payload_hash)
    assert row is not None and row["status"] == "failed"
    assert row["reason_code"] == "BASE_CHANGED"


# -- duplicate approval: one row, one publication ------------------------
def test_duplicate_approval_is_idempotent_and_publishes_once(
    settings: Settings, engine: Engine
) -> None:
    with _client(settings, engine) as client:
        task_id, finding_id, payload_hash = _waiting_approval(client)
        task_id = _live_clone(engine, task_id)
    service = WorkbenchService(engine, settings)
    task = service.store.task(task_id)
    remote = RecordingRemote(base_sha=task["commit"], files=_base_files())
    service = WorkbenchService(engine, settings, publication_write_scope=_scope(remote))
    first = service.approve(task_id, finding_id, payload_hash=payload_hash, actor="owner")
    repeated = service.approve(task_id, finding_id, payload_hash=payload_hash, actor="owner")
    assert first["publication"]["status"] == "not_started"
    assert repeated["publication"]["status"] == "not_started"
    task_row = service.store.task(task_id)
    assert task_row["status"] == "publishing"
    with engine.transaction() as conn:
        rows = conn.execute(
            "SELECT publication_id FROM app.workbench_publications "
            "WHERE task_id=%s AND finding_id=%s",
            (task_id, finding_id),
        ).fetchall()
    assert len(rows) == 1
    # Two concurrent dispatchers must publish the same approval exactly once.
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: service.publish(task_id, finding_id), range(2)))
    assert [write.split(":")[0] for write in remote.writes] == [
        "branch",
        "commit",
        "pull_request",
    ]
    assert len(remote.pulls) == 1
    published = service.store.finding(task_id, finding_id)
    assert published.publication is not None
    assert published.publication.status == "published"


# -- payload changed: the old publication is invalidated ----------------
def test_payload_change_invalidates_a_queued_publication(
    settings: Settings, engine: Engine
) -> None:
    with _client(settings, engine) as client:
        task_id, finding_id, payload_hash = _waiting_approval(client)
        task_id = _live_clone(engine, task_id)
    service = WorkbenchService(engine, settings)
    task = service.store.task(task_id)
    finding = service.store.finding(task_id, finding_id)
    assert finding.fix is not None
    bundle = finding.fix.bundle
    remote = RecordingRemote(base_sha=task["commit"], files=_base_files())
    approval = PublicationApproval.grant(
        bundle, actor="owner", approved_at="2026-11-10T00:00:00+00:00"
    )
    first = service.publications.enqueue(
        task=task,
        finding_id=finding_id,
        bundle=bundle,
        approval=approval,
        mode="live",
        status="ready",
    )
    changed = seal_bundle(
        repository=bundle.repository,
        base_branch=bundle.base_branch,
        base_sha=bundle.base_sha,
        tree_hash=bundle.tree_hash,
        proposal_id=bundle.proposal_id,
        branch=bundle.branch,
        title=bundle.title,
        body=bundle.body + "\n\nAdditional reviewer note.",
        diff=bundle.diff,
        files=bundle.files,
    )
    changed_approval = PublicationApproval.grant(
        changed, actor="owner", approved_at="2026-11-10T00:00:01+00:00"
    )
    second = service.publications.enqueue(
        task=task,
        finding_id=finding_id,
        bundle=changed,
        approval=changed_approval,
        mode="live",
        status="ready",
    )
    assert first["publication_id"] != second["publication_id"]
    assert service.publications.get(first["publication_id"])["status"] == "stale"
    transport = WorkbenchDraftTransport(
        service.publications, mode="live", reader=remote, writer=remote, scope=_scope(remote)
    )
    dispatcher = PublicationDispatcher(engine, transport)
    assert dispatcher.tick() == second["publication_id"]
    # Only the current approved payload may reach the remote; the stale one never does.
    assert len(remote.pulls) == 1
    assert service.publications.get(second["publication_id"])["status"] == "succeeded"
    assert service.publications.get(first["publication_id"])["status"] == "stale"


# -- write timeout with a successful remote: reconcile, never replay -----
def test_write_timeout_after_remote_success_is_reconciled_without_replay(
    settings: Settings, engine: Engine
) -> None:
    with _client(settings, engine) as client:
        task_id, finding_id, payload_hash = _waiting_approval(client)
        task_id = _live_clone(engine, task_id)
    service = WorkbenchService(engine, settings)
    task = service.store.task(task_id)
    remote = RecordingRemote(
        base_sha=task["commit"], files=_base_files(), fail_pr_after_write=True
    )
    service = WorkbenchService(engine, settings, publication_write_scope=_scope(remote))
    service.approve(task_id, finding_id, payload_hash=payload_hash, actor="owner")
    unknown = service.publish(task_id, finding_id)
    assert unknown["publication"]["status"] == "outcome_unknown"
    row = service.publications.find(task_id, finding_id, payload_hash)
    assert row is not None and row["status"] == "outcome_unknown"
    assert len(remote.pulls) == 1
    before = list(remote.writes)

    reconciled = service.reconcile_publication(task_id, finding_id)
    assert reconciled["stage"] == "pr_created"
    assert reconciled["publication"]["status"] == "published"
    assert remote.writes == before
    assert len(remote.pulls) == 1
    row = service.publications.find(task_id, finding_id, payload_hash)
    assert row is not None and row["status"] == "succeeded"
    assert row["reason_code"] == "RECONCILED"
    assert [step["kind"] for step in service.publications.steps(row["publication_id"])] == [
        "branch",
        "commit",
        "pull_request",
    ]


# -- read-only credential never becomes a writer ------------------------
def test_read_only_credential_never_writes(settings: Settings, engine: Engine) -> None:
    with _client(settings, engine) as client:
        task_id, finding_id, payload_hash = _waiting_approval(client)
        task_id = _live_clone(engine, task_id)
    service = WorkbenchService(engine, settings)
    task = service.store.task(task_id)
    remote = RecordingRemote(base_sha=task["commit"], files=_base_files(), read_only=True)
    service = WorkbenchService(engine, settings, publication_write_scope=_scope(remote))
    exported = service.approve(task_id, finding_id, payload_hash=payload_hash, actor="owner")
    assert exported["publication"]["status"] == "exported"
    with_publication = service.publish(task_id, finding_id)
    assert remote.writes == []
    assert with_publication["stage"] == "waiting_approval"
    assert with_publication["publication"]["status"] == "write_permission_required"


# -- revoked write authorization is refused before dispatch -------------
def test_revoked_write_scope_is_refused_before_dispatch(
    settings: Settings, engine: Engine
) -> None:
    with _client(settings, engine) as client:
        task_id, finding_id, payload_hash = _waiting_approval(client)
        task_id = _live_clone(engine, task_id)
    service = WorkbenchService(engine, settings)
    task = service.store.task(task_id)
    remote = RecordingRemote(base_sha=task["commit"], files=_base_files())
    scope = _scope(remote)
    service = WorkbenchService(engine, settings, publication_write_scope=scope)
    queued = service.approve(task_id, finding_id, payload_hash=payload_hash, actor="owner")
    assert queued["publication"]["status"] == "not_started"
    assert service.store.task(task_id)["status"] == "publishing"
    scope.revoke()
    refused = service.publish(task_id, finding_id)
    assert remote.writes == []
    assert refused["stage"] == "waiting_approval"
    assert refused["publication"]["status"] == "write_permission_required"
    row = service.publications.find(task_id, finding_id, payload_hash)
    assert row is not None
    assert row["status"] == "failed"
    assert row["reason_code"] == "PUBLICATION_WRITE_REVOKED"


# -- restart recovery: in-flight publication is never replayed ----------
def test_recovery_marks_in_flight_publication_unknown_without_retry(
    settings: Settings, engine: Engine
) -> None:
    with _client(settings, engine) as client:
        task_id, finding_id, payload_hash = _waiting_approval(client)
        task_id = _live_clone(engine, task_id)
    service = WorkbenchService(engine, settings)
    task = service.store.task(task_id)
    finding = service.store.finding(task_id, finding_id)
    assert finding.fix is not None
    approval = PublicationApproval.grant(
        finding.fix.bundle, actor="owner", approved_at="2026-11-10T00:00:00+00:00"
    )
    row = service.publications.enqueue(
        task=task,
        finding_id=finding_id,
        bundle=finding.fix.bundle,
        approval=approval,
        mode="live",
        status="ready",
    )
    claimed = engine.claim_publication("live")
    assert claimed is not None and claimed["publication_id"] == row["publication_id"]
    recovered = engine.recover()
    assert recovered["unknown_publications"] == 1
    assert service.publications.get(row["publication_id"])["status"] == "outcome_unknown"
    remote = RecordingRemote(base_sha=task["commit"], files=_base_files())
    transport = WorkbenchDraftTransport(
        service.publications, mode="live", reader=remote, writer=remote, scope=_scope(remote)
    )
    assert PublicationDispatcher(engine, transport).tick() is None
    assert remote.writes == []


# -- payload binding: a tampered bundle is rejected before enqueue ------
def test_approval_must_bind_the_exact_reviewed_bundle(
    settings: Settings, engine: Engine
) -> None:
    with _client(settings, engine) as client:
        task_id, finding_id, _ = _waiting_approval(client)
        task_id = _live_clone(engine, task_id)
    service = WorkbenchService(engine, settings)
    task = service.store.task(task_id)
    finding = service.store.finding(task_id, finding_id)
    assert finding.fix is not None
    bundle = finding.fix.bundle
    stale = PublicationApproval.grant(
        bundle, actor="owner", approved_at="2026-11-10T00:00:00+00:00"
    )
    tampered = bundle.model_copy(update={"diff": bundle.diff + "\n# tampered\n"})
    try:
        service.publications.enqueue(
            task=task,
            finding_id=finding_id,
            bundle=tampered,
            approval=stale,
            mode="live",
            status="ready",
        )
    except DomainError as exc:
        assert exc.code == "APPROVAL_STALE"
    else:  # pragma: no cover - a mutated bundle must never be accepted
        raise AssertionError("A tampered bundle was accepted for publication")
