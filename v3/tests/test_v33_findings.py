"""V3.3 A03 finding store, dedupe and defect discovery tests."""

from __future__ import annotations

import pytest

from mgar_v3.domain import DomainError, digest
from mgar_v3.findings.discovery import (
    FINDING_VERIFICATION_REQUIRED,
    InvestigationProposal,
    candidates_from_scan_report,
    classify_finding,
    coverage_from_scan_report,
    discover_scan_report,
    propose,
)
from mgar_v3.findings.model import (
    FINDING_SCHEMA,
    LIFECYCLE_STATUSES,
    Finding,
    FindingCandidate,
    FindingEvidence,
    FindingReport,
    dedupe_key_for,
)
from mgar_v3.findings.store import FindingStore
from mgar_v3.scanners.domain import (
    NO_TESTS,
    RACE_CHECK_DISABLED,
    ScanFinding,
    ScanLocation,
    ScannerCoverage,
    ScanReport,
    ScanResult,
)

RUN_ID = "3d8f6c2a-7b41-4c8e-9da2-5046d8b3a1f7"
REPOSITORY = "octo/fixture"
COMMIT = "a" * 40


def _location(path: str, line: int = 7, column: int = 2) -> ScanLocation:
    return ScanLocation(path=path, line=line, column=column)


def _evidence(
    *,
    kind: str = "scanner",
    evidence_id: str | None = None,
    description: str = "evidence",
    content_hash: str = "a" * 64,
    location: ScanLocation | None = None,
) -> FindingEvidence:
    return FindingEvidence(
        evidence_id=evidence_id or digest({"description": description}),
        kind=kind,
        source_uri="scan://fixture/path#L7",
        description=description,
        content_hash=content_hash,
        location=location,
    )


def _candidate(
    *,
    rule_id: str = "go-vet",
    path: str = "main.go",
    line: int = 7,
    root_cause: str = "",
    classification: str = "defect",
    severity: str = "warning",
    fix_risk: str = "medium",
    summary: str = "fmt.Printf format mismatch",
    evidence: list[FindingEvidence] | None = None,
    origin: str = "scanner",
    scanner_ids: list[str] | None = None,
) -> FindingCandidate:
    return FindingCandidate(
        category="vet",
        classification=classification,
        severity=severity,
        confidence=1.0,
        summary=summary,
        source_locations=[_location(path, line)],
        rule_id=rule_id,
        evidence=[] if evidence is None else evidence,
        root_cause=root_cause,
        fix_risk=fix_risk,
        origin=origin,
        scanner_ids=["go"] if scanner_ids is None else scanner_ids,
    )


def _scan_finding(
    *,
    scanner_id: str = "go",
    rule_id: str = "go-vet",
    category: str = "vet",
    severity: str = "warning",
    message: str = "fmt.Printf format mismatch",
    path: str = "main.go",
    line: int = 7,
) -> ScanFinding:
    return ScanFinding(
        scanner_id=scanner_id,
        rule_id=rule_id,
        category=category,
        severity=severity,
        message=message,
        location=ScanLocation(path=path, line=line, column=2),
    )


def _result(
    scanner_id: str,
    *,
    language: str = "Go",
    status: str = "completed",
    findings: list[ScanFinding] | None = None,
    required: bool = True,
    reason: str | None = None,
    considered: int = 3,
    scanned: int = 3,
) -> ScanResult:
    return ScanResult(
        scanner_id=scanner_id,
        version="1",
        language=language,
        status=status,
        required=required,
        findings=[] if findings is None else findings,
        reason=reason,
        coverage=ScannerCoverage(files_considered=considered, files_scanned=scanned),
    )


def _report(
    results: list[ScanResult],
    *,
    considered: int = 3,
    scanned: int = 3,
    unsupported: list[str] | None = None,
    complete: bool = True,
) -> ScanReport:
    return ScanReport(
        repository=REPOSITORY,
        commit=COMMIT,
        results=results,
        unsupported_languages=[] if unsupported is None else unsupported,
        complete=complete,
        coverage=ScannerCoverage(files_considered=considered, files_scanned=scanned),
    )


def test_finding_contract_carries_every_required_field_and_lifecycle() -> None:
    required = {
        "finding_id",
        "run_id",
        "repository",
        "commit",
        "category",
        "severity",
        "confidence",
        "source_locations",
        "evidence_ids",
        "reproduction_status",
        "suggested_fix",
        "lifecycle_status",
    }
    assert FINDING_SCHEMA == "v33-finding-v1"
    assert required <= set(Finding.model_fields)
    assert set(LIFECYCLE_STATUSES) == {
        "suspected",
        "investigating",
        "reproduced",
        "confirmed_static",
        "not_reproduced",
        "fix_proposed",
        "patch_verified",
        "awaiting_approval",
    }


def test_known_defects_produce_independent_findings() -> None:
    report = _report(
        [
            _result(
                "go",
                findings=[
                    _scan_finding(path="main.go", line=7, rule_id="go-vet"),
                    _scan_finding(
                        path="main_test.go",
                        line=12,
                        rule_id="go-test",
                        category="test_failure",
                        severity="error",
                        message="TestAdd failed: expected 3, got 2",
                    ),
                ],
            )
        ]
    )
    store, coverage = discover_scan_report(RUN_ID, report)
    findings = store.findings()

    assert len(findings) == 2
    assert len({finding.finding_id for finding in findings}) == 2
    by_rule = {finding.rule_ids[0]: finding for finding in findings}
    assert by_rule["go-vet"].classification == "defect"
    assert by_rule["go-test"].classification == "defect"
    assert by_rule["go-test"].severity == "error"
    for finding in findings:
        assert finding.evidence_ids
        assert finding.uncertainty.strip()
        assert finding.lifecycle_status == "suspected"
        assert finding.reproduction_status == "not_attempted"
    assert coverage.complete is True


def test_duplicate_scanner_warning_is_one_finding_not_multiple_bugs() -> None:
    store = FindingStore(run_id=RUN_ID, repository=REPOSITORY, commit=COMMIT)
    store.add_candidate(
        _candidate(line=7, evidence=[_evidence(description="first observation")])
    )
    store.add_candidate(
        _candidate(line=9, evidence=[_evidence(description="second observation")])
    )

    findings = store.findings()
    assert len(findings) == 1
    merged = findings[0]
    assert len(merged.source_locations) == 2
    assert len(merged.evidence_ids) == 2
    assert merged.rule_ids == ["go-vet"]
    assert store.report().duplicates_merged == 1
    assert store.report().candidates_received == 2


def test_dedupe_uses_path_rule_and_root_cause_clue() -> None:
    assert dedupe_key_for("./main.go", "GO-VET", "  Nil Map   Write ") == (
        "main.go|go-vet|nil map write"
    )

    store = FindingStore(run_id=RUN_ID, repository=REPOSITORY, commit=COMMIT)
    store.add_candidate(_candidate(root_cause="nil map write"))
    store.add_candidate(_candidate(root_cause="unchecked error", line=8))
    store.add_candidate(_candidate(path="other.go", root_cause="nil map write"))

    assert len(store.findings()) == 3
    assert store.report().duplicates_merged == 0


def test_real_defects_quality_advice_and_supply_chain_risk_are_distinct() -> None:
    assert classify_finding("test_failure", "error") == "defect"
    assert classify_finding("lint", "warning") == "quality"
    assert classify_finding("dependency", "error") == "risk"

    report = _report(
        [
            _result(
                "python-ruff",
                language="Python",
                findings=[
                    _scan_finding(
                        scanner_id="python-ruff",
                        rule_id="F401",
                        category="lint",
                        severity="warning",
                        message="imported but unused",
                        path="app.py",
                        line=3,
                    )
                ],
            ),
            _result(
                "python-pytest",
                language="Python",
                findings=[
                    _scan_finding(
                        scanner_id="python-pytest",
                        rule_id="python-pytest",
                        category="test_failure",
                        severity="error",
                        message="test_add failed",
                        path="tests/test_app.py",
                        line=9,
                    )
                ],
            ),
            _result(
                "dependency",
                language="repository",
                findings=[
                    _scan_finding(
                        scanner_id="dependency",
                        rule_id="CI_PIPE_SHELL",
                        category="dependency",
                        severity="error",
                        message="remote shell pipe",
                        path=".github/workflows/ci.yml",
                        line=7,
                    )
                ],
            ),
        ]
    )
    store, _ = discover_scan_report(RUN_ID, report)
    by_rule = {finding.rule_ids[0]: finding for finding in store.findings()}

    assert {finding.rule_ids[0]: finding.classification for finding in store.findings()} == {
        "F401": "quality",
        "python-pytest": "defect",
        "CI_PIPE_SHELL": "risk",
    }
    assert by_rule["python-pytest"].severity == "error"


def test_model_guess_without_evidence_cannot_become_reproduced() -> None:
    store = FindingStore(run_id=RUN_ID, repository=REPOSITORY, commit=COMMIT)
    proposal = InvestigationProposal(
        category="bug",
        severity="error",
        summary="nil dereference when cache misses",
        source_locations=[_location("service/cache.py", line=42)],
        claimed_status="reproduced",
    )
    outcome = propose(store, proposal)

    assert outcome.accepted is False
    assert outcome.rejection_code == FINDING_VERIFICATION_REQUIRED
    assert outcome.finding.lifecycle_status == "suspected"
    assert outcome.finding.reproduction_status == "not_attempted"

    store.transition(outcome.finding.finding_id, "investigating")
    with pytest.raises(DomainError) as error:
        store.transition(outcome.finding.finding_id, "reproduced")
    assert error.value.code == "FINDING_EVIDENCE_REQUIRED"
    assert store.get(outcome.finding.finding_id).lifecycle_status == "investigating"
    assert store.get(outcome.finding.finding_id).reproduction_status == "attempted"


def test_lifecycle_requires_evidence_and_a_fix_before_advancing() -> None:
    store = FindingStore(run_id=RUN_ID, repository=REPOSITORY, commit=COMMIT)
    finding = store.add_candidate(
        _candidate(evidence=[_evidence(description="vet observation")])
    )

    with pytest.raises(DomainError) as jump:
        store.transition(finding.finding_id, "patch_verified")
    assert jump.value.code == "FINDING_TRANSITION_INVALID"

    store.transition(finding.finding_id, "investigating")
    reproduced = store.transition(
        finding.finding_id,
        "reproduced",
        evidence=[_evidence(kind="test_run", description="go test ./... exit 1")],
    )
    assert reproduced.reproduction_status == "reproduced"

    with pytest.raises(DomainError) as missing_fix:
        store.transition(finding.finding_id, "fix_proposed")
    assert missing_fix.value.code == "FINDING_FIX_REQUIRED"

    fixed = store.transition(
        finding.finding_id, "fix_proposed", suggested_fix="guard the nil map write"
    )
    assert fixed.suggested_fix == "guard the nil map write"
    verified = store.transition(
        finding.finding_id,
        "patch_verified",
        evidence=[_evidence(kind="patch_verification", description="patch test pass")],
    )
    assert verified.reproduction_status == "reproduced"
    approved = store.transition(finding.finding_id, "awaiting_approval")
    assert approved.lifecycle_status == "awaiting_approval"


def test_static_confirmation_and_non_reproduction_need_their_evidence() -> None:
    store = FindingStore(run_id=RUN_ID, repository=REPOSITORY, commit=COMMIT)
    empty = store.add_candidate(_candidate(rule_id="empty"))
    store.transition(empty.finding_id, "investigating")
    with pytest.raises(DomainError) as missing_static:
        store.transition(empty.finding_id, "confirmed_static")
    assert missing_static.value.code == "FINDING_EVIDENCE_REQUIRED"

    static = store.add_candidate(
        _candidate(rule_id="static", evidence=[_evidence(description="static proof")])
    )
    store.transition(static.finding_id, "investigating")
    confirmed = store.transition(static.finding_id, "confirmed_static")
    assert confirmed.reproduction_status == "static_only"

    with pytest.raises(DomainError) as missing_attempt:
        store.transition(empty.finding_id, "not_reproduced")
    assert missing_attempt.value.code == "FINDING_EVIDENCE_REQUIRED"
    not_reproduced = store.transition(
        empty.finding_id,
        "not_reproduced",
        evidence=[_evidence(kind="test_run", description="reproduction attempt failed")],
    )
    assert not_reproduced.reproduction_status == "not_reproduced"


def test_findings_sort_by_severity_reproducibility_and_fix_risk() -> None:
    store = FindingStore(run_id=RUN_ID, repository=REPOSITORY, commit=COMMIT)
    warning = store.add_candidate(
        _candidate(
            rule_id="warning-rule",
            severity="warning",
            fix_risk="medium",
            summary="warning",
            evidence=[_evidence(description="warning")],
        )
    )
    store.add_candidate(
        _candidate(
            rule_id="high-risk",
            severity="error",
            fix_risk="high",
            summary="risky error",
        )
    )
    static = store.add_candidate(
        _candidate(
            rule_id="static-error",
            severity="error",
            fix_risk="medium",
            summary="static error",
            evidence=[_evidence(description="static")],
        )
    )

    store.transition(warning.finding_id, "investigating")
    store.transition(
        warning.finding_id,
        "reproduced",
        evidence=[_evidence(kind="test_run", description="reproduced")],
    )
    store.transition(static.finding_id, "investigating")
    store.transition(static.finding_id, "confirmed_static")

    ordered = [finding.rule_ids[0] for finding in store.findings()]
    assert ordered == ["static-error", "high-risk", "warning-rule"]


def test_review_coverage_marks_every_unscanned_scope() -> None:
    report = _report(
        [
            _result("go"),
            _result(
                "dependency",
                language="repository",
                status="failed",
                reason="SCANNER_FAILED",
            ),
            _result(
                "python-pytest",
                language="Python",
                status="skipped",
                reason=NO_TESTS,
            ),
            _result(
                "go-race",
                status="skipped",
                required=False,
                reason=RACE_CHECK_DISABLED,
            ),
        ],
        considered=10,
        scanned=6,
        unsupported=["Rust"],
        complete=False,
    )
    coverage = coverage_from_scan_report(report, {"Go": 100, "Python": 50, "Rust": 10})

    assert coverage.files_considered == 10
    assert coverage.files_scanned == 6
    assert coverage.unscanned_files == 4
    assert coverage.scanned_languages == ["Go", "Python"]
    assert coverage.unsupported_languages == ["Rust"]
    assert coverage.unscanned_scopes == [
        "files:4",
        "language:Rust",
        "scanner:dependency:failed:SCANNER_FAILED",
        "scanner:go-race:skipped:RACE_CHECK_DISABLED",
    ]
    assert coverage.complete is False
    assert coverage.scanner_statuses["python-pytest"] == "skipped"

    clean = coverage_from_scan_report(
        _report([_result("go"), _result("python-ruff", language="Python")])
    )
    assert clean.complete is True
    assert clean.unscanned_scopes == []


def test_report_round_trips_and_repeats_byte_for_byte() -> None:
    report = _report(
        [
            _result(
                "go",
                findings=[
                    _scan_finding(path="main.go", line=7, rule_id="go-vet"),
                    _scan_finding(
                        path="main_test.go",
                        line=12,
                        rule_id="go-test",
                        category="test_failure",
                        severity="error",
                    ),
                ],
            )
        ]
    )
    first_store, first_coverage = discover_scan_report(RUN_ID, report)
    second_store, second_coverage = discover_scan_report(RUN_ID, report)
    first = first_store.report(first_coverage)
    second = second_store.report(second_coverage)

    assert first.model_dump_json() == second.model_dump_json()
    assert [item.finding_id for item in first.findings] == [
        item.finding_id for item in second.findings
    ]
    assert FindingReport.model_validate_json(first.model_dump_json()) == first
    assert len(candidates_from_scan_report(report)) == 2


def test_invalid_identity_and_evidence_reuse_fail_closed() -> None:
    with pytest.raises(DomainError) as identity:
        FindingStore(run_id="bad id", repository=REPOSITORY, commit=COMMIT)
    assert identity.value.code == "FINDING_IDENTITY_INVALID"

    store = FindingStore(run_id=RUN_ID, repository=REPOSITORY, commit=COMMIT)
    finding = store.add_candidate(
        _candidate(evidence=[_evidence(description="original", evidence_id="e" * 64)])
    )
    with pytest.raises(DomainError) as conflict:
        store.transition(
            finding.finding_id,
            "investigating",
            evidence=[
                _evidence(
                    description="different content",
                    evidence_id="e" * 64,
                    content_hash="b" * 64,
                )
            ],
        )
    assert conflict.value.code == "FINDING_EVIDENCE_CONFLICT"
    assert store.get(finding.finding_id).lifecycle_status == "suspected"
