"""Normalized domain for the V3.4 unified repository workbench.

The five UI labels are the contract of this module:

``Suspected`` -> ``Reproduced`` -> ``Patch Verified`` -> ``Waiting Approval``
-> ``PR Created``.

A suggested fix is only a proposal.  It can never move a finding past
``Suspected``/``Reproduced`` because every transition needs evidence: a failing
reproduction receipt, a passing post-patch receipt for the same tests, an
approval bound to the reviewed payload hash, and a receipted publication whose
Draft PR is verified.  A Draft PR is never a merge.
"""

from __future__ import annotations

import hashlib
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from mgar_v3.domain import DomainError, Model
from mgar_v3.repair.pr_delivery import DraftPullRequest, PublicationStep, ReviewBundle
from mgar_v3.repository_analysis.domain import parse_repository_url, validate_repo_path

WorkbenchMode = Literal["audit", "issue_repair", "audit_and_repair"]
TaskStatus = Literal[
    "queued",
    "auditing",
    "ready",
    "reproducing",
    "repairing",
    "waiting_approval",
    "publishing",
    "completed",
    "failed",
    "paused",
    "cancelled",
]
FindingStage = Literal[
    "suspected",
    "reproduced",
    "patch_verified",
    "waiting_approval",
    "pr_created",
]
Severity = Literal["error", "warning", "info"]
Classification = Literal["defect", "risk", "quality"]
FixRisk = Literal["low", "medium", "high"]
ExecutionMode = Literal["fixture", "live"]

STAGE_LABELS: dict[FindingStage, str] = {
    "suspected": "Suspected",
    "reproduced": "Reproduced",
    "patch_verified": "Patch Verified",
    "waiting_approval": "Waiting Approval",
    "pr_created": "PR Created",
}

STAGE_RANK: dict[FindingStage, int] = {
    "suspected": 0,
    "reproduced": 1,
    "patch_verified": 2,
    "waiting_approval": 3,
    "pr_created": 4,
}

ALLOWED_FINDING_STAGES: dict[FindingStage, set[FindingStage]] = {
    "suspected": {"reproduced"},
    "reproduced": {"patch_verified"},
    "patch_verified": {"waiting_approval"},
    "waiting_approval": {"pr_created"},
    "pr_created": set(),
}

ALLOWED_TASK_STAGES: dict[TaskStatus, set[TaskStatus]] = {
    "queued": {"auditing", "ready", "failed", "paused", "cancelled"},
    "auditing": {"ready", "failed", "paused", "cancelled"},
    "ready": {"reproducing", "repairing", "completed", "paused", "cancelled"},
    "reproducing": {"ready", "repairing", "failed", "paused", "cancelled"},
    "repairing": {"waiting_approval", "ready", "failed", "paused", "cancelled"},
    "waiting_approval": {"publishing", "cancelled"},
    "publishing": {"completed", "waiting_approval", "failed", "cancelled"},
    "completed": set(),
    "failed": {"queued", "cancelled"},
    "paused": {"queued", "cancelled"},
    "cancelled": set(),
}

SEVERITY_SCORE: dict[Severity, int] = {"error": 600, "warning": 350, "info": 120}
CLASSIFICATION_SCORE: dict[Classification, int] = {"defect": 200, "risk": 120, "quality": 0}
FIX_RISK_SCORE: dict[FixRisk, int] = {"low": 0, "medium": 20, "high": 40}


def validate_task_stage(current: TaskStatus, target: TaskStatus) -> None:
    if target == current:
        return
    if target not in ALLOWED_TASK_STAGES[current]:
        raise DomainError(
            "TASK_TRANSITION_INVALID",
            f"Task cannot move from {current} to {target}",
            409,
        )


class CoverageRequest(Model):
    """The requested repository scan scope, validated as repository-relative."""

    scope: Literal["repository", "paths"] = "repository"
    include_paths: list[str] = Field(default_factory=list, max_length=50)
    exclude_paths: list[str] = Field(default_factory=list, max_length=50)
    description: str = Field(default="", max_length=500)

    @field_validator("include_paths", "exclude_paths")
    @classmethod
    def repository_relative(cls, values: list[str]) -> list[str]:
        cleaned: list[str] = []
        for value in values:
            path = validate_repo_path(value.rstrip("/"))
            if path not in cleaned:
                cleaned.append(path)
        return cleaned

    @model_validator(mode="after")
    def paths_require_scope(self) -> CoverageRequest:
        if self.scope == "paths" and not self.include_paths:
            raise ValueError("paths scope requires at least one include_path")
        return self


class CreateWorkbenchTask(Model):
    repository_url: Annotated[str, Field(min_length=1, max_length=400)]
    mode: WorkbenchMode
    task: str = Field(default="", max_length=8000)
    ref: str | None = Field(default=None, max_length=200)
    commit: str | None = Field(default=None, max_length=40)
    issue_number: Annotated[int | None, Field(strict=True, gt=0)] = None
    execution_mode: ExecutionMode = "fixture"
    coverage: CoverageRequest = Field(default_factory=CoverageRequest)
    limits: dict[str, Any] = Field(default_factory=dict)
    auto_start: bool = True
    client_request_id: Annotated[str | None, Field(max_length=200)] = None

    @field_validator("repository_url")
    @classmethod
    def accepted_repository_url(cls, value: str) -> str:
        parse_repository_url(value)
        return value

    @field_validator("commit")
    @classmethod
    def full_commit_only(cls, value: str | None) -> str | None:
        if value is not None and (
            len(value) != 40 or any(c not in "0123456789abcdef" for c in value)
        ):
            raise ValueError("commit must be a full 40-character lowercase sha")
        return value

    @model_validator(mode="after")
    def mode_requirements(self) -> CreateWorkbenchTask:
        if self.mode == "issue_repair" and self.issue_number is None:
            raise ValueError("issue_repair requires issue_number")
        if self.mode != "issue_repair" and len(self.task.strip()) < 3:
            raise ValueError("audit modes require a natural-language task")
        return self


class ApproveWorkbenchFinding(Model):
    payload_hash: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class WorkbenchMessage(Model):
    body: Annotated[str, Field(min_length=1, max_length=8000)]
    client_request_id: Annotated[str | None, Field(max_length=200)] = None


class TestReceipt(Model):
    receipt_id: str = Field(min_length=1, max_length=100)
    command: str = Field(min_length=1, max_length=500)
    exit_code: int
    passed: bool
    tests: list[str] = Field(default_factory=list, max_length=50)
    skipped: int = Field(default=0, ge=0)
    duration_ms: int = Field(ge=0)
    log_sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    output_excerpt: str = Field(default="", max_length=4000)


class FindingSource(Model):
    kind: Literal["file", "issue", "pull_request", "test_run", "scan"] = "file"
    uri: str = Field(min_length=1, max_length=1000)
    evidence_id: str = Field(min_length=1, max_length=100)
    quote: str = Field(default="", max_length=2000)
    content_hash: str = Field(default="", max_length=64)


class ReproductionPlan(Model):
    """A concrete, repository-relative plan for demonstrating a defect."""

    test_file: str = Field(min_length=1, max_length=300)
    test_node: str = Field(min_length=1, max_length=500)
    expected_failure: str = Field(default="", max_length=1000)

    @field_validator("test_file", "test_node")
    @classmethod
    def relative_test_path(cls, value: str) -> str:
        path = value.split("::", 1)[0]
        validate_repo_path(path)
        return value


class ReproductionRecord(Model):
    status: Literal["not_attempted", "reproduced", "failed"] = "not_attempted"
    record: str = Field(default="", max_length=4000)
    command: str = Field(default="", max_length=500)
    receipt: TestReceipt | None = None


class ApprovalRecord(Model):
    status: Literal["pending", "approved", "rejected"] = "pending"
    payload_hash: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    requested_at: str = Field(min_length=1, max_length=40)
    actor: str = Field(default="", max_length=100)
    approved_at: str | None = Field(default=None, max_length=40)
    approval_id: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def approval_identity(self) -> ApprovalRecord:
        if self.status == "approved" and (
            not self.actor or self.approved_at is None or self.approval_id is None
        ):
            raise ValueError("approved findings require actor, approved_at and approval_id")
        return self


class PublicationRecord(Model):
    status: Literal[
        "not_started",
        "published",
        "write_permission_required",
        "base_changed",
        "outcome_unknown",
    ] = "not_started"
    reason: str = Field(default="", max_length=1000)
    steps: list[PublicationStep] = Field(default_factory=list, max_length=3)
    pull_request: DraftPullRequest | None = None
    pull_request_url: str | None = Field(default=None, max_length=500)
    merged: Literal[False] = False
    remote: Literal["in_memory_fixture", "github"] = "in_memory_fixture"
    published_at: str | None = Field(default=None, max_length=40)


class FixRecord(Model):
    """A verified patch.  It only exists after the same tests fail then pass."""

    suggested_fix: str = Field(min_length=1, max_length=4000)
    diff: str = Field(min_length=1, max_length=131072)
    diff_sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    files: list[str] = Field(min_length=1, max_length=16)
    tree_hash_before: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    tree_hash_after: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    before_receipt: TestReceipt
    after_receipt: TestReceipt
    bundle: ReviewBundle

    @model_validator(mode="after")
    def verified_evidence(self) -> FixRecord:
        if self.diff_sha256 != hashlib.sha256(self.diff.encode()).hexdigest():
            raise ValueError("diff_sha256 does not match the stored diff")
        if self.before_receipt.passed or not self.after_receipt.passed:
            raise ValueError("a verified fix requires a failing before and a passing after receipt")
        if self.before_receipt.command != self.after_receipt.command:
            raise ValueError("before and after receipts must run the same command")
        if sorted(self.before_receipt.tests) != sorted(self.after_receipt.tests):
            raise ValueError("before and after receipts must cover the same tests")
        if sorted(self.files) != sorted(item.path for item in self.bundle.files):
            raise ValueError("fix files must match the reviewed bundle")
        return self

    @property
    def verified(self) -> bool:
        return True


def derive_stage(
    *,
    reproduction: ReproductionRecord | None,
    fix: FixRecord | None,
    approval: ApprovalRecord | None,
    publication: PublicationRecord | None,
) -> FindingStage:
    """Return the highest stage that is fully supported by stored evidence."""

    if publication is not None and publication.status == "published":
        if approval is None or approval.status != "approved":
            raise ValueError("published findings require an approved review")
        if publication.pull_request is None or publication.pull_request.draft is not True:
            raise ValueError("published findings require a verified Draft PR")
        kinds = {
            step.kind
            for step in publication.steps
            if step.status in {"succeeded", "already_present"}
        }
        if not {"branch", "commit", "pull_request"} <= kinds:
            raise ValueError("published findings require branch, commit and pull request steps")
        return "pr_created"
    if approval is not None and approval.status in {"pending", "approved"}:
        if fix is None or not fix.verified:
            raise ValueError("approval requires a patch-verified fix")
        if approval.payload_hash != fix.bundle.payload_hash:
            raise ValueError("approval payload hash does not match the reviewed bundle")
        return "waiting_approval"
    if fix is not None and fix.verified:
        return "patch_verified"
    if reproduction is not None and reproduction.status == "reproduced":
        return "reproduced"
    return "suspected"


class WorkbenchFinding(Model):
    finding_id: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    task_id: UUID
    title: str = Field(min_length=1, max_length=300)
    summary: str = Field(default="", max_length=4000)
    rule_id: str = Field(default="", max_length=100)
    path: str = Field(default="", max_length=500)
    start_line: int = Field(default=1, ge=1)
    end_line: int = Field(default=1, ge=1)
    classification: Classification = "defect"
    severity: Severity = "warning"
    fix_risk: FixRisk = "low"
    confidence: float = Field(default=0.5, ge=0, le=1)
    source: FindingSource
    stage: FindingStage = "suspected"
    suggested_fix: str = Field(default="", max_length=4000)
    reproduction_plan: ReproductionPlan | None = None
    reproduction: ReproductionRecord | None = None
    fix: FixRecord | None = None
    approval: ApprovalRecord | None = None
    publication: PublicationRecord | None = None
    risk_score: int = Field(default=0, ge=0, le=1000)
    risk_rank: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def stage_is_evidence_backed(self) -> WorkbenchFinding:
        expected = derive_stage(
            reproduction=self.reproduction,
            fix=self.fix,
            approval=self.approval,
            publication=self.publication,
        )
        if self.stage != expected:
            raise ValueError(
                f"stage {self.stage} is not supported by evidence; expected {expected}"
            )
        return self

    @property
    def fix_state(self) -> str:
        if self.publication is not None and self.publication.status == "published":
            return "published"
        if self.approval is not None and self.approval.status in {"pending", "approved"}:
            return "awaiting_approval"
        if self.fix is not None:
            return "verified"
        if self.suggested_fix:
            return "proposed_not_verified"
        return "none"

    def view(self) -> dict[str, Any]:
        data = self.model_dump(mode="json")
        data["label"] = STAGE_LABELS[self.stage]
        data["fix_state"] = self.fix_state
        data["verified"] = self.fix is not None
        data["suggested_fix_is_not_applied"] = self.fix is None and bool(self.suggested_fix)
        data["disclaimer"] = (
            "A suggested fix is a proposal, not an applied change. Patch Verified requires a "
            "failing reproduction and a passing post-patch receipt. PR Created means a Draft PR "
            "was receipted; this workbench never merges."
        )
        return data


def score_finding(
    *,
    severity: Severity,
    classification: Classification,
    confidence: float,
    fix_risk: FixRisk,
) -> int:
    return (
        SEVERITY_SCORE[severity]
        + CLASSIFICATION_SCORE[classification]
        + FIX_RISK_SCORE[fix_risk]
        + round(confidence * 100)
    )


def rank_findings(findings: list[WorkbenchFinding]) -> list[WorkbenchFinding]:
    ordered = sorted(findings, key=lambda item: (-item.risk_score, item.path, item.finding_id))
    return [
        item.model_copy(update={"risk_rank": index}) for index, item in enumerate(ordered, start=1)
    ]


def advance_finding(finding: WorkbenchFinding, **updates: Any) -> WorkbenchFinding:
    """Apply updates, recompute the stage and reject any skipped evidence gate."""

    data = finding.model_dump(mode="json")
    data.update(updates)
    reproduction = (
        ReproductionRecord.model_validate(data["reproduction"])
        if data.get("reproduction") is not None
        else None
    )
    fix = FixRecord.model_validate(data["fix"]) if data.get("fix") is not None else None
    approval = (
        ApprovalRecord.model_validate(data["approval"])
        if data.get("approval") is not None
        else None
    )
    publication = (
        PublicationRecord.model_validate(data["publication"])
        if data.get("publication") is not None
        else None
    )
    data["stage"] = derive_stage(
        reproduction=reproduction,
        fix=fix,
        approval=approval,
        publication=publication,
    )
    updated = WorkbenchFinding.model_validate(data)
    if updated.stage == finding.stage:
        return updated
    if updated.stage not in ALLOWED_FINDING_STAGES[finding.stage]:
        raise DomainError(
            "FINDING_STAGE_INVALID",
            f"Finding cannot skip from {finding.stage} to {updated.stage}",
            409,
        )
    return updated


def new_finding_id(task_id: UUID, rule_id: str, path: str, title: str) -> str:
    return hashlib.sha256(f"{task_id}|{rule_id}|{path}|{title}".encode()).hexdigest()


def task_status_for_findings(
    findings: list[WorkbenchFinding],
    *,
    current: TaskStatus,
) -> TaskStatus:
    if not findings or current in {"paused", "cancelled", "failed"}:
        return current
    stages = {finding.stage for finding in findings}
    if "pr_created" in stages:
        return "completed"
    if "waiting_approval" in stages:
        return "waiting_approval"
    return "ready"


__all__ = [
    "ALLOWED_FINDING_STAGES",
    "ALLOWED_TASK_STAGES",
    "ApproveWorkbenchFinding",
    "ApprovalRecord",
    "CoverageRequest",
    "CreateWorkbenchTask",
    "FixRecord",
    "FindingSource",
    "FindingStage",
    "PublicationRecord",
    "ReproductionPlan",
    "ReproductionRecord",
    "STAGE_LABELS",
    "STAGE_RANK",
    "TaskStatus",
    "TestReceipt",
    "WorkbenchFinding",
    "WorkbenchMessage",
    "WorkbenchMode",
    "advance_finding",
    "derive_stage",
    "new_finding_id",
    "rank_findings",
    "score_finding",
    "task_status_for_findings",
    "validate_task_stage",
]
