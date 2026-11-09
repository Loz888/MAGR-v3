"""A04 independent defect verifier.

The verifier owns the only path from ``investigating`` to ``reproduced``:
it records a baseline, runs the reproduction command inside a bounded sandbox,
classifies the outcome mechanically and attaches a commit/environment-bound
execution receipt. An agent cannot submit its own success claim.
"""

from __future__ import annotations

import platform
from collections.abc import Callable, Sequence
from typing import Annotated, Literal

from pydantic import Field

from mgar_v3.domain import DomainError, Model, digest
from mgar_v3.findings.model import (
    Finding,
    FindingEvidence,
    FindingLifecycle,
)
from mgar_v3.findings.store import FindingStore
from mgar_v3.scanners.sandbox import CommandResult
from mgar_v3.verification.receipts import (
    ExecutionClassification,
    ExecutionReceipt,
    ReceiptJournal,
    execution_receipt,
)

VerificationStatus = Literal[
    "reproduced",
    "not_reproduced",
    "static_confirmed",
    "environment_error",
    "missing_dependency",
    "no_tests",
    "timeout",
    "command_error",
    "signature_mismatch",
    "baseline_unhealthy",
    "denied",
    "recorded_existing",
]

STATIC_EVIDENCE_KINDS = frozenset(
    {"scanner", "static_analysis", "investigation", "manual"}
)
UNHEALTHY_BASELINE = frozenset(
    {"environment_error", "missing_dependency", "timeout", "command_error"}
)

CommandRunner = Callable[[Sequence[str], float], CommandResult]

_REPOSITORY = r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$"
_COMMIT = r"^[a-f0-9]{40}$"
_HASH = r"^[a-f0-9]{64}$"


class VerificationEnvironment(Model):
    """Exactly which commit, toolchain and sandbox the receipts belong to."""

    environment_id: Annotated[str, Field(pattern=_HASH)]
    repository: Annotated[str, Field(pattern=_REPOSITORY)]
    commit: Annotated[str, Field(pattern=_COMMIT)]
    sandbox: Literal["local", "docker"]
    os: Annotated[str, Field(min_length=1, max_length=80)]
    python: Annotated[str, Field(min_length=1, max_length=80)]
    tool_versions: dict[str, str] = Field(default_factory=dict)
    image: str | None = None
    network_enabled: bool = False

    @classmethod
    def create(
        cls,
        *,
        repository: str,
        commit: str,
        sandbox: Literal["local", "docker"],
        python: str,
        tool_versions: dict[str, str] | None = None,
        image: str | None = None,
        network_enabled: bool = False,
        os_name: str | None = None,
    ) -> VerificationEnvironment:
        versions = dict(tool_versions or {})
        resolved_os = os_name or platform.system()
        payload = {
            "repository": repository.lower(),
            "commit": commit,
            "sandbox": sandbox,
            "os": resolved_os,
            "python": python,
            "tool_versions": versions,
            "image": image,
            "network_enabled": network_enabled,
        }
        return cls(
            environment_id=digest(payload),
            repository=repository,
            commit=commit,
            sandbox=sandbox,
            os=resolved_os,
            python=python,
            tool_versions=versions,
            image=image,
            network_enabled=network_enabled,
        )


class BaselineRun(Model):
    """The recorded baseline test state at the verified commit."""

    receipt: ExecutionReceipt
    classification: ExecutionClassification
    environment_healthy: bool
    baseline_dirty: bool


class ReproductionPlan(Model):
    finding_id: Annotated[str, Field(pattern=_HASH)]
    command: Annotated[list[str], Field(min_length=1, max_length=64)]
    timeout: Annotated[float, Field(gt=0, le=1_800)] = 120
    expected_failure_signature: Annotated[str, Field(max_length=400)] | None = None
    rationale: Annotated[str, Field(max_length=1_000)] = ""
    static_only: bool = False


class VerificationOutcome(Model):
    finding_id: Annotated[str, Field(pattern=_HASH)]
    status: VerificationStatus
    lifecycle_status: FindingLifecycle
    reason: str = ""
    receipt_id: Annotated[str, Field(pattern=_HASH)] | None = None
    environment_id: Annotated[str, Field(pattern=_HASH)]
    commit: Annotated[str, Field(pattern=_COMMIT)]
    reproduce_command: str = ""
    classification: ExecutionClassification | None = None


class VerificationSummary(Model):
    repository: str
    commit: str
    environment_id: str
    baseline_receipt_id: str | None = None
    baseline_classification: ExecutionClassification | None = None
    findings_total: int = 0
    reproduced: int = 0
    not_reproduced: int = 0
    unverified: int = 0
    zero_defects: bool = False
    complete: bool = False


class DefectVerifier:
    """Mechanical verification; every claim is backed by a verifier-minted receipt."""

    def __init__(
        self,
        store: FindingStore,
        *,
        environment: VerificationEnvironment,
        runner: CommandRunner,
        journal: ReceiptJournal,
        baseline_command: Sequence[str],
        baseline_timeout: float = 120,
    ) -> None:
        self.store = store
        self.environment = environment
        self.runner = runner
        self.journal = journal
        self.baseline_command = [str(item) for item in baseline_command]
        self.baseline_timeout = baseline_timeout
        self._baseline: BaselineRun | None = None

    def record_baseline(self) -> BaselineRun:
        result = self._run(self.baseline_command, self.baseline_timeout)
        receipt = self._mint("baseline", self.baseline_command, result)
        classification = receipt.classification
        baseline = BaselineRun(
            receipt=receipt,
            classification=classification,
            environment_healthy=classification not in UNHEALTHY_BASELINE,
            baseline_dirty=classification == "test_failure",
        )
        self._baseline = baseline
        return baseline

    def baseline(self) -> BaselineRun:
        return self._baseline or self.record_baseline()

    def verify(self, plan: ReproductionPlan) -> VerificationOutcome:
        finding = self.store.get(plan.finding_id)
        if finding.classification == "risk" or plan.static_only:
            return self._outcome(
                finding,
                "denied",
                "STATIC_ONLY_CONFIRMATION_REQUIRED",
            )
        baseline = self.baseline()
        if not baseline.environment_healthy:
            return self._outcome(
                finding,
                "baseline_unhealthy",
                f"baseline was {baseline.classification}",
                receipt=baseline.receipt,
            )

        if finding.lifecycle_status == "suspected":
            finding = self.store.transition(finding.finding_id, "investigating")

        try:
            result = self._run(plan.command, plan.timeout)
        except DomainError as error:
            return self._outcome(finding, "denied", error.code)
        receipt = self._mint(finding.finding_id, plan.command, result)
        classification = receipt.classification

        if classification == "test_failure" and plan.expected_failure_signature:
            if plan.expected_failure_signature not in receipt.output_excerpt:
                return self._outcome(
                    finding,
                    "signature_mismatch",
                    "the failure did not match the expected signature",
                    receipt=receipt,
                )

        if classification == "test_failure":
            self.store.transition(
                finding.finding_id,
                "reproduced",
                evidence=[self._test_run_evidence(finding, receipt, "reproduced")],
            )
            return self._outcome(finding, "reproduced", receipt=receipt)
        if classification == "passed":
            self.store.transition(
                finding.finding_id,
                "not_reproduced",
                evidence=[
                    self._test_run_evidence(
                        finding, receipt, "the reproduction command passed"
                    )
                ],
            )
            return self._outcome(finding, "not_reproduced", receipt=receipt)
        return self._outcome(finding, classification, receipt=receipt)

    def verify_static(
        self,
        finding_id: str,
        *,
        evidence: FindingEvidence,
        rationale: str,
    ) -> VerificationOutcome:
        """Static risks are confirmed statically and can never claim reproduction."""

        finding = self.store.get(finding_id)
        if evidence.kind not in STATIC_EVIDENCE_KINDS:
            raise DomainError(
                "STATIC_EVIDENCE_REQUIRED",
                "static confirmation needs scanner or static-analysis evidence",
                409,
            )
        if not rationale.strip():
            raise DomainError(
                "STATIC_RATIONALE_REQUIRED",
                "static confirmation needs an explicit rationale",
                409,
            )
        if finding.lifecycle_status == "suspected":
            finding = self.store.transition(finding_id, "investigating")
        self.store.transition(finding_id, "confirmed_static", evidence=[evidence])
        return self._outcome(finding, "static_confirmed", rationale)

    def submit_receipt(self, receipt: ExecutionReceipt) -> VerificationOutcome:
        """Reject agent-authored receipts; only verifier-minted records count."""

        finding = self.store.get(receipt.finding_id)
        if (
            receipt.commit != self.environment.commit
            or receipt.environment_id != self.environment.environment_id
        ):
            return self._outcome(finding, "denied", "RECEIPT_BINDING_MISMATCH")
        if not self.journal.owns(receipt.receipt_id):
            return self._outcome(finding, "denied", "AGENT_SELF_VERIFICATION_DENIED")
        return self._outcome(
            finding, "recorded_existing", "receipt was minted by this verifier", receipt
        )

    def summary(self) -> VerificationSummary:
        baseline = self._baseline
        findings = self.store.findings()
        reproduced = sum(
            1 for finding in findings if finding.reproduction_status == "reproduced"
        )
        not_reproduced = sum(
            1 for finding in findings if finding.reproduction_status == "not_reproduced"
        )
        unverified = sum(
            1
            for finding in findings
            if finding.lifecycle_status in {"suspected", "investigating"}
        )
        baseline_classification = None if baseline is None else baseline.classification
        zero_defects = (
            not findings
            and baseline is not None
            and baseline.classification in {"passed", "no_tests"}
        )
        complete = (
            baseline is not None
            and baseline.environment_healthy
            and unverified == 0
        )
        return VerificationSummary(
            repository=self.environment.repository,
            commit=self.environment.commit,
            environment_id=self.environment.environment_id,
            baseline_receipt_id=None if baseline is None else baseline.receipt.receipt_id,
            baseline_classification=baseline_classification,
            findings_total=len(findings),
            reproduced=reproduced,
            not_reproduced=not_reproduced,
            unverified=unverified,
            zero_defects=zero_defects,
            complete=complete,
        )

    def _run(self, command: Sequence[str], timeout: float) -> CommandResult:
        return self.runner([str(item) for item in command], timeout)

    def _mint(
        self, finding_id: str, command: Sequence[str], result: CommandResult
    ) -> ExecutionReceipt:
        receipt = execution_receipt(
            finding_id=finding_id,
            command=command,
            result=result,
            commit=self.environment.commit,
            environment_id=self.environment.environment_id,
            sandbox=self.environment.sandbox,
        )
        self.journal.append(receipt)
        return receipt

    def _test_run_evidence(
        self, finding: Finding, receipt: ExecutionReceipt, description: str
    ) -> FindingEvidence:
        return FindingEvidence(
            evidence_id=digest({"kind": "test_run", "receipt": receipt.receipt_id}),
            kind="test_run",
            source_uri=f"receipt://{receipt.receipt_id}",
            description=f"{description}: {receipt.reproduce_command}"[:1_000],
            content_hash=receipt.receipt_id,
            location=finding.source_locations[0] if finding.source_locations else None,
        )

    def _outcome(
        self,
        finding: Finding,
        status: VerificationStatus,
        reason: str = "",
        receipt: ExecutionReceipt | None = None,
    ) -> VerificationOutcome:
        current = self.store.get(finding.finding_id)
        return VerificationOutcome(
            finding_id=current.finding_id,
            status=status,
            lifecycle_status=current.lifecycle_status,
            reason=reason,
            receipt_id=None if receipt is None else receipt.receipt_id,
            environment_id=self.environment.environment_id,
            commit=self.environment.commit,
            reproduce_command="" if receipt is None else receipt.reproduce_command,
            classification=None if receipt is None else receipt.classification,
        )
