"""V3.3 A03 finding contracts: traceable candidate defects.

A scanner warning or a model-proposed suspicion is not a bug yet. Findings keep
their evidence, uncertainty and reproduction state explicit so the same
observation cannot be counted twice and an unsupported claim can never advance
to ``reproduced``.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, field_validator

from mgar_v3.domain import Model, digest
from mgar_v3.scanners.domain import ScanLocation

FINDING_SCHEMA = "v33-finding-v1"
FINDING_REPORT_SCHEMA = "v33-finding-report-v1"

FindingLifecycle = Literal[
    "suspected",
    "investigating",
    "reproduced",
    "confirmed_static",
    "not_reproduced",
    "fix_proposed",
    "patch_verified",
    "awaiting_approval",
]
ReproductionStatus = Literal[
    "not_attempted",
    "attempted",
    "static_only",
    "reproduced",
    "not_reproduced",
]
FindingClassification = Literal["defect", "quality", "risk"]
FindingSeverity = Literal["error", "warning", "info"]
FixRisk = Literal["low", "medium", "high"]
EvidenceKind = Literal[
    "scanner",
    "static_analysis",
    "test_run",
    "investigation",
    "patch_verification",
    "manual",
]
FindingOrigin = Literal["scanner", "investigation", "manual"]

LIFECYCLE_STATUSES: tuple[FindingLifecycle, ...] = (
    "suspected",
    "investigating",
    "reproduced",
    "confirmed_static",
    "not_reproduced",
    "fix_proposed",
    "patch_verified",
    "awaiting_approval",
)

SEVERITY_ORDER: dict[FindingSeverity, int] = {"info": 0, "warning": 1, "error": 2}
CLASSIFICATION_ORDER: dict[FindingClassification, int] = {"quality": 0, "risk": 1, "defect": 2}
REPRODUCIBILITY_ORDER: dict[FindingLifecycle, int] = {
    "not_reproduced": 0,
    "suspected": 1,
    "investigating": 2,
    "confirmed_static": 3,
    "reproduced": 4,
    "fix_proposed": 4,
    "patch_verified": 5,
    "awaiting_approval": 5,
}
FIX_RISK_ORDER: dict[FixRisk, int] = {"low": 0, "medium": 1, "high": 2}

_IDENTITY = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
_REPOSITORY = r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$"
_COMMIT = r"^[a-f0-9]{40}$"
_HASH = r"^[a-f0-9]{64}$"


def normalize_path(path: str) -> str:
    """Normalize a repository-relative path without touching its case."""

    value = path.replace("\\", "/").strip()
    while value.startswith("./"):
        value = value[2:]
    return "/".join(segment for segment in value.split("/") if segment not in {"", "."})


def dedupe_key_for(path: str, rule_id: str, root_cause: str = "") -> str:
    """Deduplicate one observation, never one issue per scanner.

    Path, normalized rule id and the caller-supplied root-cause clue form the
    identity. A repeated scanner warning on the same path therefore merges into
    a single Finding instead of being counted as several bugs.
    """

    root = " ".join(root_cause.strip().lower().split())
    return "|".join([normalize_path(path), rule_id.strip().lower(), root])


def finding_id_for(run_id: str, repository: str, commit: str, dedupe_key: str) -> str:
    return digest(
        {
            "schema": FINDING_SCHEMA,
            "run_id": run_id,
            "repository": repository.lower(),
            "commit": commit,
            "dedupe_key": dedupe_key,
        }
    )


class FindingEvidence(Model):
    """One mechanical proof attached to a Finding.

    ``evidence_id`` may be an existing ``app.evidence`` UUID or a deterministic
    identifier minted from scanner output. ``kind`` decides which lifecycle
    states the evidence can support.
    """

    evidence_id: Annotated[str, Field(pattern=_IDENTITY)]
    kind: EvidenceKind
    source_uri: Annotated[str, Field(min_length=1, max_length=400)]
    description: Annotated[str, Field(min_length=1, max_length=1_000)]
    content_hash: Annotated[str, Field(pattern=_HASH)]
    location: ScanLocation | None = None


class Finding(Model):
    finding_id: Annotated[str, Field(pattern=_HASH)]
    run_id: Annotated[str, Field(pattern=_IDENTITY)]
    repository: Annotated[str, Field(pattern=_REPOSITORY)]
    commit: Annotated[str, Field(pattern=_COMMIT)]
    category: Annotated[str, Field(min_length=1, max_length=60)]
    classification: FindingClassification
    severity: FindingSeverity
    confidence: Annotated[float, Field(ge=0, le=1)]
    summary: Annotated[str, Field(min_length=1, max_length=2_000)]
    source_locations: Annotated[list[ScanLocation], Field(min_length=1, max_length=32)]
    evidence_ids: Annotated[list[str], Field(max_length=32)]
    reproduction_status: ReproductionStatus
    suggested_fix: str = ""
    lifecycle_status: FindingLifecycle
    uncertainty: Annotated[str, Field(min_length=1, max_length=1_000)]
    root_cause: Annotated[str, Field(max_length=300)] = ""
    fix_risk: FixRisk = "medium"
    origin: FindingOrigin
    dedupe_key: Annotated[str, Field(min_length=1, max_length=1_000)]
    rule_ids: Annotated[list[str], Field(max_length=16)] = Field(default_factory=list)
    scanner_ids: Annotated[list[str], Field(max_length=16)] = Field(default_factory=list)
    schema_version: str = FINDING_SCHEMA

    @field_validator("repository")
    @classmethod
    def _lower_repository(cls, value: str) -> str:
        return value.lower()


class FindingCandidate(Model):
    """A pre-store observation from a scanner, a model investigation or a human."""

    category: Annotated[str, Field(min_length=1, max_length=60)]
    classification: FindingClassification
    severity: FindingSeverity
    confidence: Annotated[float, Field(ge=0, le=1)] = 1.0
    summary: Annotated[str, Field(min_length=1, max_length=2_000)]
    source_locations: Annotated[list[ScanLocation], Field(min_length=1, max_length=32)]
    rule_id: Annotated[str, Field(min_length=1, max_length=200)]
    evidence: Annotated[list[FindingEvidence], Field(max_length=32)] = Field(
        default_factory=list
    )
    uncertainty: Annotated[str, Field(max_length=1_000)] = ""
    suggested_fix: Annotated[str, Field(max_length=4_000)] = ""
    root_cause: Annotated[str, Field(max_length=300)] = ""
    fix_risk: FixRisk = "medium"
    origin: FindingOrigin
    scanner_ids: Annotated[list[str], Field(max_length=16)] = Field(default_factory=list)


class ReviewCoverage(Model):
    """How much of the repository the review actually covered."""

    files_considered: Annotated[int, Field(ge=0)] = 0
    files_scanned: Annotated[int, Field(ge=0)] = 0
    unscanned_files: Annotated[int, Field(ge=0)] = 0
    scanned_languages: list[str] = Field(default_factory=list)
    unsupported_languages: list[str] = Field(default_factory=list)
    scanner_statuses: dict[str, str] = Field(default_factory=dict)
    unscanned_scopes: list[str] = Field(default_factory=list, max_length=100)
    complete: bool = False


class FindingReport(Model):
    schema_version: str = FINDING_REPORT_SCHEMA
    run_id: str
    repository: str
    commit: str
    findings: list[Finding] = Field(default_factory=list)
    evidence: list[FindingEvidence] = Field(default_factory=list)
    coverage: ReviewCoverage = Field(default_factory=ReviewCoverage)
    candidates_received: Annotated[int, Field(ge=0)] = 0
    duplicates_merged: Annotated[int, Field(ge=0)] = 0


def finding_sort_key(finding: Finding) -> tuple[int, int, int, str]:
    """Severity first, then reproducibility, then fix risk, then stable id."""

    return (
        -SEVERITY_ORDER[finding.severity],
        -REPRODUCIBILITY_ORDER[finding.lifecycle_status],
        FIX_RISK_ORDER[finding.fix_risk],
        finding.finding_id,
    )
