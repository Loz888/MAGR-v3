"""Finding Store: dedupe, evidence registry and enforced lifecycle gates (A03)."""

from __future__ import annotations

import re
from collections.abc import Sequence

from mgar_v3.domain import DomainError
from mgar_v3.findings.model import (
    CLASSIFICATION_ORDER,
    FIX_RISK_ORDER,
    SEVERITY_ORDER,
    EvidenceKind,
    Finding,
    FindingCandidate,
    FindingClassification,
    FindingEvidence,
    FindingLifecycle,
    FindingReport,
    FindingSeverity,
    FixRisk,
    ReviewCoverage,
    dedupe_key_for,
    finding_id_for,
    finding_sort_key,
)
from mgar_v3.scanners.domain import ScanLocation

_ALLOWED: dict[FindingLifecycle, frozenset[FindingLifecycle]] = {
    "suspected": frozenset({"investigating"}),
    "investigating": frozenset({"reproduced", "confirmed_static", "not_reproduced"}),
    "reproduced": frozenset({"fix_proposed"}),
    "confirmed_static": frozenset({"fix_proposed"}),
    "not_reproduced": frozenset({"fix_proposed"}),
    "fix_proposed": frozenset({"patch_verified"}),
    "patch_verified": frozenset({"awaiting_approval"}),
    "awaiting_approval": frozenset(),
}

_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_COMMIT = re.compile(r"^[a-f0-9]{40}$")

_DEFAULT_UNCERTAINTY = {
    "scanner": "Scanner output is a static observation; runtime impact is not verified.",
    "investigation": "Model-proposed candidate; no independent verification yet.",
    "manual": "Manually reported candidate; no independent verification yet.",
}


class FindingStore:
    """Per-run store that never counts one observation as several bugs."""

    def __init__(self, *, run_id: str, repository: str, commit: str) -> None:
        if not _IDENTITY.fullmatch(run_id):
            raise DomainError("FINDING_IDENTITY_INVALID", "invalid run_id", 400)
        if not _REPOSITORY.fullmatch(repository):
            raise DomainError("FINDING_IDENTITY_INVALID", "invalid repository", 400)
        if not _COMMIT.fullmatch(commit):
            raise DomainError("FINDING_IDENTITY_INVALID", "invalid commit", 400)
        self.run_id = run_id
        self.repository = repository.lower()
        self.commit = commit
        self._findings: dict[str, Finding] = {}
        self._by_dedupe_key: dict[str, str] = {}
        self._evidence: dict[str, FindingEvidence] = {}
        self._candidates_received = 0
        self._duplicates_merged = 0

    def add_candidate(self, candidate: FindingCandidate) -> Finding:
        """Register a candidate, merging it into an existing dedupe bucket."""

        self._candidates_received += 1
        dedupe_key = dedupe_key_for(
            candidate.source_locations[0].path, candidate.rule_id, candidate.root_cause
        )
        existing_id = self._by_dedupe_key.get(dedupe_key)
        if existing_id is not None:
            existing = self._findings[existing_id]
            merged = self._merge(existing, candidate, dedupe_key)
            self._findings[existing_id] = merged
            self._duplicates_merged += 1
            return merged

        evidence_ids = self._register(candidate.evidence)
        finding = Finding(
            finding_id=finding_id_for(
                self.run_id, self.repository, self.commit, dedupe_key
            ),
            run_id=self.run_id,
            repository=self.repository,
            commit=self.commit,
            category=candidate.category,
            classification=candidate.classification,
            severity=candidate.severity,
            confidence=candidate.confidence,
            summary=candidate.summary,
            source_locations=list(candidate.source_locations),
            evidence_ids=evidence_ids,
            reproduction_status="not_attempted",
            suggested_fix=candidate.suggested_fix,
            lifecycle_status="suspected",
            uncertainty=candidate.uncertainty
            or _DEFAULT_UNCERTAINTY[candidate.origin],
            root_cause=candidate.root_cause,
            fix_risk=candidate.fix_risk,
            origin=candidate.origin,
            dedupe_key=dedupe_key,
            rule_ids=[candidate.rule_id],
            scanner_ids=list(candidate.scanner_ids),
        )
        self._findings[finding.finding_id] = finding
        self._by_dedupe_key[dedupe_key] = finding.finding_id
        return finding

    def transition(
        self,
        finding_id: str,
        status: FindingLifecycle,
        *,
        evidence: Sequence[FindingEvidence] = (),
        suggested_fix: str | None = None,
        fix_risk: FixRisk | None = None,
        confidence: float | None = None,
    ) -> Finding:
        """Advance one Finding only when its evidence supports the claim."""

        current = self.get(finding_id)
        if status not in _ALLOWED[current.lifecycle_status]:
            raise DomainError(
                "FINDING_TRANSITION_INVALID",
                f"{current.lifecycle_status} cannot transition to {status}",
                409,
            )

        kinds = self._kinds_for(current, evidence)
        if status == "reproduced" and "test_run" not in kinds:
            raise DomainError(
                "FINDING_EVIDENCE_REQUIRED",
                "reproduced requires a test_run evidence record",
                409,
            )
        if status == "confirmed_static" and not (current.evidence_ids or evidence):
            raise DomainError(
                "FINDING_EVIDENCE_REQUIRED",
                "confirmed_static requires static evidence",
                409,
            )
        if status == "not_reproduced" and "test_run" not in kinds:
            raise DomainError(
                "FINDING_EVIDENCE_REQUIRED",
                "not_reproduced requires an attempted test_run record",
                409,
            )
        if status == "patch_verified" and not kinds.intersection(
            {"test_run", "patch_verification"}
        ):
            raise DomainError(
                "FINDING_EVIDENCE_REQUIRED",
                "patch_verified requires test_run or patch_verification evidence",
                409,
            )
        if status == "awaiting_approval" and not current.evidence_ids:
            raise DomainError(
                "FINDING_EVIDENCE_REQUIRED",
                "awaiting_approval requires retained verification evidence",
                409,
            )

        suggested = current.suggested_fix if suggested_fix is None else suggested_fix
        if status == "fix_proposed" and not suggested.strip():
            raise DomainError(
                "FINDING_FIX_REQUIRED",
                "fix_proposed requires a non-empty suggested_fix",
                409,
            )

        new_ids = self._register(evidence)
        evidence_ids = list(dict.fromkeys([*current.evidence_ids, *new_ids]))
        reproduction: str = current.reproduction_status
        if status == "reproduced":
            reproduction = "reproduced"
        elif status == "confirmed_static":
            reproduction = "static_only"
        elif status == "not_reproduced":
            reproduction = "not_reproduced"
        elif status == "investigating" and reproduction == "not_attempted":
            reproduction = "attempted"

        updated = current.model_copy(
            update={
                "lifecycle_status": status,
                "reproduction_status": reproduction,
                "evidence_ids": evidence_ids,
                "suggested_fix": suggested,
                "fix_risk": current.fix_risk if fix_risk is None else fix_risk,
                "confidence": current.confidence if confidence is None else confidence,
            }
        )
        self._findings[finding_id] = updated
        return updated

    def get(self, finding_id: str) -> Finding:
        finding = self._findings.get(finding_id)
        if finding is None:
            raise DomainError("FINDING_NOT_FOUND", f"unknown finding {finding_id}", 404)
        return finding

    def findings(self) -> list[Finding]:
        return sorted(self._findings.values(), key=finding_sort_key)

    def evidence(self) -> list[FindingEvidence]:
        return sorted(self._evidence.values(), key=lambda item: item.evidence_id)

    def report(self, coverage: ReviewCoverage | None = None) -> FindingReport:
        return FindingReport(
            run_id=self.run_id,
            repository=self.repository,
            commit=self.commit,
            findings=self.findings(),
            evidence=self.evidence(),
            coverage=coverage or ReviewCoverage(),
            candidates_received=self._candidates_received,
            duplicates_merged=self._duplicates_merged,
        )

    def _register(self, evidence: Sequence[FindingEvidence]) -> list[str]:
        ids: list[str] = []
        for item in evidence:
            prior = self._evidence.get(item.evidence_id)
            if prior is None:
                self._evidence[item.evidence_id] = item
            elif prior != item:
                raise DomainError(
                    "FINDING_EVIDENCE_CONFLICT",
                    f"evidence {item.evidence_id} was reused with different content",
                    409,
                )
            if item.evidence_id not in ids:
                ids.append(item.evidence_id)
        return ids

    def _kinds_for(
        self, current: Finding, incoming: Sequence[FindingEvidence]
    ) -> set[EvidenceKind]:
        kinds = {
            self._evidence[evidence_id].kind
            for evidence_id in current.evidence_ids
            if evidence_id in self._evidence
        }
        kinds.update(item.kind for item in incoming)
        return kinds

    def _merge(
        self, existing: Finding, candidate: FindingCandidate, dedupe_key: str
    ) -> Finding:
        new_evidence_ids = self._register(candidate.evidence)
        locations = _unique_locations(
            [*existing.source_locations, *candidate.source_locations]
        )
        rule_ids = list(dict.fromkeys([*existing.rule_ids, candidate.rule_id]))
        scanner_ids = list(dict.fromkeys([*existing.scanner_ids, *candidate.scanner_ids]))
        return existing.model_copy(
            update={
                "severity": _max_severity(existing.severity, candidate.severity),
                "classification": _max_classification(
                    existing.classification, candidate.classification
                ),
                "fix_risk": _max_risk(existing.fix_risk, candidate.fix_risk),
                "confidence": max(existing.confidence, candidate.confidence),
                "source_locations": locations,
                "evidence_ids": list(
                    dict.fromkeys([*existing.evidence_ids, *new_evidence_ids])
                ),
                "suggested_fix": existing.suggested_fix or candidate.suggested_fix,
                "root_cause": existing.root_cause or candidate.root_cause,
                "rule_ids": rule_ids,
                "scanner_ids": scanner_ids,
                "dedupe_key": dedupe_key,
            }
        )


def _unique_locations(locations: Sequence[ScanLocation]) -> list[ScanLocation]:
    seen: dict[tuple[str, int | None, int | None, int | None], ScanLocation] = {}
    for location in locations:
        key = (location.path, location.line, location.column, location.end_line)
        if key not in seen:
            seen[key] = location
    return list(seen.values())


def _max_severity(first: FindingSeverity, second: FindingSeverity) -> FindingSeverity:
    return first if SEVERITY_ORDER[first] >= SEVERITY_ORDER[second] else second


def _max_classification(
    first: FindingClassification, second: FindingClassification
) -> FindingClassification:
    return (
        first
        if CLASSIFICATION_ORDER[first] >= CLASSIFICATION_ORDER[second]
        else second
    )


def _max_risk(first: FixRisk, second: FixRisk) -> FixRisk:
    return first if FIX_RISK_ORDER[first] >= FIX_RISK_ORDER[second] else second

