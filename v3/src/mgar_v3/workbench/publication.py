"""Durable Workbench Draft PR publication through the Engine outbox (V3.5 R04).

Publication is split into three durable phases, mirroring the coordination
contract used by the live repair profile:

* approval binds the reviewed :class:`ReviewBundle` -- repository, base
  commit, tested tree, diff SHA-256 and payload hash;
* approval only enqueues a publication action in PostgreSQL.  The Workbench
  HTTP request never performs a GitHub write;
* a leased :class:`~mgar_v3.effects.dispatcher.PublicationDispatcher` claims
  the action and writes branch, commit and Draft PR one receipted step at a
  time.  A read-only credential can never become a writer, and an unknown
  remote result is reconciled through read-only lookups instead of replay.

A workbench finding never merges a pull request.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from psycopg.types.json import Jsonb

from mgar_v3.coordination.engine import Engine, Row
from mgar_v3.domain import DomainError, digest
from mgar_v3.effects.dispatcher import DefinitiveFailure
from mgar_v3.repair.pr_delivery import (
    DraftPRPublisher,
    DraftPullRequest,
    PublicationApproval,
    PublicationStep,
    ReadTransport,
    ReviewBundle,
    WriteTransport,
    verify_approval,
)
from mgar_v3.repair.workspace import blob
from mgar_v3.workbench.domain import PublicationRecord

PublicationMode = Literal["fixture", "live"]
StepName = Literal["branch", "commit", "pull_request"]


class PublicationDenied(DefinitiveFailure):
    """A publication precondition failed before any remote write was sent."""

    def __init__(self, code: str, reason: str) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason


class PublicationUncertain(OSError):
    """A remote write may have happened; only read-only reconciliation is safe."""


class PublicationWriteScope:
    """Independent live write authorization for Workbench publication.

    It is deliberately separate from the read-only GitHub credential: read
    access never implies the ability to push a branch or open a pull request.
    The scope is bound to one actor, one repository, one base branch set and
    an expiry, and can be revoked at any time before dispatch.
    """

    def __init__(
        self,
        *,
        actor: str,
        repository: str,
        reader: ReadTransport,
        writer: WriteTransport,
        expires_at: str,
        base_branches: Sequence[str] = ("main",),
        revoked: bool = False,
    ) -> None:
        self.actor = actor
        self.repository = repository.lower()
        self.reader = reader
        self.writer = writer
        self.expires_at = expires_at
        self.base_branches = tuple(base_branches)
        self.revoked = revoked

    def revoke(self) -> None:
        self.revoked = True

    def validate(self, bundle: ReviewBundle) -> None:
        if self.revoked:
            raise DomainError(
                "PUBLICATION_WRITE_REVOKED", "GitHub write authorization was revoked", 403
            )
        if datetime.fromisoformat(self.expires_at) <= datetime.now(UTC):
            raise DomainError(
                "PUBLICATION_WRITE_EXPIRED", "GitHub write authorization has expired", 403
            )
        if self.repository != bundle.repository:
            raise DomainError(
                "PUBLICATION_WRITE_REPOSITORY_MISMATCH",
                "GitHub write authorization is bound to another repository",
                403,
            )
        if bundle.base_branch not in self.base_branches:
            raise DomainError(
                "PUBLICATION_WRITE_BASE_BRANCH_DENIED",
                "GitHub write authorization excludes the reviewed base branch",
                403,
            )
        if self.writer.scope != "write":
            raise DomainError(
                "GITHUB_WRITE_SCOPE_REQUIRED",
                "Draft PR publication requires the separate GitHub write credential",
                403,
            )


def approval_from_row(row: Mapping[str, Any]) -> PublicationApproval:
    return PublicationApproval(
        approval_id=str(row["approval_id"]),
        actor=str(row["actor"]),
        repository=str(row["repository"]),
        base_sha=str(row["base_sha"]),
        tree_hash=str(row["tree_hash"]),
        payload_hash=str(row["payload_hash"]),
        diff_sha256=str(row["diff_sha256"]),
        approved_at=str(row["approved_at"]),
    )


class WorkbenchPublicationLedger:
    """Durable publication rows in PostgreSQL, owned by the Engine session."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def enqueue(
        self,
        *,
        task: Mapping[str, Any],
        finding_id: str,
        bundle: ReviewBundle,
        approval: PublicationApproval,
        mode: PublicationMode,
        status: Literal["ready", "exported"],
        reason_code: str = "",
        reason: str = "",
    ) -> dict[str, Any]:
        verify_approval(bundle, approval)
        if bundle.repository != str(task["repository"]).lower():
            raise DomainError(
                "PUBLICATION_REPOSITORY_MISMATCH",
                "The reviewed bundle belongs to another repository",
                409,
            )
        if bundle.base_sha != task["commit"]:
            raise DomainError(
                "PUBLICATION_BASE_CHANGED",
                "The reviewed base commit is not the task snapshot",
                409,
            )
        with self.engine.transaction() as conn:
            in_flight = conn.execute(
                "SELECT publication_id FROM app.workbench_publications "
                "WHERE task_id=%s AND finding_id=%s AND payload_hash<>%s "
                "AND status IN ('dispatching','outcome_unknown') LIMIT 1",
                (task["task_id"], finding_id, bundle.payload_hash),
            ).fetchone()
            if in_flight is not None:
                raise DomainError(
                    "PUBLICATION_IN_FLIGHT",
                    "An earlier publication may have reached the remote; reconcile it first",
                    409,
                )
            conn.execute(
                "UPDATE app.workbench_publications SET status='stale',"
                "reason_code='PAYLOAD_CHANGED',updated_at=now() "
                "WHERE task_id=%s AND finding_id=%s AND payload_hash<>%s "
                "AND status IN ('ready','exported','failed')",
                (task["task_id"], finding_id, bundle.payload_hash),
            )
            conn.execute(
                "INSERT INTO app.workbench_publications("
                "publication_id,task_id,finding_id,execution_mode,repository,base_branch,"
                "base_sha,tree_hash,proposal_id,branch,payload_hash,diff_sha256,approval_id,"
                "actor,approved_at,bundle,status,reason_code,reason) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT(task_id,finding_id,payload_hash) DO NOTHING",
                (
                    uuid4(),
                    task["task_id"],
                    finding_id,
                    mode,
                    bundle.repository,
                    bundle.base_branch,
                    bundle.base_sha,
                    bundle.tree_hash,
                    bundle.proposal_id,
                    bundle.branch,
                    bundle.payload_hash,
                    hashlib.sha256(bundle.diff.encode()).hexdigest(),
                    approval.approval_id,
                    approval.actor,
                    approval.approved_at,
                    Jsonb(bundle.model_dump(mode="json")),
                    status,
                    reason_code[:100],
                    reason[:1000],
                ),
            )
            row = conn.execute(
                "SELECT * FROM app.workbench_publications "
                "WHERE task_id=%s AND finding_id=%s AND payload_hash=%s",
                (task["task_id"], finding_id, bundle.payload_hash),
            ).fetchone()
        if row is None:
            raise DomainError("PUBLICATION_NOT_RECORDED", "Publication was not recorded", 500)
        return dict(row)

    def get(self, publication_id: Any) -> dict[str, Any]:
        with self.engine.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM app.workbench_publications WHERE publication_id=%s",
                (publication_id,),
            ).fetchone()
        if row is None:
            raise DomainError("NOT_FOUND", "Workbench publication was not found", 404)
        return dict(row)

    def find(
        self,
        task_id: Any,
        finding_id: str,
        payload_hash: str | None = None,
    ) -> dict[str, Any] | None:
        if payload_hash is None:
            return self.latest(task_id, finding_id)
        with self.engine.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM app.workbench_publications "
                "WHERE task_id=%s AND finding_id=%s AND payload_hash=%s",
                (task_id, finding_id, payload_hash),
            ).fetchone()
        return dict(row) if row is not None else None

    def latest(self, task_id: Any, finding_id: str) -> dict[str, Any] | None:
        with self.engine.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM app.workbench_publications "
                "WHERE task_id=%s AND finding_id=%s ORDER BY created_at DESC LIMIT 1",
                (task_id, finding_id),
            ).fetchone()
        return dict(row) if row is not None else None

    def steps(self, publication_id: Any) -> list[dict[str, Any]]:
        with self.engine.transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM app.workbench_publication_steps "
                "WHERE publication_id=%s ORDER BY ordinal",
                (publication_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def begin_step(
        self,
        publication_id: Any,
        lease_token: Any,
        ordinal: int,
        kind: StepName,
        request_hash: str,
    ) -> str:
        """Reserve a step.  A receipted step is never performed twice."""
        with self.engine.transaction() as conn:
            publication = conn.execute(
                "SELECT * FROM app.workbench_publications "
                "WHERE publication_id=%s FOR UPDATE",
                (publication_id,),
            ).fetchone()
            if publication is None:
                raise DomainError("NOT_FOUND", "Workbench publication was not found", 404)
            if (
                publication["status"] != "dispatching"
                or publication["lease_token"] != lease_token
            ):
                raise DomainError("FENCED", "Publication claim is no longer current")
            step = conn.execute(
                "SELECT * FROM app.workbench_publication_steps "
                "WHERE publication_id=%s AND ordinal=%s FOR UPDATE",
                (publication_id, ordinal),
            ).fetchone()
            if step is not None:
                if step["status"] == "succeeded":
                    return "succeeded"
                raise PublicationUncertain("step outcome was already unknown")
            conn.execute(
                "INSERT INTO app.workbench_publication_steps("
                "publication_id,ordinal,kind,status,request_hash) "
                "VALUES (%s,%s,%s,'dispatching',%s)",
                (publication_id, ordinal, kind, request_hash),
            )
        return "dispatching"

    def receipt_step(
        self,
        publication_id: Any,
        ordinal: int,
        kind: StepName,
        receipt: Mapping[str, Any],
    ) -> None:
        payload = dict(receipt)
        with self.engine.transaction() as conn:
            conn.execute(
                "INSERT INTO app.workbench_publication_steps("
                "publication_id,ordinal,kind,status,request_hash,receipt) "
                "VALUES (%s,%s,%s,'succeeded',%s,%s) "
                "ON CONFLICT(publication_id,ordinal) DO UPDATE SET "
                "status='succeeded',receipt=EXCLUDED.receipt,updated_at=now()",
                (
                    publication_id,
                    ordinal,
                    kind,
                    digest(payload),
                    Jsonb(payload),
                ),
            )

    def attach_pull_request(self, publication_id: Any, pull: DraftPullRequest) -> None:
        with self.engine.transaction() as conn:
            conn.execute(
                "UPDATE app.workbench_publications SET pull_request=%s,updated_at=now() "
                "WHERE publication_id=%s",
                (Jsonb(pull.model_dump(mode="json")), publication_id),
            )

    def rearm(self, publication_id: Any) -> None:
        """Queue an exported publication once an independent write scope exists."""
        with self.engine.transaction() as conn:
            conn.execute(
                "UPDATE app.workbench_publications SET status='ready',reason_code='',"
                "reason='',updated_at=now() WHERE publication_id=%s AND status='exported'",
                (publication_id,),
            )

    def cancel_task(
        self, task_id: Any, *, reason_code: str = "TASK_CANCELLED", reason: str = ""
    ) -> dict[str, int]:
        """Invalidate queued publications; never cancel an in-flight write."""
        with self.engine.transaction() as conn:
            cancelled = conn.execute(
                "UPDATE app.workbench_publications SET status='cancelled',"
                "reason_code=%s,reason=%s,updated_at=now() "
                "WHERE task_id=%s AND status IN ('ready','exported') RETURNING publication_id",
                (reason_code[:100], reason[:1000], task_id),
            ).fetchall()
            uncertain = conn.execute(
                "UPDATE app.workbench_publications SET status='outcome_unknown',"
                "reason_code='CANCELLED_DURING_DISPATCH',reason=%s,lease_token=NULL,"
                "lease_until=NULL,updated_at=now() "
                "WHERE task_id=%s AND status='dispatching' RETURNING publication_id",
                (reason[:1000], task_id),
            ).fetchall()
            for row in uncertain:
                conn.execute(
                    "UPDATE app.workbench_publication_steps SET status='outcome_unknown' "
                    "WHERE publication_id=%s AND status='dispatching'",
                    (row["publication_id"],),
                )
        return {"cancelled": len(cancelled), "outcome_unknown": len(uncertain)}


class WorkbenchDraftTransport:
    """Draft PR transport for the Workbench outbox.

    It performs branch -> commit -> Draft PR, receipts every remote effect in
    PostgreSQL, and re-validates the approval before the first write.  It is
    the only object allowed to call the write transport.
    """

    action_types: tuple[str, ...] = ("workbench_draft_pr",)

    def __init__(
        self,
        ledger: WorkbenchPublicationLedger,
        *,
        mode: PublicationMode,
        reader: ReadTransport,
        writer: WriteTransport,
        scope: PublicationWriteScope | None = None,
    ) -> None:
        self.ledger = ledger
        self.mode: str = mode
        self.reader = reader
        self.writer = writer
        self.scope = scope

    def send(self, action: Row) -> str:
        bundle = ReviewBundle.model_validate(action["bundle"])
        approval = approval_from_row(action)
        verify_approval(bundle, approval)
        DraftPRPublisher(self.reader, self.writer).review(bundle)
        if self.scope is not None:
            try:
                self.scope.validate(bundle)
            except DomainError as exc:
                raise PublicationDenied(exc.code, str(exc)) from exc
        elif self.mode == "live":
            raise PublicationDenied(
                "PUBLICATION_WRITE_SCOPE_REQUIRED",
                "Live publication requires an independent GitHub write authorization",
            )
        if self.writer.scope != "write":
            raise PublicationDenied(
                "GITHUB_WRITE_SCOPE_REQUIRED",
                "The configured credential is read-only; no write was attempted",
            )
        publication_id = action["publication_id"]
        lease = action["lease_token"]
        if self.reader.branch_sha(bundle.repository, bundle.base_branch) != bundle.base_sha:
            raise PublicationDenied(
                "BASE_CHANGED",
                "Base branch moved before publication; new tests and approval are required",
            )
        head = self.reader.branch_sha(bundle.repository, bundle.branch)
        commit_present = False
        if head is None:
            self._write(
                publication_id,
                lease,
                0,
                "branch",
                {
                    "repository": bundle.repository,
                    "branch": bundle.branch,
                    "from_branch": bundle.base_branch,
                },
                lambda: self.writer.create_branch(
                    bundle.repository, bundle.branch, bundle.base_branch
                ),
            )
            head = self.reader.branch_sha(bundle.repository, bundle.branch)
            if head != bundle.base_sha:
                raise PublicationUncertain("Created branch does not point at the approved base")
            self._receipt(publication_id, 0, "branch", {"already_present": False, "sha": head})
        elif head == bundle.base_sha:
            self._receipt(publication_id, 0, "branch", {"already_present": True, "sha": head})
        elif self._commit_matches(bundle, head):
            self._receipt(publication_id, 0, "branch", {"already_present": True, "sha": head})
            self._receipt(publication_id, 1, "commit", {"already_present": True, "sha": head})
            commit_present = True
        else:
            raise PublicationDenied(
                "BRANCH_CONFLICT",
                "Publication branch points at a commit that is not the approved tree",
            )
        if not commit_present:
            message = f"{bundle.title} [{bundle.proposal_id[:12]}]"
            files = {item.path: item.content for item in bundle.files}
            self._write(
                publication_id,
                lease,
                1,
                "commit",
                {
                    "branch": bundle.branch,
                    "files": sorted(files),
                    "message": message,
                },
                lambda: self.writer.push_files(
                    bundle.repository, bundle.branch, files, message
                ),
            )
            head = self.reader.branch_sha(bundle.repository, bundle.branch)
            if head is None or not self._commit_matches(bundle, head):
                raise PublicationUncertain(
                    "Published commit does not contain the approved files"
                )
            self._receipt(publication_id, 1, "commit", {"already_present": False, "sha": head})
        pull = self.reader.find_draft(bundle.repository, bundle.branch, bundle.base_branch)
        if pull is not None:
            if not self._matches(bundle, pull, head):
                raise PublicationUncertain(
                    "A pull request already exists but differs from the approved draft"
                )
            self._receipt(
                publication_id,
                2,
                "pull_request",
                {"already_present": True, "number": pull.number, "sha": head},
            )
        else:
            self._write(
                publication_id,
                lease,
                2,
                "pull_request",
                {
                    "base": bundle.base_branch,
                    "head": bundle.branch,
                    "title": bundle.title,
                },
                lambda: self.writer.create_draft_pull_request(
                    bundle.repository,
                    bundle.base_branch,
                    bundle.branch,
                    bundle.title,
                    bundle.body,
                ),
            )
            pull = self.reader.find_draft(bundle.repository, bundle.branch, bundle.base_branch)
            if pull is None or not self._matches(bundle, pull, head):
                raise PublicationUncertain("Created Draft PR could not be verified")
            self._receipt(
                publication_id,
                2,
                "pull_request",
                {"already_present": False, "number": pull.number, "sha": head},
            )
        self.ledger.attach_pull_request(publication_id, pull)
        return str(pull.number)

    def find(self, action: Row) -> str | None:
        """Read-only reconciliation after an unknown remote outcome."""
        bundle = ReviewBundle.model_validate(action["bundle"])
        approval = approval_from_row(action)
        verify_approval(bundle, approval)
        publication_id = action["publication_id"]
        head = self.reader.branch_sha(bundle.repository, bundle.branch)
        if head is None:
            return None
        if head == bundle.base_sha:
            self._receipt(publication_id, 0, "branch", {"already_present": True, "sha": head})
            return None
        if not self._commit_matches(bundle, head):
            return None
        self._receipt(publication_id, 0, "branch", {"already_present": True, "sha": head})
        self._receipt(publication_id, 1, "commit", {"already_present": True, "sha": head})
        pull = self.reader.find_draft(bundle.repository, bundle.branch, bundle.base_branch)
        if pull is None or not self._matches(bundle, pull, head):
            return None
        self._receipt(
            publication_id,
            2,
            "pull_request",
            {"already_present": True, "number": pull.number, "sha": head},
        )
        self.ledger.attach_pull_request(publication_id, pull)
        return str(pull.number)

    def _write(
        self,
        publication_id: Any,
        lease: Any,
        ordinal: int,
        kind: StepName,
        request: Mapping[str, Any],
        write: Any,
    ) -> None:
        state = self.ledger.begin_step(
            publication_id, lease, ordinal, kind, digest(dict(request))
        )
        if state == "succeeded":
            return
        write()
        try:
            self._receipt(publication_id, ordinal, kind, {"already_present": False})
        except Exception as exc:  # a lost receipt means the outcome is unknown
            raise PublicationUncertain(
                f"Step receipt could not be recorded: {type(exc).__name__}"
            ) from exc

    def _receipt(
        self, publication_id: Any, ordinal: int, kind: StepName, receipt: Mapping[str, Any]
    ) -> None:
        self.ledger.receipt_step(publication_id, ordinal, kind, receipt)

    def _commit_matches(self, bundle: ReviewBundle, sha: str) -> bool:
        blobs = self.reader.commit_blobs(bundle.repository, sha)
        if blobs is None:
            return False
        return all(blobs.get(item.path) == blob(item.content.encode()) for item in bundle.files)

    def _matches(self, bundle: ReviewBundle, pull: DraftPullRequest, head: str) -> bool:
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


def record_from_row(
    row: Mapping[str, Any], steps: Sequence[Mapping[str, Any]]
) -> PublicationRecord:
    """Map a durable publication row to the public finding publication record."""
    status = _publication_status(row)
    records = [
        PublicationStep(
            ordinal=int(step["ordinal"]),
            kind=step["kind"],
            status=(
                "already_present"
                if bool((step.get("receipt") or {}).get("already_present"))
                else "succeeded"
            ),
            detail=str(step["kind"]),
        )
        for step in steps
        if step["status"] == "succeeded" and step["kind"] in {"branch", "commit", "pull_request"}
    ]
    pull = (
        DraftPullRequest.model_validate(row["pull_request"])
        if row.get("pull_request") is not None
        else None
    )
    published = status == "published" and pull is not None
    url = None
    if published and pull is not None:
        url = (
            f"fixture://github/{pull.repository}/pull/{pull.number}"
            if row["execution_mode"] == "fixture"
            else f"https://github.com/{pull.repository}/pull/{pull.number}"
        )
    return PublicationRecord(
        status=status,
        reason=str(row["reason"])[:1000],
        steps=records,
        pull_request=pull,
        pull_request_url=url,
        remote="in_memory_fixture" if row["execution_mode"] == "fixture" else "github",
        published_at=(
            row["updated_at"].isoformat(timespec="seconds")
            if published and row.get("updated_at") is not None
            else None
        ),
    )


def _publication_status(row: Mapping[str, Any]) -> str:
    status = str(row["status"])
    code = str(row["reason_code"])
    if status == "succeeded":
        return "published"
    if status == "exported":
        return "exported"
    if status == "outcome_unknown":
        return "outcome_unknown"
    if status in {"cancelled", "stale"}:
        return "cancelled"
    if status == "dispatching":
        return "dispatching"
    if status == "failed":
        if code == "BASE_CHANGED":
            return "base_changed"
        if code.startswith("PUBLICATION_WRITE") or code == "GITHUB_WRITE_SCOPE_REQUIRED":
            return "write_permission_required"
        return "failed"
    return "not_started"


__all__ = [
    "PublicationDenied",
    "PublicationMode",
    "PublicationUncertain",
    "PublicationWriteScope",
    "WorkbenchDraftTransport",
    "WorkbenchPublicationLedger",
    "approval_from_row",
    "record_from_row",
]
