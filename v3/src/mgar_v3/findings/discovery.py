"""A03 discovery: turn scanner output and model proposals into candidates."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated

from pydantic import Field

from mgar_v3.domain import DomainError, Model, digest
from mgar_v3.findings.model import (
    Finding,
    FindingCandidate,
    FindingClassification,
    FindingEvidence,
    FindingLifecycle,
    FindingSeverity,
    FixRisk,
    ReviewCoverage,
)
from mgar_v3.findings.store import FindingStore
from mgar_v3.scanners.domain import (
    NO_TESTS,
    SUPPORTED_LANGUAGES,
    ScanFinding,
    ScanLocation,
    ScanReport,
    scan_languages,
)

FINDING_VERIFICATION_REQUIRED = "FINDING_VERIFICATION_REQUIRED"

_DEFECT_CATEGORIES = frozenset(
    {"bug", "compile_error", "panic", "race", "security", "test_failure", "vet"}
)
_RISK_CATEGORIES = frozenset({"ci", "configuration", "dependency", "supply_chain"})


def classify_finding(category: str, severity: FindingSeverity) -> FindingClassification:
    """Separate real defects from quality advice and repository-level risk."""

    name = category.strip().lower()
    if name in _RISK_CATEGORIES:
        return "risk"
    if name in _DEFECT_CATEGORIES:
        return "defect"
    if severity == "error":
        return "defect"
    return "quality"


def _evidence_for(scanner_id: str, version: str, finding: ScanFinding) -> FindingEvidence:
    location = finding.location or ScanLocation(path="repository")
    line_suffix = f"#L{location.line}" if location.line is not None else ""
    source_uri = f"scan://{scanner_id}@{version}/{location.path}{line_suffix}"
    content_hash = digest(
        {
            "scanner_id": scanner_id,
            "scanner_version": version,
            "rule_id": finding.rule_id,
            "category": finding.category,
            "message": finding.message,
            "path": location.path,
            "line": location.line,
            "column": location.column,
            "end_line": location.end_line,
        }
    )
    evidence_id = digest({"kind": "scanner", "content_hash": content_hash})
    return FindingEvidence(
        evidence_id=evidence_id,
        kind="scanner",
        source_uri=source_uri[:400],
        description=finding.message,
        content_hash=content_hash,
        location=location,
    )


def candidate_from_scan_finding(
    scanner_id: str, version: str, finding: ScanFinding
) -> FindingCandidate:
    location = finding.location or ScanLocation(path="repository")
    return FindingCandidate(
        category=finding.category,
        classification=classify_finding(finding.category, finding.severity),
        severity=finding.severity,
        confidence=finding.confidence,
        summary=finding.message,
        source_locations=[location],
        rule_id=finding.rule_id,
        evidence=[_evidence_for(scanner_id, version, finding)],
        uncertainty=(
            f"{scanner_id} reports a static observation; root cause and runtime "
            "impact are not independently verified."
        ),
        origin="scanner",
        scanner_ids=[scanner_id],
    )


def candidates_from_scan_report(report: ScanReport) -> list[FindingCandidate]:
    """Only completed scanners can contribute candidates; skips stay explicit."""

    candidates: list[FindingCandidate] = []
    for result in report.results:
        if result.status != "completed":
            continue
        for finding in result.findings:
            candidates.append(
                candidate_from_scan_finding(result.scanner_id, result.version, finding)
            )
    return candidates


def coverage_from_scan_report(
    report: ScanReport, languages: Mapping[str, int] | None = None
) -> ReviewCoverage:
    """Record the denominator and every scope the review did not scan."""

    considered = report.coverage.files_considered
    scanned = report.coverage.files_scanned
    unscanned = max(0, considered - scanned)
    if languages is not None:
        supported, _ = scan_languages(languages)
        scanned_languages = supported
    else:
        scanned_languages = sorted(
            {
                result.language
                for result in report.results
                if result.status == "completed" and result.language in SUPPORTED_LANGUAGES
            }
        )

    scanner_statuses = {
        result.scanner_id: result.status
        for result in sorted(report.results, key=lambda item: item.scanner_id)
    }
    scopes = [f"language:{language}" for language in sorted(report.unsupported_languages)]
    if unscanned:
        scopes.append(f"files:{unscanned}")
    for result in sorted(report.results, key=lambda item: item.scanner_id):
        if result.status == "completed" or result.reason == NO_TESTS:
            continue
        scope = f"scanner:{result.scanner_id}:{result.status}"
        if result.reason:
            scope += f":{result.reason}"
        scopes.append(scope)

    return ReviewCoverage(
        files_considered=considered,
        files_scanned=scanned,
        unscanned_files=unscanned,
        scanned_languages=scanned_languages,
        unsupported_languages=sorted(report.unsupported_languages),
        scanner_statuses=scanner_statuses,
        unscanned_scopes=sorted(scopes),
        complete=(
            report.complete
            and unscanned == 0
            and not report.unsupported_languages
        ),
    )


def discover_scan_report(
    run_id: str, report: ScanReport, languages: Mapping[str, int] | None = None
) -> tuple[FindingStore, ReviewCoverage]:
    store = FindingStore(run_id=run_id, repository=report.repository, commit=report.commit)
    for candidate in candidates_from_scan_report(report):
        store.add_candidate(candidate)
    return store, coverage_from_scan_report(report, languages)


class InvestigationProposal(Model):
    """A model-proposed defect candidate; it may not verify itself."""

    category: Annotated[str, Field(min_length=1, max_length=60)]
    severity: FindingSeverity
    confidence: Annotated[float, Field(ge=0, le=1)] = 0.5
    summary: Annotated[str, Field(min_length=1, max_length=2_000)]
    source_locations: Annotated[list[ScanLocation], Field(min_length=1, max_length=32)]
    rule_id: Annotated[str, Field(min_length=1, max_length=200)] = "investigation"
    evidence: Annotated[list[FindingEvidence], Field(max_length=32)] = Field(
        default_factory=list
    )
    uncertainty: Annotated[str, Field(max_length=1_000)] = ""
    suggested_fix: Annotated[str, Field(max_length=4_000)] = ""
    root_cause: Annotated[str, Field(max_length=300)] = ""
    fix_risk: FixRisk = "medium"
    claimed_status: FindingLifecycle | None = None


class ProposalOutcome(Model):
    finding: Finding
    claimed_status: FindingLifecycle
    accepted: bool
    rejection_code: str | None = None


def propose(store: FindingStore, proposal: InvestigationProposal) -> ProposalOutcome:
    """Store a model proposal as ``suspected``; only investigation may self-advance.

    Any stronger claim is rejected because A03 has no independent verifier: a
    model statement alone can never make a finding ``reproduced``.
    """

    candidate = FindingCandidate(
        category=proposal.category,
        classification=classify_finding(proposal.category, proposal.severity),
        severity=proposal.severity,
        confidence=proposal.confidence,
        summary=proposal.summary,
        source_locations=list(proposal.source_locations),
        rule_id=proposal.rule_id,
        evidence=list(proposal.evidence),
        uncertainty=proposal.uncertainty
        or "Model-proposed candidate; no independent verification yet.",
        suggested_fix=proposal.suggested_fix,
        root_cause=proposal.root_cause,
        fix_risk=proposal.fix_risk,
        origin="investigation",
    )
    finding = store.add_candidate(candidate)
    claimed = proposal.claimed_status or "suspected"
    if claimed == "suspected":
        return ProposalOutcome(finding=finding, claimed_status=claimed, accepted=True)
    if claimed == "investigating":
        try:
            updated = store.transition(finding.finding_id, "investigating")
        except DomainError as error:
            return ProposalOutcome(
                finding=store.get(finding.finding_id),
                claimed_status=claimed,
                accepted=False,
                rejection_code=error.code,
            )
        return ProposalOutcome(finding=updated, claimed_status=claimed, accepted=True)
    return ProposalOutcome(
        finding=finding,
        claimed_status=claimed,
        accepted=False,
        rejection_code=FINDING_VERIFICATION_REQUIRED,
    )


def scan_location(path: str, line: int | None = None) -> ScanLocation:
    """Small helper shared by callers that only know a path and line."""

    return ScanLocation(path=path, line=line)


__all__ = [
    "FINDING_VERIFICATION_REQUIRED",
    "InvestigationProposal",
    "ProposalOutcome",
    "candidate_from_scan_finding",
    "candidates_from_scan_report",
    "classify_finding",
    "coverage_from_scan_report",
    "discover_scan_report",
    "propose",
    "scan_location",
]
