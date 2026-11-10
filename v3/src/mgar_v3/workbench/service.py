"""Unified repository workbench service (V3.4 U01).

This module composes existing capabilities instead of widening them:

* audit delegates to the read-only V3.2 repository analysis service;
* findings use the V3.3 lifecycle and evidence vocabulary;
* patch verification runs the repository's test with a real local receipt;
* approval and Draft PR publication reuse the R05 review/approval contract.

Fixture mode uses the deterministic offline repository shipped with the
repository-analysis runtime.  Live repair and publication stay inside the R05
profile flow; the workbench never enables GitHub writes by itself.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from psycopg.errors import UniqueViolation

from mgar_v3.coordination.engine import Engine
from mgar_v3.domain import DomainError, digest
from mgar_v3.findings.model import Finding, FindingEvidence, normalize_path
from mgar_v3.repair.handoff import (
    ParentAnalysisRun,
    RepairAuthorization,
    RepairHandoff,
    RepairHandoffBuilder,
)
from mgar_v3.repair.pr_delivery import (
    DraftPRPublisher,
    MemoryPublicationLog,
    PublicationApproval,
    PublicationFile,
    seal_bundle,
)
from mgar_v3.repair.workspace import blob
from mgar_v3.repository_analysis.domain import (
    AnalysisLimits,
    CreateAnalysisInput,
    FollowUpInput,
    parse_repository_url,
)
from mgar_v3.repository_analysis.fixture import FIXTURE_FILES, fixture_commit
from mgar_v3.repository_analysis.service import RepositoryAnalysisService
from mgar_v3.scanners.domain import ScanLocation
from mgar_v3.settings import Settings
from mgar_v3.verification.verifier import STATIC_EVIDENCE_KINDS
from mgar_v3.workbench.domain import (
    STAGE_LABELS,
    STAGE_RANK,
    ApprovalRecord,
    CoverageRequest,
    CreateWorkbenchTask,
    FindingSource,
    ReproductionPlan,
    ReproductionRecord,
    StaticConfirmation,
    TaskPhase,
    TaskStatus,
    TestReceipt,
    WorkbenchFinding,
    advance_finding,
    new_finding_id,
    rank_findings,
    score_finding,
)
from mgar_v3.workbench.store import WorkbenchStore

DISPATCHER_PATH = "src/dispatcher.py"
TEST_FILE = "tests/test_dispatcher.py"
TEST_NODE = "tests/test_dispatcher.py::test_unknown_request_is_handled"
TEST_SOURCE = (
    "from src.dispatcher import dispatch\n\n"
    "def test_unknown_request_is_handled():\n"
    '    assert dispatch("unknown") == "handled:unknown"\n'
)
FIXED_DISPATCHER = (
    "def dispatch(request: str) -> str:\n"
    "    return execute(request)\n\n"
    "def execute(request: str) -> str:\n"
    "    return 'handled:' + request\n"
)
_PYTEST = re.compile(r"(\d+) (passed|failed|skipped|error)")
_HASH = re.compile(r"^[a-f0-9]{64}$")
_EVIDENCE_URI = re.compile(
    r"^github://(?P<repository>[^/@\s]+/[^/@\s]+)@(?P<commit>[a-f0-9]{40})(?:/(?P<path>.+))?$"
)
_VERIFIED_STATES = frozenset({"reproduced", "confirmed_static"})


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _safe_env() -> dict[str, str]:
    allowed = {
        "PATH",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "HOME",
        "LANG",
        "LC_ALL",
        "PYTHONIOENCODING",
        "PYTHONDONTWRITEBYTECODE",
    }
    return {key: value for key, value in os.environ.items() if key in allowed}


def _tree_hash(files: dict[str, str]) -> str:
    hashes = {
        path: hashlib.sha256(content.encode()).hexdigest()
        for path, content in sorted(files.items())
    }
    return digest(hashes)


def _unified_diff(path: str, before: str, after: str) -> str:
    body = "".join(
        difflib.unified_diff(
            before.splitlines(True),
            after.splitlines(True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            n=3,
        )
    )
    return f"diff --git a/{path} b/{path}\n{body}"


def _path_from_uri(uri: str) -> str:
    marker = "@"
    if marker not in uri:
        return ""
    tail = uri.split(marker, 1)[1]
    return tail.split("/", 1)[1] if "/" in tail else ""


def _languages(files: dict[str, str]) -> list[str]:
    mapping = {".py": "Python", ".md": "Markdown", ".js": "JavaScript", ".ts": "TypeScript"}
    return sorted(
        {
            mapping[suffix]
            for path in files
            for suffix in Path(path).suffixes
            if suffix in mapping
        }
    )


class _InMemoryRemote:
    """A write-scoped GitHub stand-in used only for fixture publication receipts."""

    scope: Literal["read", "write"] = "write"

    def __init__(self, *, repository: str, base_branch: str, base_sha: str, files: dict[str, str]):
        self.repository = repository.lower()
        self.base_branch = base_branch
        self.base_sha = base_sha
        self.branches: dict[str, str] = {base_branch: base_sha}
        self.commits: dict[str, dict[str, str]] = {
            base_sha: {path: blob(content.encode()) for path, content in files.items()}
        }
        self.pulls: list[Any] = []

    def branch_sha(self, repository: str, branch: str) -> str | None:
        return self.branches.get(branch)

    def commit_blobs(self, repository: str, sha: str) -> dict[str, str] | None:
        return self.commits.get(sha)

    def find_draft(self, repository: str, head: str, base: str) -> Any | None:
        for pull in self.pulls:
            if pull.head_ref == head and pull.base_ref == base:
                return pull
        return None

    def create_branch(self, repository: str, branch: str, from_branch: str) -> None:
        self.branches[branch] = self.branches[from_branch]

    def push_files(
        self,
        repository: str,
        branch: str,
        files: dict[str, str] | Any,
        message: str,
    ) -> str:
        sha = hashlib.sha1(
            f"u01:{branch}:{digest(dict(files))}:{message}".encode()
        ).hexdigest()
        self.branches[branch] = sha
        self.commits[sha] = {path: blob(content.encode()) for path, content in files.items()}
        return sha

    def create_draft_pull_request(
        self, repository: str, base: str, head: str, title: str, body: str
    ) -> int:
        from mgar_v3.repair.pr_delivery import DraftPullRequest

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
        return number


class WorkbenchService:
    def __init__(
        self,
        engine: Engine,
        settings: Settings,
        repo_service: RepositoryAnalysisService | None = None,
    ):
        self.engine = engine
        self.settings = settings
        self.store = WorkbenchStore(engine)
        self.repo = repo_service or RepositoryAnalysisService(engine, settings)

    # -- create ----------------------------------------------------------
    def create(self, request: CreateWorkbenchTask, *, actor: str | None = None) -> dict[str, Any]:
        self._authority_check(request)
        repository, _, _ = parse_repository_url(request.repository_url)
        identity = request.model_dump(mode="json")
        identity.pop("client_request_id", None)
        request_digest = digest(identity)
        request_key = (
            f"workbench:{request.client_request_id}"
            if request.client_request_id
            else f"workbench:{uuid4()}"
        )
        existing = self.store.task_by_request_key(request_key)
        if existing is not None:
            if existing["request_digest"] != request_digest:
                raise DomainError(
                    "REQUEST_ID_REUSED",
                    "client_request_id was reused with a different request",
                    409,
                )
            return self.view(existing["task_id"])
        limits = AnalysisLimits.model_validate(request.limits or {})
        actor = actor or self.settings.local_actor
        if request.execution_mode == "live":
            if not request.auto_start:
                raise DomainError(
                    "LIVE_AUDIT_AUTOSTART_REQUIRED",
                    "Live audit tasks must be queued when created",
                    400,
                )
            analysis_input = CreateAnalysisInput(
                repository_url=request.repository_url,
                task=self._task_text(request),
                ref=request.ref,
                commit=request.commit,
                execution_mode="live",
                client_request_id=request_key,
                limits=limits,
            )
            spec = self.repo.create(analysis_input, actor=actor)
            task = self.store.create_task(
                request,
                repository=repository.full_name,
                commit=spec.commit,
                request_key=request_key,
                request_digest=request_digest,
                task_text=self._task_text(request),
                limits=limits.model_dump(mode="json"),
            )
            task = self._transition(
                task,
                "investigating",
                status="auditing",
                reason="live audit queued behind the read-only analysis profile",
                event={
                    "stage": "audit",
                    "status": "started",
                    "summary": (
                        "Live repository audit queued behind the read-only analysis profile"
                    ),
                    "payload": {"run_id": str(spec.run_id)},
                    "kind": "run.audit",
                },
                idempotency_key=f"audit-started:{task['task_id']}",
            )
            self.store.link_run(task["task_id"], "audit", spec.run_id)
            return self.view(task["task_id"])
        commit = request.commit or fixture_commit(repository.full_name, request.ref)
        try:
            task = self.store.create_task(
                request,
                repository=repository.full_name,
                commit=commit,
                request_key=request_key,
                request_digest=request_digest,
                task_text=self._task_text(request),
                limits=limits.model_dump(mode="json"),
            )
        except UniqueViolation:
            existing = self.store.task_by_request_key(request_key)
            if existing is None:
                raise
            return self.view(existing["task_id"])
        self._event(
            task["task_id"],
            stage="task",
            status="queued",
            summary=f"{request.mode} task created for {repository.full_name}@{commit[:12]}",
            payload={"mode": request.mode, "coverage": request.coverage.model_dump(mode="json")},
        )
        if request.auto_start:
            return self.run(task["task_id"])
        return self.view(task["task_id"])

    # -- execution -------------------------------------------------------
    def run(self, task_id: UUID) -> dict[str, Any]:
        task = self._sync_live_audit(self.store.task(task_id))
        if task["status"] in {"paused", "cancelled", "completed", "waiting_approval", "publishing"}:
            raise DomainError(
                "TASK_NOT_RUNNABLE", f"Task status {task['status']} cannot be started", 409
            )
        if task["execution_mode"] == "live" and task["mode"] == "issue_repair":
            raise DomainError(
                "WORKBENCH_LIVE_REPAIR_PROFILE_REQUIRED",
                "Live issue repair and publication remain behind the R05 profile flow",
                403,
            )
        task = self.store.bump_epoch(task_id, expected_epoch=task["execution_epoch"])
        if task["mode"] == "issue_repair" and task["status"] == "queued":
            task = self._transition(
                task,
                "suspected",
                status="ready",
                reason="issue finding seeded from the requested report",
                event={
                    "stage": "task",
                    "status": "ready",
                    "summary": "Issue report accepted as a suspected finding",
                    "kind": "task.state",
                },
                idempotency_key=f"issue-seeded:{task_id}",
            )
        if task["mode"] in {"audit", "audit_and_repair"} and task["audit_run_id"] is None:
            self._start_audit(task)
            task = self.store.task(task_id)
        if task["mode"] in {"issue_repair", "audit_and_repair"}:
            self._auto_repair(task)
        return self.view(task_id)

    def _start_audit(self, task: dict[str, Any]) -> None:
        task_id = task["task_id"]
        task = self._transition(
            task,
            "investigating",
            status="auditing",
            reason=f"audit started for {task['repository']}@{task['commit'][:12]}",
            event={
                "stage": "audit",
                "status": "started",
                "summary": f"Audit started for {task['repository']}@{task['commit'][:12]}",
                "kind": "run.audit",
            },
            idempotency_key=f"audit-started:{task_id}",
        )
        limits = AnalysisLimits.model_validate(task["limits"] or {})
        analysis_input = CreateAnalysisInput(
            repository_url=task["repository_url"],
            task=task["task"],
            commit=task["commit"],
            execution_mode=task["execution_mode"],
            client_request_id=f"workbench-audit:{task_id}",
            limits=limits,
        )
        try:
            spec = self.repo.create(analysis_input, actor=self.settings.local_actor)
        except DomainError as exc:
            current = self.store.task(task_id)
            self._transition(
                current,
                "failed",
                status="failed",
                reason=str(exc),
                event={
                    "stage": "audit",
                    "status": "failed",
                    "summary": str(exc),
                    "payload": {"code": exc.code},
                    "kind": "run.audit",
                },
            )
            raise
        self.store.link_run(task_id, "audit", spec.run_id)
        if task["execution_mode"] == "fixture":
            self.repo.execute(spec.run_id)
            self._import_audit(task_id, spec.run_id)

    def _auto_repair(self, task: dict[str, Any]) -> None:
        task_id = task["task_id"]
        if task["execution_mode"] == "live":
            self._live_repair_gate(task)
            return
        findings = self.store.findings(task_id)
        if task["mode"] == "issue_repair" and not findings:
            finding = self._seed_issue_finding(task)
            self.store.upsert_findings(task_id, [finding])
            findings = [finding]
        candidate = next(
            (item for item in findings if item.reproduction_plan is not None),
            None,
        )
        if candidate is None:
            raise DomainError(
                "FINDING_NOT_REPRODUCIBLE",
                "No finding has a concrete reproduction plan",
                409,
            )
        if candidate.stage == "suspected":
            candidate = self._reproduce(task, candidate)
        if candidate.stage == "reproduced":
            self._prepare_repair(self.store.task(task_id), candidate)

    def _live_repair_gate(self, task: dict[str, Any]) -> None:
        """Live repair never invents a Dispatcher target or bypasses a handoff."""

        task_id = task["task_id"]
        findings = self.store.findings(task_id)
        candidate = next(
            (
                item
                for item in findings
                if item.verification in {"reproduced", "confirmed_static"}
            ),
            None,
        )
        if candidate is None:
            if task["status"] == "auditing":
                return
            self._wait_for_handoff(
                task,
                "No reproduced or statically confirmed finding can start a live repair",
            )
            return
        stored = self.store.handoff(task_id, candidate.finding_id)
        if stored is None:
            self._wait_for_handoff(
                task,
                "A live repair requires an authorization-bound RepairHandoff",
                payload={"finding_id": candidate.finding_id},
            )
            return
        handoff = self.handoff(
            task_id,
            candidate.finding_id,
            authorization=RepairAuthorization.model_validate(
                stored["payload"]["authorization"]
            ),
        )
        self._wait_for_handoff(
            task,
            f"Handoff {handoff['handoff_id'][:12]} is ready; repair execution is not enabled",
            payload={
                "finding_id": candidate.finding_id,
                "handoff_id": handoff["handoff_id"],
            },
        )

    def _wait_for_handoff(
        self,
        task: dict[str, Any],
        reason: str,
        *,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        current = self.store.task(task["task_id"])
        if current["phase"] == "waiting_input":
            return current
        return self._transition(
            current,
            "waiting_input",
            status="ready",
            reason=reason,
            event={
                "stage": "repair",
                "status": "waiting_input",
                "summary": reason,
                "payload": payload or {},
                "kind": "handoff.waiting",
            },
            idempotency_key=f"handoff-waiting:{task['task_id']}",
        )

    def reproduce(self, task_id: UUID, finding_id: str) -> dict[str, Any]:
        task = self.store.task(task_id)
        finding = self.store.finding(task_id, finding_id)
        updated = self._reproduce(task, finding)
        return updated.view()

    def confirm_static(
        self,
        task_id: UUID,
        finding_id: str,
        *,
        evidence: FindingEvidence,
        rationale: str = "The read-only audit evidence was statically confirmed.",
    ) -> dict[str, Any]:
        """Confirm a suspected finding with retained static evidence, never a guess."""

        task = self.store.task(task_id)
        finding = self.store.finding(task_id, finding_id)
        if finding.verification == "reproduced":
            return finding.view()
        if evidence.kind not in STATIC_EVIDENCE_KINDS:
            raise DomainError(
                "STATIC_EVIDENCE_REQUIRED",
                "Static confirmation needs scanner, static-analysis or investigation evidence",
                409,
            )
        source = finding.source
        if not source.commit or source.commit != task["commit"]:
            raise DomainError(
                "HANDOFF_VERSION_MISMATCH",
                "Finding evidence is not bound to the task commit",
                409,
            )
        if (
            evidence.evidence_id != source.evidence_id
            or evidence.source_uri != source.uri
            or evidence.content_hash != source.content_hash
        ):
            raise DomainError(
                "FINDING_EVIDENCE_MISMATCH",
                "Static evidence does not match the finding's bound source",
                409,
            )
        location = evidence.location
        if (
            location is None
            or location.path != finding.path
            or (location.line or finding.start_line) != finding.start_line
            or (location.end_line or finding.end_line) != finding.end_line
        ):
            raise DomainError(
                "FINDING_EVIDENCE_MISMATCH",
                "Static evidence line range does not match the finding",
                409,
            )
        if finding.static_confirmation is not None:
            return finding.view()
        confirmation = StaticConfirmation(
            evidence=evidence,
            rationale=rationale.strip(),
            confirmed_at=_now(),
        )
        updated = advance_finding(finding, static_confirmation=confirmation)
        self._persist_finding(task_id, updated)
        self._event(
            task_id,
            stage="finding",
            status="confirmed_static",
            summary=f"Finding {finding.finding_id[:12]} statically confirmed",
            payload={
                "finding_id": finding.finding_id,
                "evidence_id": evidence.evidence_id,
                "evidence_kind": evidence.kind,
            },
        )
        return updated.view()

    def handoff(
        self,
        task_id: UUID,
        finding_id: str,
        *,
        authorization: RepairAuthorization,
    ) -> dict[str, Any]:
        """Mint or reuse the one content-addressed handoff bound to a finding."""

        task = self.store.task(task_id)
        finding = self.store.finding(task_id, finding_id)
        stored = self.store.handoff(task_id, finding_id)
        if stored is not None:
            return self._reuse_handoff(task, finding, stored)
        handoff = self._build_handoff(task, finding, authorization)
        payload = handoff.model_dump(mode="json")
        content = json.dumps(
            handoff.content(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
        content_hash = self.store.put_artifact(content)
        if content_hash != handoff.handoff_id:
            raise DomainError(
                "HANDOFF_ARTIFACT_MISMATCH",
                "Serialized handoff bytes do not match the computed identity",
                409,
            )
        stored = self.store.save_handoff(
            task_id,
            finding_id,
            handoff_id=handoff.handoff_id,
            content_hash=content_hash,
            payload=payload,
        )
        self._event(
            task_id,
            stage="handoff",
            status="created",
            summary=f"RepairHandoff {handoff.handoff_id[:12]} bound to finding",
            payload={
                "finding_id": finding_id,
                "handoff_id": handoff.handoff_id,
                "content_hash": content_hash,
                "allowed_paths": list(handoff.allowed_paths),
                "parent_run_id": str(handoff.parent_run_id),
            },
        )
        return self._handoff_view(handoff, stored, reused=False)

    def stored_handoff(self, task_id: UUID, finding_id: str) -> dict[str, Any] | None:
        stored = self.store.handoff(task_id, finding_id)
        return None if stored is None else stored

    def _reproduce(self, task: dict[str, Any], finding: WorkbenchFinding) -> WorkbenchFinding:
        task_id = task["task_id"]
        if task["execution_mode"] != "fixture":
            raise DomainError(
                "REPRODUCTION_FIXTURE_ONLY",
                "Live reproduction is not enabled by the workbench; use the R05 profile flow",
                403,
            )
        if finding.reproduction_plan is None:
            raise DomainError(
                "REPRODUCTION_PLAN_REQUIRED",
                "This finding has no concrete reproduction plan",
                409,
            )
        if finding.stage != "suspected":
            return finding
        task = self._transition(
            task,
            "suspected",
            status="reproducing",
            reason=f"reproduction started for {finding.finding_id[:12]}",
            event={
                "stage": "reproduce",
                "status": "started",
                "summary": (
                    f"Reproducing {finding.finding_id[:12]} with "
                    f"{finding.reproduction_plan.test_node}"
                ),
                "payload": {"finding_id": finding.finding_id},
                "kind": "test.reproduction",
            },
            idempotency_key=f"reproduce-started:{task_id}:{finding.finding_id}",
        )
        receipt, _ = self._fixture_tests(task, patched=False)
        if receipt.passed:
            record = ReproductionRecord(
                status="failed",
                record=(
                    "The reproduction test passed on the unpatched tree, "
                    "so no defect was proven."
                ),
                command=receipt.command,
                receipt=receipt,
            )
            self._persist_finding(task_id, advance_finding(finding, reproduction=record))
            current = self.store.task(task_id)
            self._transition(
                current,
                "suspected",
                status="ready",
                reason="reproduction not observed on the unpatched tree",
                event={
                    "stage": "reproduce",
                    "status": "not_observed",
                    "summary": "The reproduction test passed on the unpatched tree",
                    "payload": {"finding_id": finding.finding_id, "receipt_id": receipt.receipt_id},
                    "kind": "test.reproduction",
                },
            )
            raise DomainError(
                "REPRODUCTION_NOT_OBSERVED",
                "The reproduction test passed on the unpatched tree",
                409,
            )
        record = ReproductionRecord(
            status="reproduced",
            record=(
                "The reproduction test failed on the unpatched tree. Output excerpt: "
                + receipt.output_excerpt[-800:]
            ),
            command=receipt.command,
            receipt=receipt,
        )
        updated_record = advance_finding(finding, reproduction=record)
        self._persist_finding(task_id, updated_record)
        current = self.store.task(task_id)
        self._transition(
            current,
            "reproduced",
            status="ready",
            reason=f"finding {finding.finding_id[:12]} reproduced by a failing test",
            event={
                "stage": "reproduce",
                "status": "reproduced",
                "summary": (
                    f"Finding {finding.finding_id[:12]} reproduced by a failing test receipt"
                ),
                "payload": {
                    "finding_id": finding.finding_id,
                    "receipt_id": receipt.receipt_id,
                    "log_sha256": receipt.log_sha256,
                },
                "kind": "test.reproduction",
            },
            idempotency_key=f"reproduced:{task_id}:{finding.finding_id}",
        )
        return updated_record

    def repair(self, task_id: UUID, finding_id: str) -> dict[str, Any]:
        task = self.store.task(task_id)
        finding = self.store.finding(task_id, finding_id)
        return self._prepare_repair(task, finding).view()

    def _prepare_repair(
        self, task: dict[str, Any], finding: WorkbenchFinding
    ) -> WorkbenchFinding:
        task_id = task["task_id"]
        if task["execution_mode"] != "fixture":
            raise DomainError(
                "LIVE_REPAIR_EXECUTION_NOT_ENABLED",
                "Live repair requires the handoff-driven runtime, not the fixture dispatcher",
                403,
            )
        if finding.stage != "reproduced" or finding.reproduction is None:
            raise DomainError(
                "FINDING_NOT_REPRODUCED",
                "A repair proposal requires a reproduced finding",
                409,
            )
        before = finding.reproduction.receipt
        if before is None or before.passed:
            raise DomainError(
                "REPRODUCTION_RECEIPT_REQUIRED",
                "A failing reproduction receipt is required before a patch is proposed",
                409,
            )
        task = self._transition(
            task,
            "repairing",
            status="repairing",
            reason=f"repair started for {finding.finding_id[:12]}",
            event={
                "stage": "repair",
                "status": "started",
                "summary": f"Preparing a verified patch for {finding.finding_id[:12]}",
                "payload": {"finding_id": finding.finding_id},
                "kind": "patch.repair",
            },
            idempotency_key=f"repair-started:{task_id}:{finding.finding_id}",
        )
        after, _ = self._fixture_tests(task, patched=True)
        if not after.passed:
            current = self.store.task(task_id)
            self._transition(
                current,
                "reproduced",
                status="ready",
                reason="the proposed patch did not pass the reproduction test",
                event={
                    "stage": "repair",
                    "status": "failed",
                    "summary": "The proposed patch did not pass the reproduction test",
                    "payload": {
                        "finding_id": finding.finding_id,
                        "receipt_id": after.receipt_id,
                    },
                    "kind": "patch.repair",
                },
            )
            raise DomainError(
                "PATCH_DID_NOT_PASS",
                "The proposed patch did not pass the reproduction test",
                409,
            )
        before_files = self._fixture_files(patched=False)
        after_files = self._fixture_files(patched=True)
        diff = _unified_diff(
            DISPATCHER_PATH,
            before_files[DISPATCHER_PATH],
            after_files[DISPATCHER_PATH],
        )
        diff_sha = hashlib.sha256(diff.encode()).hexdigest()
        tree_before = _tree_hash(before_files)
        tree_after = _tree_hash(after_files)
        repository = task["repository"]
        branch = f"mgar/u01-{finding.finding_id[:12]}"
        title = f"U01: {finding.title}"[:200]
        body = (
            f"Verified repair proposal for finding {finding.finding_id}.\n\n"
            f"Reproduction receipt: {before.receipt_id} (pytest failed on the unpatched tree)\n"
            f"Patch verification receipt: {after.receipt_id} "
            "(pytest passed on the patched tree)\n\n"
            "This is a Draft PR proposal. It has not been merged and the workbench never merges."
        )
        proposal_id = digest(
            {
                "finding_id": finding.finding_id,
                "diff_sha256": diff_sha,
                "after_receipt": after.receipt_id,
            }
        )
        bundle = seal_bundle(
            repository=repository,
            base_branch="main",
            base_sha=task["commit"],
            tree_hash=tree_after,
            proposal_id=proposal_id,
            branch=branch,
            title=title,
            body=body,
            diff=diff,
            files=[
                PublicationFile(path=DISPATCHER_PATH, content=after_files[DISPATCHER_PATH])
            ],
        )
        fix_data = {
            "suggested_fix": finding.suggested_fix or "Route every request through execute().",
            "diff": diff,
            "diff_sha256": diff_sha,
            "files": [DISPATCHER_PATH],
            "tree_hash_before": tree_before,
            "tree_hash_after": tree_after,
            "before_receipt": before,
            "after_receipt": after,
            "bundle": bundle,
        }
        from mgar_v3.workbench.domain import FixRecord

        verified = advance_finding(finding, fix=FixRecord.model_validate(fix_data))
        approval = ApprovalRecord(
            status="pending",
            payload_hash=bundle.payload_hash,
            requested_at=_now(),
        )
        updated = advance_finding(verified, approval=approval)
        self._persist_finding(task_id, updated)
        current = self.store.task(task_id)
        current = self._transition(
            current,
            "patch_verified",
            status="repairing",
            reason=f"patch {proposal_id[:12]} verified against the reproduction test",
            event={
                "stage": "repair",
                "status": "patch_verified",
                "summary": f"Patch verified for {finding.finding_id[:12]}",
                "payload": {
                    "finding_id": finding.finding_id,
                    "diff_sha256": diff_sha,
                    "proposal_id": proposal_id,
                },
                "kind": "patch.verified",
            },
            idempotency_key=f"patch-verified:{task_id}:{proposal_id}",
        )
        self._transition(
            current,
            "waiting_approval",
            status="waiting_approval",
            stage="waiting_approval",
            reason="human approval is required before publication",
            event={
                "stage": "repair",
                "status": "waiting_approval",
                "summary": (
                    f"Patch verified for {finding.finding_id[:12]}; human approval required"
                ),
                "payload": {
                    "finding_id": finding.finding_id,
                    "diff_sha256": diff_sha,
                    "payload_hash": bundle.payload_hash,
                },
                "kind": "approval.requested",
            },
            idempotency_key=f"approval-requested:{task_id}:{bundle.payload_hash}",
        )
        return updated

    # -- approval and publication ---------------------------------------
    def approve(
        self,
        task_id: UUID,
        finding_id: str,
        *,
        payload_hash: str,
        actor: str | None = None,
    ) -> dict[str, Any]:
        task = self.store.task(task_id)
        finding = self.store.finding(task_id, finding_id)
        actor = actor or self.settings.local_actor
        if actor.lower() not in {item.lower() for item in self.settings.allowed_actors}:
            raise DomainError("ACTOR_NOT_ALLOWED", "Actor is not configured for approvals", 403)
        if finding.stage != "waiting_approval" or finding.fix is None or finding.approval is None:
            raise DomainError(
                "APPROVAL_NOT_PENDING", "Finding is not waiting for approval", 409
            )
        if (
            finding.approval.payload_hash != payload_hash
            or finding.fix.bundle.payload_hash != payload_hash
        ):
            raise DomainError(
                "APPROVAL_STALE",
                "Approval payload hash does not match the reviewed patch",
                409,
            )
        if finding.publication is not None and finding.publication.status == "published":
            return finding.view()
        task = self._transition(
            task,
            "publishing",
            status="publishing",
            reason=f"publication approved by {actor}",
            event={
                "stage": "approval",
                "status": "approved",
                "summary": f"Human approval recorded for {finding_id[:12]}",
                "payload": {
                    "finding_id": finding_id,
                    "payload_hash": payload_hash,
                },
                "kind": "approval.approved",
            },
            idempotency_key=f"approval-approved:{task_id}:{payload_hash}",
        )
        approval = PublicationApproval.grant(
            finding.fix.bundle, actor=actor, approved_at=_now()
        )
        approved_record = ApprovalRecord(
            status="approved",
            payload_hash=payload_hash,
            requested_at=finding.approval.requested_at,
            actor=actor,
            approved_at=approval.approved_at,
            approval_id=approval.approval_id,
        )
        with_approval = advance_finding(finding, approval=approved_record)
        self._persist_finding(task_id, with_approval)
        remote = _InMemoryRemote(
            repository=task["repository"],
            base_branch="main",
            base_sha=task["commit"],
            files=self._fixture_files(patched=False),
        )
        publisher = DraftPRPublisher(remote, remote, memory=MemoryPublicationLog())
        outcome = publisher.publish(finding.fix.bundle, approval)
        from mgar_v3.workbench.domain import PublicationRecord

        publication = PublicationRecord(
            status=outcome.status,
            reason=outcome.reason,
            steps=outcome.steps,
            pull_request=outcome.pull_request,
            pull_request_url=(
                f"fixture://github/{task['repository']}/pull/{outcome.pull_request.number}"
                if outcome.pull_request is not None
                else None
            ),
            remote="in_memory_fixture",
            published_at=_now() if outcome.status == "published" else None,
        )
        updated = advance_finding(with_approval, publication=publication)
        self._persist_finding(task_id, updated)
        published = updated.stage == "pr_created"
        current = self.store.task(task_id)
        self._transition(
            current,
            "pr_created" if published else "waiting_approval",
            status="completed" if published else "waiting_approval",
            stage=updated.stage,
            reason=(
                "draft PR receipted in the fixture remote"
                if published
                else f"publication did not create a Draft PR: {outcome.reason}"
            ),
            event={
                "stage": "publication",
                "status": "published" if published else outcome.status,
                "summary": (
                    "Draft PR receipted in the fixture remote"
                    if published
                    else f"Publication did not create a Draft PR: {outcome.reason}"
                ),
                "payload": {
                    "finding_id": finding_id,
                    "status": outcome.status,
                    "steps": [step.model_dump(mode="json") for step in outcome.steps],
                    "pull_request": (
                        outcome.pull_request.model_dump(mode="json")
                        if outcome.pull_request is not None
                        else None
                    ),
                },
                "kind": "pr.receipt",
            },
            idempotency_key=f"publication:{task_id}:{approval.approval_id}",
        )
        return updated.view()

    # -- controls --------------------------------------------------------
    def pause(self, task_id: UUID) -> dict[str, Any]:
        task = self.store.task(task_id)
        if task["status"] not in {"queued", "auditing", "reproducing", "repairing"}:
            raise DomainError("TASK_NOT_PAUSABLE", "Task is not running", 409)
        if task["audit_run_id"] is not None:
            try:
                self.repo.pause(task["audit_run_id"])
            except DomainError:
                pass
        self._transition(
            task,
            "paused",
            status="paused",
            reason="paused by a human",
            bump_epoch=True,
            event={
                "stage": "control",
                "status": "paused",
                "summary": "Task paused by a human",
                "kind": "task.control",
            },
            idempotency_key=f"paused:{task_id}:{task['revision']}",
        )
        return self.view(task_id)

    def resume(self, task_id: UUID) -> dict[str, Any]:
        task = self.store.task(task_id)
        if task["status"] != "paused":
            raise DomainError("TASK_NOT_PAUSED", "Task is not paused", 409)
        if task["audit_run_id"] is not None:
            try:
                self.repo.resume(task["audit_run_id"])
            except DomainError:
                pass
        self._transition(
            task,
            task["resume_phase"],
            status="queued",
            reason="resumed by a human",
            bump_epoch=True,
            event={
                "stage": "control",
                "status": "resumed",
                "summary": "Task resumed by a human",
                "payload": {"phase": task["resume_phase"]},
                "kind": "task.control",
            },
            idempotency_key=f"resumed:{task_id}:{task['revision']}",
        )
        return self.view(task_id)

    def cancel(self, task_id: UUID) -> dict[str, Any]:
        task = self.store.task(task_id)
        if task["status"] in {"completed", "cancelled"}:
            raise DomainError("TASK_NOT_CANCELLABLE", "Task is already terminal", 409)
        if task["audit_run_id"] is not None:
            try:
                self.repo.cancel(task["audit_run_id"])
            except DomainError:
                pass
        self._transition(
            task,
            "cancelled",
            status="cancelled",
            reason="cancelled by a human",
            bump_epoch=True,
            event={
                "stage": "control",
                "status": "cancelled",
                "summary": "Task cancelled by a human",
                "kind": "task.control",
            },
            idempotency_key=f"cancelled:{task_id}:{task['revision']}",
        )
        return self.view(task_id)

    # -- follow-up -------------------------------------------------------
    def follow_up(
        self,
        task_id: UUID,
        body: str,
        *,
        client_request_id: str | None = None,
    ) -> dict[str, Any]:
        parent = self.store.task(task_id)
        if parent["execution_mode"] == "live":
            request = self._follow_up_request(parent, body, client_request_id)
            return self.create(request)
        request = self._follow_up_request(parent, body, client_request_id)
        repository = parent["repository"]
        request_digest = digest(request.model_dump(mode="json"))
        request_key = (
            f"workbench:{client_request_id}"
            if client_request_id
            else f"workbench:follow:{task_id}:{uuid4()}"
        )
        limits = AnalysisLimits.model_validate(parent["limits"] or {})
        task = self.store.create_task(
            request,
            repository=repository,
            commit=parent["commit"],
            request_key=request_key,
            request_digest=request_digest,
            task_text=body,
            limits=limits.model_dump(mode="json"),
            parent_task_id=task_id,
            thread_id=parent["thread_id"],
        )
        self._event(
            task_id,
            stage="follow_up",
            status="created",
            summary="Follow-up task created in the same thread",
            payload={"child_task_id": str(task["task_id"])},
        )
        if parent["audit_run_id"] is not None:
            try:
                spec = self.repo.follow_up(
                    parent["audit_run_id"],
                    FollowUpInput(
                        client_request_id=request_key,
                        body=body,
                    ),
                )
            except DomainError:
                spec = None
            if spec is not None:
                task = self._transition(
                    task,
                    "investigating",
                    status="auditing",
                    reason="follow-up audit started in the same thread",
                    event={
                        "stage": "audit",
                        "status": "started",
                        "summary": "Follow-up audit started",
                        "payload": {"run_id": str(spec.run_id)},
                        "kind": "run.audit",
                    },
                    idempotency_key=f"follow-up-started:{task['task_id']}",
                )
                self.store.link_run(task["task_id"], "audit", spec.run_id)
                self.repo.execute(spec.run_id)
                self._import_audit(task["task_id"], spec.run_id)
                return self.view(task["task_id"])
        return self.run(task["task_id"])

    # -- read ------------------------------------------------------------
    def view(self, task_id: UUID) -> dict[str, Any]:
        task = self._sync_live_audit(self.store.task(task_id))
        findings = rank_findings(self.store.findings(task_id))
        events = self.store.events(task_id)
        memory = self.store.memory(task_id)
        stage = self._task_stage(findings, task)
        return {
            "task_id": task["task_id"],
            "thread_id": task["thread_id"],
            "parent_task_id": task["parent_task_id"],
            "mode": task["mode"],
            "repository_url": task["repository_url"],
            "repository": task["repository"],
            "commit": task["commit"],
            "task": task["task"],
            "execution_mode": task["execution_mode"],
            "fixture": task["execution_mode"] == "fixture",
            "status": task["status"],
            "phase": task["phase"],
            "resume_phase": task["resume_phase"],
            "execution_epoch": task["execution_epoch"],
            "stage": stage,
            "stage_label": STAGE_LABELS[stage],
            "revision": task["revision"],
            "run_links": self.store.run_links(task_id),
            "transitions": self.store.transitions(task_id),
            "handoffs": self.store.handoffs(task_id),
            "audit_run_id": task["audit_run_id"],
            "repair_run_id": task["repair_run_id"],
            "verifier_run_id": task["verifier_run_id"],
            "created_at": task["created_at"],
            "updated_at": task["updated_at"],
            "coverage": {
                "request": task["coverage"],
                "observed": task["observed_coverage"] or {},
            },
            "findings": [finding.view() for finding in findings],
            "memory": memory
            or {
                "query": task["task"],
                "commit": task["commit"],
                "retrieval_mode": "not_run",
                "follow_up": False,
                "advisories": [],
                "plan_hints": [],
                "confirmed_facts": [],
                "excluded": [],
            },
            "timeline": events,
            "summary": self._summary(findings),
            "next_action": self._next_action(task, findings),
            "controls": self._controls(task),
            "labels": dict(STAGE_LABELS),
        }

    def history(self, limit: int = 30) -> list[dict[str, Any]]:
        rows = []
        for task in self.store.tasks(limit):
            findings = rank_findings(self.store.findings(task["task_id"]))
            rows.append(
                {
                    "task_id": task["task_id"],
                    "mode": task["mode"],
                    "repository": task["repository"],
                    "commit": task["commit"],
                    "task": task["task"],
                    "status": task["status"],
                    "phase": task["phase"],
                    "stage": self._task_stage(findings, task),
                    "stage_label": STAGE_LABELS[self._task_stage(findings, task)],
                    "execution_mode": task["execution_mode"],
                    "findings": len(findings),
                    "created_at": task["created_at"],
                    "revision": task["revision"],
                }
            )
        return rows

    def finding(self, task_id: UUID, finding_id: str) -> dict[str, Any]:
        return self.store.finding(task_id, finding_id).view()

    def diff(self, task_id: UUID, finding_id: str) -> dict[str, Any]:
        finding = self.store.finding(task_id, finding_id)
        if finding.fix is None:
            raise DomainError("DIFF_NOT_READY", "No verified patch exists for this finding", 404)
        return {
            "finding_id": finding.finding_id,
            "diff": finding.fix.diff,
            "diff_sha256": finding.fix.diff_sha256,
            "files": finding.fix.files,
            "before_receipt": finding.fix.before_receipt.model_dump(mode="json"),
            "after_receipt": finding.fix.after_receipt.model_dump(mode="json"),
            "tree_hash_before": finding.fix.tree_hash_before,
            "tree_hash_after": finding.fix.tree_hash_after,
            "payload_hash": finding.fix.bundle.payload_hash,
            "reviewed": True,
        }

    def profile(self) -> dict[str, Any]:
        analysis = self.repo.profile()
        return {
            "modes": ["audit", "issue_repair", "audit_and_repair"],
            "execution_modes": ["fixture", "live"],
            "stages": dict(STAGE_LABELS),
            "fixture_repair": True,
            "live_repair": "r05-profile-required",
            "live_publication": "r05-profile-required",
            "merge_supported": False,
            "analysis": analysis,
            "notes": [
                "Suggested fixes are proposals, never applied changes.",
                "Patch Verified requires a failing reproduction and a passing post-patch receipt.",
                "PR Created is a Draft PR receipt; the workbench never merges.",
            ],
        }

    # -- internals -------------------------------------------------------
    def _authority_check(self, request: CreateWorkbenchTask) -> None:
        if request.execution_mode == "live" and request.mode == "issue_repair":
            raise DomainError(
                "WORKBENCH_LIVE_REPAIR_PROFILE_REQUIRED",
                "Live issue repair and publication remain behind the R05 profile flow",
                403,
            )

    @staticmethod
    def _task_text(request: CreateWorkbenchTask) -> str:
        if request.mode == "issue_repair":
            return request.task.strip() or f"Repair issue #{request.issue_number}"
        return request.task.strip()

    def _follow_up_request(
        self, parent: dict[str, Any], body: str, client_request_id: str | None
    ) -> CreateWorkbenchTask:
        return CreateWorkbenchTask(
            repository_url=parent["repository_url"],
            mode="audit",
            task=body,
            commit=parent["commit"],
            execution_mode=parent["execution_mode"],
            coverage=CoverageRequest.model_validate(parent["coverage"]),
            limits=parent["limits"],
            auto_start=True,
            client_request_id=client_request_id,
        )

    def _sync_live_audit(self, task: dict[str, Any]) -> dict[str, Any]:
        if (
            task["execution_mode"] != "live"
            or task["audit_run_id"] is None
            or task["status"] not in {"queued", "auditing"}
        ):
            return task
        detail = self.repo.detail(task["audit_run_id"])
        run = detail["run"]
        if run["run_status"] == "succeeded" and not self.store.findings(task["task_id"]):
            self._import_audit(task["task_id"], task["audit_run_id"], task=task)
            return self.store.task(task["task_id"])
        if run["run_status"] in {"failed", "cancelled"}:
            try:
                self._transition(
                    task,
                    "failed",
                    status="failed",
                    reason=f"live audit run ended as {run['run_status']}",
                    event={
                        "stage": "audit",
                        "status": "failed",
                        "summary": f"Live audit run ended as {run['run_status']}",
                        "payload": {"run_id": str(task["audit_run_id"])},
                        "kind": "run.audit",
                    },
                    idempotency_key=f"audit-failed:{task['task_id']}",
                )
            except DomainError:
                pass
        return self.store.task(task["task_id"])

    def _build_handoff(
        self,
        task: dict[str, Any],
        finding: WorkbenchFinding,
        authorization: RepairAuthorization,
    ) -> RepairHandoff:
        if finding.verification not in _VERIFIED_STATES:
            raise DomainError(
                "HANDOFF_TARGET_UNVERIFIED",
                "Only a reproduced or statically confirmed finding can start a repair",
                409,
            )
        parent = self._parent_analysis(task)
        if parent.repository != task["repository"].lower():
            raise DomainError(
                "HANDOFF_REPOSITORY_MISMATCH",
                "Audit parent belongs to a different repository",
                409,
            )
        if parent.commit != task["commit"]:
            raise DomainError(
                "HANDOFF_VERSION_MISMATCH",
                "Audit parent reviewed a different commit",
                409,
            )
        evidence = self._verification_evidence(task, finding)
        snapshot = self._snapshot_finding(task, finding, evidence)
        objective = (finding.suggested_fix.strip() or f"Repair: {finding.title}").strip()
        budget = AnalysisLimits.model_validate(task["limits"] or {})
        bundle = RepairHandoffBuilder(None, allowed_actors=self.settings.allowed_actors)
        return bundle.build_from_snapshot(
            snapshot,
            evidence,
            parent=parent,
            objective=objective,
            budget=budget,
            authorization=authorization,
            allowed_paths=None,
            execution_mode=task["execution_mode"],
        )

    def _reuse_handoff(
        self,
        task: dict[str, Any],
        finding: WorkbenchFinding,
        stored: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            handoff = RepairHandoff.model_validate(stored["payload"])
        except ValueError as exc:
            raise DomainError(
                "HANDOFF_CORRUPTED", "Stored handoff payload is invalid", 409
            ) from exc
        if (
            handoff.handoff_id != stored["handoff_id"]
            or handoff.computed_id() != handoff.handoff_id
            or handoff.handoff_id != stored["content_hash"]
        ):
            raise DomainError(
                "HANDOFF_CORRUPTED", "Stored handoff identity does not match its content", 409
            )
        self.engine.artifacts.read(stored["content_hash"])
        if handoff.finding_id != finding.finding_id:
            raise DomainError(
                "HANDOFF_STALE", "Stored handoff targets a different finding", 409
            )
        if handoff.repository != task["repository"].lower():
            raise DomainError(
                "HANDOFF_REPOSITORY_MISMATCH", "Stored handoff targets another repository", 409
            )
        if handoff.base_commit != task["commit"]:
            raise DomainError(
                "HANDOFF_VERSION_MISMATCH", "Stored handoff targets another commit", 409
            )
        current = self._verification_evidence(task, finding)
        if [item.model_dump(mode="json") for item in current] != [
            item.model_dump(mode="json") for item in handoff.evidence
        ]:
            raise DomainError(
                "HANDOFF_STALE",
                "Finding evidence changed after the handoff was minted",
                409,
            )
        parent = self._parent_analysis(task)
        if (
            parent.run_id != handoff.parent_run_id
            or parent.report_hash != handoff.parent_report_hash
            or parent.status != "succeeded"
        ):
            raise DomainError(
                "HANDOFF_PARENT_NOT_SETTLED",
                "The parent analysis no longer matches the handoff",
                409,
            )
        if handoff.execution_budget != AnalysisLimits.model_validate(task["limits"] or {}):
            raise DomainError(
                "HANDOFF_STALE", "Task limits changed after the handoff was minted", 409
            )
        handoff.authorization.validate_scope(
            repository=handoff.repository,
            budget=handoff.execution_budget,
            allowed_actors=self.settings.allowed_actors,
        )
        return self._handoff_view(handoff, stored, reused=True)

    def _parent_analysis(self, task: dict[str, Any]) -> ParentAnalysisRun:
        run_id = task["audit_run_id"]
        if run_id is None:
            raise DomainError(
                "HANDOFF_PARENT_NOT_SETTLED",
                "A repair handoff requires a completed read-only audit",
                409,
            )
        try:
            spec = self.repo.store.spec(run_id)
            return ParentAnalysisRun.from_spec(spec)
        except DomainError as exc:
            if exc.code == "HANDOFF_PARENT_NOT_SETTLED":
                raise
            raise DomainError(
                "HANDOFF_PARENT_NOT_SETTLED",
                "The parent audit is not available for a handoff",
                409,
            ) from exc

    def _bound_source_evidence(
        self, task: dict[str, Any], finding: WorkbenchFinding
    ) -> FindingEvidence:
        source = finding.source
        if not source.evidence_id or not _HASH.fullmatch(source.content_hash):
            raise DomainError(
                "HANDOFF_EVIDENCE_MISSING",
                "A handoff needs retained evidence with a content hash",
                409,
            )
        if not source.commit or source.commit != task["commit"]:
            raise DomainError(
                "HANDOFF_VERSION_MISMATCH",
                "Finding evidence is not bound to the task commit",
                409,
            )
        match = _EVIDENCE_URI.match(source.uri)
        if match is None:
            raise DomainError(
                "HANDOFF_EVIDENCE_MISSING",
                "Finding evidence is not a commit-addressed repository URI",
                409,
            )
        if match.group("repository").lower() != task["repository"].lower():
            raise DomainError(
                "HANDOFF_REPOSITORY_MISMATCH",
                "Finding evidence comes from another repository",
                409,
            )
        if match.group("commit") != source.commit:
            raise DomainError(
                "HANDOFF_VERSION_MISMATCH",
                "Finding evidence URI names a different commit",
                409,
            )
        uri_path = normalize_path(match.group("path") or "")
        if (
            not finding.path
            or len(finding.path) > 400
            or (uri_path and normalize_path(finding.path) != uri_path)
        ):
            raise DomainError(
                "HANDOFF_PATH_DENIED",
                "Finding evidence does not point at the finding's allowed path",
                409,
            )
        if finding.start_line < 1 or finding.end_line < finding.start_line:
            raise DomainError(
                "HANDOFF_EVIDENCE_MISSING", "Finding line range is invalid", 409
            )
        description = (source.quote.strip() or finding.summary.strip() or finding.title)[:1000]
        return FindingEvidence(
            evidence_id=source.evidence_id,
            kind="investigation",
            source_uri=source.uri,
            description=description,
            content_hash=source.content_hash,
            location=ScanLocation(
                path=finding.path,
                line=finding.start_line,
                end_line=finding.end_line,
            ),
        )

    def _verification_evidence(
        self, task: dict[str, Any], finding: WorkbenchFinding
    ) -> list[FindingEvidence]:
        bound = self._bound_source_evidence(task, finding)
        if finding.verification == "reproduced":
            receipt = (
                finding.reproduction.receipt
                if finding.reproduction is not None
                else None
            )
            if receipt is None or receipt.passed:
                raise DomainError(
                    "HANDOFF_REPRODUCTION_PROOF_MISSING",
                    "A reproduced finding needs a failing execution receipt",
                    409,
                )
            return [
                bound,
                FindingEvidence(
                    evidence_id=receipt.receipt_id,
                    kind="test_run",
                    source_uri=f"receipt://{receipt.receipt_id}",
                    description=f"reproduced: {receipt.command}"[:1000],
                    content_hash=receipt.receipt_id,
                    location=bound.location,
                ),
            ]
        if finding.verification == "confirmed_static":
            confirmation = finding.static_confirmation
            if confirmation is None:
                raise DomainError(
                    "HANDOFF_REPRODUCTION_PROOF_MISSING",
                    "Static confirmation evidence is missing",
                    409,
                )
            static = confirmation.evidence
            if static.kind not in STATIC_EVIDENCE_KINDS:
                raise DomainError(
                    "HANDOFF_REPRODUCTION_PROOF_MISSING",
                    "Static confirmation needs scanner or static-analysis evidence",
                    409,
                )
            if (
                static.evidence_id != bound.evidence_id
                or static.source_uri != bound.source_uri
                or static.content_hash != bound.content_hash
            ):
                raise DomainError(
                    "HANDOFF_EVIDENCE_MISSING",
                    "Static evidence does not match the finding's bound source",
                    409,
                )
            location = static.location
            if (
                location is None
                or location.path != finding.path
                or (location.line or finding.start_line) != finding.start_line
                or (location.end_line or finding.end_line) != finding.end_line
            ):
                raise DomainError(
                    "HANDOFF_EVIDENCE_MISSING",
                    "Static evidence line range does not match the finding",
                    409,
                )
            return [static]
        raise DomainError(
            "HANDOFF_TARGET_UNVERIFIED",
            "Only a reproduced or statically confirmed finding can start a repair",
            409,
        )

    def _snapshot_finding(
        self,
        task: dict[str, Any],
        finding: WorkbenchFinding,
        evidence: list[FindingEvidence],
    ) -> Finding:
        reproduced = finding.verification == "reproduced"
        rule_id = finding.rule_id or "workbench"
        return Finding(
            finding_id=finding.finding_id,
            run_id=str(task["task_id"]),
            repository=task["repository"].lower(),
            commit=finding.source.commit,
            category=rule_id[:60],
            classification=finding.classification,
            severity=finding.severity,
            confidence=finding.confidence,
            summary=(finding.summary.strip() or finding.title)[:2000],
            source_locations=[
                ScanLocation(
                    path=finding.path,
                    line=finding.start_line,
                    end_line=finding.end_line,
                )
            ],
            evidence_ids=[item.evidence_id for item in evidence],
            reproduction_status="reproduced" if reproduced else "static_only",
            suggested_fix=finding.suggested_fix,
            lifecycle_status="reproduced" if reproduced else "confirmed_static",
            uncertainty=(
                "Verified by the workbench ledger; the handoff inherits no "
                "natural-language authority."
            ),
            fix_risk=finding.fix_risk,
            origin="investigation",
            dedupe_key=f"{finding.path}|{rule_id}|{finding.finding_id}"[:1000],
            rule_ids=[rule_id[:200]],
        )

    @staticmethod
    def _handoff_view(
        handoff: RepairHandoff, stored: dict[str, Any], *, reused: bool
    ) -> dict[str, Any]:
        return {
            "handoff_id": handoff.handoff_id,
            "content_hash": stored["content_hash"],
            "artifact_verified": True,
            "reused": reused,
            "finding_id": handoff.finding_id,
            "repository": handoff.repository,
            "base_commit": handoff.base_commit,
            "parent_run_id": str(handoff.parent_run_id),
            "parent_report_hash": handoff.parent_report_hash,
            "allowed_paths": list(handoff.allowed_paths),
            "evidence_ids": [item.evidence_id for item in handoff.evidence],
            "reproduction": handoff.reproduction.model_dump(mode="json"),
            "authorization_hash": handoff.authorization_hash,
            "execution_budget": handoff.execution_budget.model_dump(mode="json"),
            "execution_mode": handoff.execution_mode,
            "created_at": stored["created_at"],
            "handoff": handoff.model_dump(mode="json"),
        }

    def _import_audit(
        self,
        task_id: UUID,
        run_id: UUID,
        *,
        task: dict[str, Any] | None = None,
    ) -> None:
        task = task or self.store.task(task_id)
        detail = self.repo.detail(run_id)
        findings: list[WorkbenchFinding] = []
        for claim in detail["claims"]:
            if claim["section"] != "findings":
                continue
            try:
                evidence = self.repo.evidence_for_claim(run_id, UUID(claim["evidence_id"]))
            except DomainError:
                continue
            source_uri = str(evidence["source_uri"])
            title = str(claim["statement"]).strip()[:120] or "Investigation finding"
            findings.append(
                self._finding(
                    task,
                    rule_id="analysis-claim",
                    title=title,
                    summary=str(claim["statement"]),
                    path=_path_from_uri(source_uri),
                    start_line=int(claim["start_line"]),
                    end_line=int(claim["end_line"]),
                    severity="warning",
                    classification="risk",
                    confidence=0.6,
                    source=FindingSource(
                        kind="file",
                        uri=source_uri,
                        evidence_id=str(claim["evidence_id"]),
                        quote=str(claim["quote"])[:2000],
                        content_hash=str(evidence["content_hash"]),
                        commit=task["commit"],
                    ),
                )
            )
        if task["execution_mode"] == "fixture":
            findings.extend(self._fixture_findings(task))
        findings = self._dedupe(findings)
        findings = rank_findings(findings)
        self.store.upsert_findings(task_id, findings)
        self._store_memory(task_id, run_id)
        observed = self._observe_coverage(task, detail, findings)
        task = self.store.update(task_id, observed_coverage=observed)
        self._transition(
            task,
            "suspected",
            status="ready",
            reason=f"audit produced {len(findings)} finding(s); none is labelled fixed",
            event={
                "stage": "audit",
                "status": "completed",
                "summary": (
                    f"Audit produced {len(findings)} finding(s); none is labelled fixed"
                ),
                "payload": {"findings": len(findings), "coverage": observed},
                "kind": "audit.completed",
            },
            idempotency_key=f"audit-completed:{task_id}",
        )
        for event in detail["events"][:20]:
            self._event(
                task_id,
                stage="agent",
                status=str(event["kind"]),
                summary=str(event["kind"]),
                payload={
                    "created_at": event["created_at"],
                    "safe_payload": str(event["payload"])[:1000],
                },
            )

    def _store_memory(self, task_id: UUID, run_id: UUID) -> None:
        try:
            spec = self.repo.store.spec(run_id)
            memory = self.repo.memory.recall_task(spec)
            self.store.set_memory(task_id, memory.model_dump(mode="json"))
        except DomainError as exc:
            self.store.set_memory(
                task_id,
                {
                    "query": "",
                    "commit": "",
                    "retrieval_mode": "unavailable",
                    "error_code": exc.code,
                    "advisories": [],
                    "plan_hints": [],
                    "confirmed_facts": [],
                    "excluded": [],
                },
            )

    def _observe_coverage(
        self,
        task: dict[str, Any],
        detail: dict[str, Any],
        findings: list[WorkbenchFinding],
    ) -> dict[str, Any]:
        request = CoverageRequest.model_validate(task["coverage"])
        if task["execution_mode"] == "fixture":
            files = dict(FIXTURE_FILES)
            return {
                "files_considered": len(files),
                "files_scanned": len(files),
                "unscanned_files": [],
                "languages": _languages(files),
                "complete": True,
                "source": "fixture_repository",
                "scope": request.scope,
                "include_paths": request.include_paths,
                "exclude_paths": request.exclude_paths,
            }
        scanned = sorted(
            {
                _path_from_uri(str(call["result_uri"]))
                for call in detail["tool_calls"]
                if call["tool"] == "read_file" and call["status"] == "succeeded"
            }
            - {""}
        )
        return {
            "files_considered": max(len(scanned), len(findings)),
            "files_scanned": len(scanned),
            "unscanned_files": [],
            "languages": _languages({path: "" for path in scanned}),
            "complete": bool(detail.get("report")),
            "source": "github_read_only",
            "scope": request.scope,
            "include_paths": request.include_paths,
            "exclude_paths": request.exclude_paths,
        }

    def _fixture_findings(self, task: dict[str, Any]) -> list[WorkbenchFinding]:
        dispatcher = FIXTURE_FILES[DISPATCHER_PATH]
        service = FIXTURE_FILES["src/service.py"]
        readme = FIXTURE_FILES["README.md"]
        return [
            self._finding(
                task,
                rule_id="WB-DISPATCH-SILENT-UNKNOWN",
                title="未知请求被静默降级为 'unknown'",
                summary=(
                    "dispatch() 只把 'startup' 交给 execute()，其余请求直接返回字符串 'unknown'。"
                    "调用方无法区分业务结果与错误，真实缺陷会被静默吞掉。"
                ),
                path=DISPATCHER_PATH,
                start_line=1,
                end_line=4,
                severity="error",
                classification="defect",
                confidence=0.92,
                source=self._fixture_source(
                    task, DISPATCHER_PATH, dispatcher, "WB-DISPATCH-SILENT-UNKNOWN"
                ),
                suggested_fix="让 dispatch 调用 execute，由 execute 对全部请求返回显式的处理结果。",
                plan=ReproductionPlan(
                    test_file=TEST_FILE,
                    test_node=TEST_NODE,
                    expected_failure="dispatch('unknown') should not silently return 'unknown'",
                ),
            ),
            self._finding(
                task,
                rule_id="WB-SERVICE-NO-VALIDATION",
                title="服务层缺少输入校验与错误边界",
                summary=(
                    "Service.handle() 直接转发请求，没有校验或异常边界；"
                    "配合静默降级会扩大影响范围。"
                ),
                path="src/service.py",
                start_line=1,
                end_line=5,
                severity="warning",
                classification="risk",
                confidence=0.74,
                source=self._fixture_source(
                    task, "src/service.py", service, "WB-SERVICE-NO-VALIDATION"
                ),
            ),
            self._finding(
                task,
                rule_id="WB-NO-REGRESSION-TESTS",
                title="仓库缺少可执行的回归测试",
                summary="Fixture 仓库没有测试目录或 CI 配置，缺陷无法被自动回归捕获。",
                path="README.md",
                start_line=1,
                end_line=4,
                severity="info",
                classification="quality",
                confidence=0.95,
                source=self._fixture_source(task, "README.md", readme, "WB-NO-REGRESSION-TESTS"),
            ),
        ]

    def _fixture_source(
        self, task: dict[str, Any], path: str, content: str, rule_id: str
    ) -> FindingSource:
        uri = f"github://{task['repository']}@{task['commit']}/{path}"
        return FindingSource(
            kind="file",
            uri=uri,
            evidence_id=str(uuid5(NAMESPACE_URL, f"{task['task_id']}|{rule_id}|{path}")),
            quote="\n".join(content.splitlines()[:5])[:1000],
            content_hash=hashlib.sha256(content.encode()).hexdigest(),
            commit=task["commit"],
        )

    def _finding(
        self,
        task: dict[str, Any],
        *,
        rule_id: str,
        title: str,
        summary: str,
        path: str,
        start_line: int,
        end_line: int,
        severity: Any,
        classification: Any,
        confidence: float,
        source: FindingSource,
        suggested_fix: str = "",
        plan: ReproductionPlan | None = None,
    ) -> WorkbenchFinding:
        return WorkbenchFinding(
            finding_id=new_finding_id(task["task_id"], rule_id, path, title),
            task_id=task["task_id"],
            title=title,
            summary=summary,
            rule_id=rule_id,
            path=path,
            start_line=start_line,
            end_line=end_line,
            classification=classification,
            severity=severity,
            fix_risk="low" if severity == "error" else "medium",
            confidence=confidence,
            source=source,
            suggested_fix=suggested_fix,
            reproduction_plan=plan,
            risk_score=score_finding(
                severity=severity,
                classification=classification,
                confidence=confidence,
                fix_risk="low" if severity == "error" else "medium",
            ),
        )

    def _seed_issue_finding(self, task: dict[str, Any]) -> WorkbenchFinding:
        issue = task.get("issue_number")
        number = int(issue) if issue is not None else 0
        title = f"Issue #{number}: {task['task'][:120]}" if number else task["task"][:140]
        return self._finding(
            task,
            rule_id=f"issue-{number}" if number else "issue-task",
            title=title,
            summary=task["task"],
            path=DISPATCHER_PATH,
            start_line=1,
            end_line=4,
            severity="warning",
            classification="defect",
            confidence=0.5,
            source=FindingSource(
                kind="issue",
                uri=(
                    f"https://github.com/{task['repository']}/issues/{number}"
                    if number
                    else f"github://{task['repository']}@{task['commit']}"
                ),
                evidence_id=str(uuid5(NAMESPACE_URL, f"{task['task_id']}|issue|{number}")),
                quote="Issue text is user input and is treated as data by the workbench.",
                content_hash="",
            ),
            suggested_fix="按 Issue 描述修复行为，并以失败→通过的测试回执验证。",
            plan=ReproductionPlan(
                test_file=TEST_FILE,
                test_node=TEST_NODE,
                expected_failure="The reported behavior is reproduced by the fixture test",
            ),
        )

    def _fixture_tests(
        self, task: dict[str, Any], *, patched: bool
    ) -> tuple[TestReceipt, str]:
        """Run the fixture reproduction on the host, never for a live task.

        V3.5 R02 removed the live repair fallback: any task that is not a
        fixture task must execute through ``SnapshotRepairRuntime`` inside the
        restricted R03 container, so untrusted repository tests can never be
        started on the host.
        """

        if task.get("execution_mode") != "fixture":
            raise DomainError(
                "WORKBENCH_LIVE_REPAIR_SANDBOX_REQUIRED",
                (
                    "Live repair tests are executed only by "
                    "repair.runtime.SnapshotRepairRuntime inside the restricted "
                    "R03 sandbox"
                ),
                409,
            )
        files = self._fixture_files(patched=patched)
        with tempfile.TemporaryDirectory(prefix="mgar-u01-") as directory:
            root = Path(directory)
            for path, content in files.items():
                target = root / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
            command = [sys.executable, "-m", "pytest", "-q", TEST_FILE]
            started = time.monotonic()
            try:
                completed = subprocess.run(
                    command,
                    cwd=root,
                    capture_output=True,
                    text=True,
                    timeout=90,
                    env=_safe_env(),
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise DomainError("TEST_TIMEOUT", "The reproduction test timed out", 504) from exc
            duration_ms = int((time.monotonic() - started) * 1000)
        output = (completed.stdout or "") + (completed.stderr or "")
        counts = {kind: int(number) for number, kind in _PYTEST.findall(output)}
        receipt = TestReceipt(
            receipt_id=digest(
                {
                    "command": " ".join(command),
                    "exit_code": completed.returncode,
                    "log_sha256": hashlib.sha256(output.encode()).hexdigest(),
                }
            ),
            command=f"python -m pytest -q {TEST_FILE}",
            exit_code=completed.returncode,
            passed=completed.returncode == 0,
            tests=[TEST_NODE],
            skipped=counts.get("skipped", 0),
            duration_ms=duration_ms,
            log_sha256=hashlib.sha256(output.encode()).hexdigest(),
            output_excerpt=output[-4000:],
        )
        return receipt, output

    def _fixture_files(self, *, patched: bool) -> dict[str, str]:
        files = dict(FIXTURE_FILES)
        files["src/__init__.py"] = ""
        files[TEST_FILE] = TEST_SOURCE
        if patched:
            files[DISPATCHER_PATH] = FIXED_DISPATCHER
        return files

    def _persist_finding(self, task_id: UUID, finding: WorkbenchFinding) -> None:
        self.store.upsert_findings(task_id, [finding])

    def _transition(
        self,
        task: dict[str, Any],
        to_phase: TaskPhase,
        *,
        status: TaskStatus | None = None,
        reason: str = "",
        stage: str | None = None,
        bump_epoch: bool = False,
        event: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Advance one task through the server-validated phase machine.

        Every write carries the caller's revision and execution epoch, so a
        stale worker is fenced before it can change a task.
        """
        return self.store.transition(
            task["task_id"],
            to_phase=to_phase,
            to_status=status,
            expected_revision=task["revision"],
            expected_epoch=task["execution_epoch"],
            actor=self.settings.local_actor,
            reason=reason,
            stage=stage,
            bump_epoch=bump_epoch,
            event=event,
            idempotency_key=idempotency_key,
        )

    def _event(
        self,
        task_id: UUID,
        *,
        stage: str,
        status: str,
        summary: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        self.store.append_events(
            task_id,
            [{"stage": stage, "status": status, "summary": summary, "payload": payload or {}}],
        )

    @staticmethod
    def _dedupe(findings: list[WorkbenchFinding]) -> list[WorkbenchFinding]:
        seen: dict[str, WorkbenchFinding] = {}
        for finding in findings:
            current = seen.get(finding.finding_id)
            if current is None or STAGE_RANK[finding.stage] > STAGE_RANK[current.stage]:
                seen[finding.finding_id] = finding
        return list(seen.values())

    @staticmethod
    def _task_stage(findings: list[WorkbenchFinding], task: dict[str, Any]) -> Any:
        if findings:
            return max(findings, key=lambda item: STAGE_RANK[item.stage]).stage
        return task["stage"]

    @staticmethod
    def _summary(findings: list[WorkbenchFinding]) -> dict[str, int]:
        summary: dict[str, int] = {stage: 0 for stage in STAGE_LABELS}
        for finding in findings:
            summary[finding.stage] += 1
        summary["total"] = len(findings)
        summary["suspected_with_plan"] = sum(
            1 for finding in findings if finding.stage == "suspected" and finding.reproduction_plan
        )
        return summary

    @staticmethod
    def _next_action(task: dict[str, Any], findings: list[WorkbenchFinding]) -> str:
        if task["status"] == "paused":
            return "resume"
        if task["status"] == "cancelled":
            return "none"
        if task["status"] == "queued":
            return "run"
        if task["status"] == "auditing":
            return "wait"
        if task["status"] == "waiting_approval":
            return "approve"
        if task["status"] == "completed":
            return "none"
        if any(finding.stage == "suspected" and finding.reproduction_plan for finding in findings):
            return "reproduce"
        if any(finding.stage == "reproduced" for finding in findings):
            return "repair"
        return "none"

    @staticmethod
    def _controls(task: dict[str, Any]) -> dict[str, bool]:
        status = task["status"]
        return {
            "can_pause": status in {"queued", "auditing", "reproducing", "repairing"},
            "can_resume": status == "paused",
            "can_cancel": status not in {"completed", "cancelled"},
            "can_run": status in {"queued", "ready", "failed"},
            "can_approve": status == "waiting_approval",
        }


__all__ = ["WorkbenchService"]
