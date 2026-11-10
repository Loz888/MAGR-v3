"""Controlled Investigation-to-Repair handoff (V3.4 R01).

A repair run may start only from a defect that A04 has already reproduced, or
statically confirmed, at the exact commit the parent V3.2 analysis reviewed.
The handoff is a typed, content-addressed contract that carries:

repository, base commit, finding id, relevant evidence, reproduction proof,
repair objective, allowed paths, execution budget, authorization and parent run.

Nothing here is specific to the historical R5 demo: there is no file name, test
name or business field from ``orders.go``.  A handoff is a pure value object so
callers cannot start a repair before every gate has passed.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Annotated, Literal, Self, cast
from uuid import UUID

from pydantic import Field, model_validator

from mgar_v3.domain import DomainError, Model, digest
from mgar_v3.findings.model import (
    Finding,
    FindingEvidence,
    FindingLifecycle,
    normalize_path,
)
from mgar_v3.findings.store import FindingStore
from mgar_v3.repository_analysis.domain import (
    AnalysisLimits,
    AnalysisSpec,
    validate_commit,
    validate_repo_path,
)
from mgar_v3.repository_analysis.workspace import validate_manifest_path

HANDOFF_SCHEMA = "v34-repair-handoff-v1"

_REPOSITORY = r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$"
_COMMIT = r"^[a-f0-9]{40}$"
_HASH = r"^[a-f0-9]{64}$"

STATIC_CONFIRMATION_KINDS = frozenset({"scanner", "static_analysis", "investigation", "manual"})
VERIFIED_LIFECYCLES: frozenset[FindingLifecycle] = frozenset({"reproduced", "confirmed_static"})
REPAIRED_LIFECYCLES: frozenset[FindingLifecycle] = frozenset(
    {"fix_proposed", "patch_verified", "awaiting_approval"}
)
SETTLED_PARENT_STATUSES = frozenset({"succeeded"})

ParentStatus = Literal[
    "queued",
    "running",
    "waiting_input",
    "succeeded",
    "failed",
    "cancelled",
    "paused",
]
PARENT_STATUSES = frozenset(
    {"queued", "running", "waiting_input", "succeeded", "failed", "cancelled", "paused"}
)


class ParentAnalysisRun(Model):
    """The settled V3.2 repository analysis a repair is allowed to inherit."""

    run_id: UUID
    repository: Annotated[str, Field(pattern=_REPOSITORY)]
    commit: Annotated[str, Field(pattern=_COMMIT)]
    graph_version: Literal["v32-repo-v1"] = "v32-repo-v1"
    status: ParentStatus = "succeeded"
    report_hash: Annotated[str, Field(pattern=_HASH)] | None = None

    @classmethod
    def from_spec(cls, spec: AnalysisSpec) -> ParentAnalysisRun:
        """Project a stored analysis spec, keeping a missing report visible."""

        if not spec.report_hash:
            raise DomainError(
                "HANDOFF_PARENT_NOT_SETTLED",
                "Parent analysis has no verified report to hand off",
                409,
            )
        status: ParentStatus = (
            cast(ParentStatus, spec.run_status) if spec.run_status in PARENT_STATUSES else "failed"
        )
        return cls(
            run_id=spec.run_id,
            repository=spec.repository.lower(),
            commit=spec.commit,
            status=status,
            report_hash=spec.report_hash,
        )


class RepairAuthorization(Model):
    """Explicit human authority to modify a repository, never implied by a flag."""

    approved_by: Annotated[str, Field(min_length=1, max_length=100)]
    expires_at: datetime
    allowed_repositories: Annotated[list[str], Field(min_length=1, max_length=100)] = Field(
        default_factory=lambda: ["*"]
    )
    limits: AnalysisLimits = Field(default_factory=AnalysisLimits)
    allow_repair: bool = False

    @model_validator(mode="after")
    def aware_expiry(self) -> Self:
        if self.expires_at.tzinfo is None:
            raise ValueError("Repair authorization expiry must include a timezone")
        return self

    @property
    def authorization_hash(self) -> str:
        return digest(self.model_dump(mode="json"))

    def validate_scope(
        self,
        *,
        repository: str,
        budget: AnalysisLimits,
        allowed_actors: Sequence[str],
    ) -> None:
        if not self.allow_repair:
            raise DomainError(
                "HANDOFF_AUTHORIZATION_DENIED",
                "Repair authorization is not enabled",
                403,
            )
        if self.expires_at <= datetime.now(UTC):
            raise DomainError(
                "HANDOFF_AUTHORIZATION_DENIED", "Repair authorization has expired", 403
            )
        if self.approved_by.lower() not in {actor.lower() for actor in allowed_actors}:
            raise DomainError(
                "HANDOFF_AUTHORIZATION_DENIED",
                "Repair approver is not an allowed actor",
                403,
            )
        allowed = {item.lower() for item in self.allowed_repositories}
        if "*" not in allowed and repository.lower() not in allowed:
            raise DomainError(
                "HANDOFF_AUTHORIZATION_DENIED",
                "Repository is outside the authorized repair scope",
                403,
            )
        if not budget.bounded_by(self.limits):
            raise DomainError(
                "HANDOFF_BUDGET_EXCEEDED",
                "Repair budget exceeds the authorized limits",
                403,
            )


class ReproductionProof(Model):
    """The mechanical proof a verified target must supply.

    ``reproduced`` requires a verifier-minted ``test_run`` receipt; a static
    confirmation may never pretend that the defect was executed.
    """

    kind: Literal["reproduced", "static_confirmation"]
    commit: Annotated[str, Field(pattern=_COMMIT)]
    evidence_ids: Annotated[list[str], Field(min_length=1, max_length=32)]
    receipt_id: Annotated[str, Field(pattern=_HASH)] | None = None
    reproduce_command: Annotated[str, Field(max_length=2_000)] | None = None

    @model_validator(mode="after")
    def proof_shape(self) -> Self:
        if self.kind == "reproduced" and self.receipt_id is None:
            raise ValueError("Reproduced proof requires a verifier receipt")
        if self.kind == "static_confirmation" and self.receipt_id is not None:
            raise ValueError("Static confirmation cannot claim an execution receipt")
        return self


class RepairHandoff(Model):
    """The complete, immutable descriptor of one controlled repair run."""

    schema_version: str = HANDOFF_SCHEMA
    handoff_id: Annotated[str, Field(pattern=_HASH)]
    repository: Annotated[str, Field(pattern=_REPOSITORY)]
    base_commit: Annotated[str, Field(pattern=_COMMIT)]
    finding_id: Annotated[str, Field(pattern=_HASH)]
    parent_run_id: UUID
    parent_report_hash: Annotated[str, Field(pattern=_HASH)]
    evidence: Annotated[list[FindingEvidence], Field(min_length=1, max_length=32)]
    reproduction: ReproductionProof
    objective: Annotated[str, Field(min_length=1, max_length=4_000)]
    allowed_paths: Annotated[list[str], Field(min_length=1, max_length=32)]
    execution_budget: AnalysisLimits
    authorization: RepairAuthorization
    authorization_hash: Annotated[str, Field(pattern=_HASH)]
    execution_mode: Literal["fixture", "live"] = "fixture"
    graph_version: Literal["r5-repair-v1"] = "r5-repair-v1"
    read_only: Literal[False] = False
    finding_lifecycle: FindingLifecycle

    def content(self) -> dict[str, object]:
        """The canonical payload that defines the handoff identity."""

        return {
            "schema_version": self.schema_version,
            "repository": self.repository,
            "base_commit": self.base_commit,
            "finding_id": self.finding_id,
            "parent_run_id": str(self.parent_run_id),
            "parent_report_hash": self.parent_report_hash,
            "evidence": [item.model_dump(mode="json") for item in self.evidence],
            "reproduction": self.reproduction.model_dump(mode="json"),
            "objective": self.objective,
            "allowed_paths": list(self.allowed_paths),
            "execution_budget": self.execution_budget.model_dump(mode="json"),
            "authorization": self.authorization.model_dump(mode="json"),
            "authorization_hash": self.authorization_hash,
            "execution_mode": self.execution_mode,
            "graph_version": self.graph_version,
            "read_only": self.read_only,
            "finding_lifecycle": self.finding_lifecycle,
        }

    def computed_id(self) -> str:
        return digest(self.content())


class RepairHandoffBuilder:
    """Validate every gate and mint one content-addressed repair handoff."""

    def __init__(self, store: FindingStore | None = None, *, allowed_actors: Sequence[str]):
        self.store = store
        self.allowed_actors = [str(actor) for actor in allowed_actors]

    def build(
        self,
        finding_id: str,
        *,
        parent: ParentAnalysisRun,
        objective: str,
        budget: AnalysisLimits,
        authorization: RepairAuthorization,
        allowed_paths: Sequence[str] | None = None,
        execution_mode: Literal["fixture", "live"] = "fixture",
    ) -> RepairHandoff:
        if self.store is None:
            raise DomainError(
                "HANDOFF_STORE_REQUIRED",
                "A stored finding is required for a store-backed handoff",
                409,
            )
        finding = self.store.get(finding_id)
        self._precheck(finding, parent)
        return self.build_from_snapshot(
            finding,
            self._evidence(finding),
            parent=parent,
            objective=objective,
            budget=budget,
            authorization=authorization,
            allowed_paths=allowed_paths,
            execution_mode=execution_mode,
        )

    def build_from_snapshot(
        self,
        finding: Finding,
        evidence: Sequence[FindingEvidence],
        *,
        parent: ParentAnalysisRun,
        objective: str,
        budget: AnalysisLimits,
        authorization: RepairAuthorization,
        allowed_paths: Sequence[str] | None = None,
        execution_mode: Literal["fixture", "live"] = "fixture",
    ) -> RepairHandoff:
        """Mint a handoff from an already-validated finding snapshot.

        The workbench keeps its own ledger; this entry point lets it reuse the
        same gates without opening a second FindingStore.  Evidence must be
        supplied explicitly, so a finding without retained proofs still fails.
        """

        registry: dict[str, FindingEvidence] = {}
        base_commit = self._precheck(finding, parent)
        for item in evidence:
            known = registry.get(item.evidence_id)
            if known is not None and known.model_dump(mode="json") != item.model_dump(
                mode="json"
            ):
                raise DomainError(
                    "HANDOFF_EVIDENCE_CONFLICT",
                    "Evidence id is bound to two different payloads",
                    409,
                )
            registry[item.evidence_id] = item
        if not finding.evidence_ids:
            raise DomainError("HANDOFF_EVIDENCE_MISSING", "Finding has no retained evidence", 409)
        missing = [item for item in finding.evidence_ids if item not in registry]
        if missing:
            raise DomainError(
                "HANDOFF_EVIDENCE_MISSING",
                "Finding references evidence outside its registry",
                409,
            )
        ordered = [registry[item] for item in finding.evidence_ids]
        reproduction = self._reproduction(finding, ordered)
        paths = self._paths(finding, allowed_paths)
        cleaned = " ".join(objective.split())
        if not cleaned:
            raise DomainError("HANDOFF_OBJECTIVE_REQUIRED", "Repair objective cannot be empty", 400)
        authorization.validate_scope(
            repository=finding.repository,
            budget=budget,
            allowed_actors=self.allowed_actors,
        )
        assert parent.report_hash is not None
        authorization_hash = authorization.authorization_hash
        handoff_id = digest(
            {
                "schema_version": HANDOFF_SCHEMA,
                "repository": finding.repository,
                "base_commit": base_commit,
                "finding_id": finding.finding_id,
                "parent_run_id": str(parent.run_id),
                "parent_report_hash": parent.report_hash,
                "evidence": [item.model_dump(mode="json") for item in ordered],
                "reproduction": reproduction.model_dump(mode="json"),
                "objective": cleaned,
                "allowed_paths": paths,
                "execution_budget": budget.model_dump(mode="json"),
                "authorization": authorization.model_dump(mode="json"),
                "authorization_hash": authorization_hash,
                "execution_mode": execution_mode,
                "graph_version": "r5-repair-v1",
                "read_only": False,
                "finding_lifecycle": finding.lifecycle_status,
            }
        )
        return RepairHandoff(
            handoff_id=handoff_id,
            repository=finding.repository,
            base_commit=base_commit,
            finding_id=finding.finding_id,
            parent_run_id=parent.run_id,
            parent_report_hash=parent.report_hash,
            evidence=ordered,
            reproduction=reproduction,
            objective=cleaned,
            allowed_paths=paths,
            execution_budget=budget,
            authorization=authorization,
            authorization_hash=authorization_hash,
            execution_mode=execution_mode,
            finding_lifecycle=finding.lifecycle_status,
        )

    @staticmethod
    def _require_parent(parent: ParentAnalysisRun) -> None:
        if parent.status not in SETTLED_PARENT_STATUSES or parent.report_hash is None:
            raise DomainError(
                "HANDOFF_PARENT_NOT_SETTLED",
                "Repair requires a settled parent analysis with a verified report",
                409,
            )

    def _precheck(self, finding: Finding, parent: ParentAnalysisRun) -> str:
        self._require_parent(parent)
        if parent.repository.lower() != finding.repository:
            raise DomainError(
                "HANDOFF_REPOSITORY_MISMATCH",
                "Finding and parent analysis belong to different repositories",
                409,
            )
        try:
            base_commit = validate_commit(parent.commit)
        except ValueError as exc:
            raise DomainError(
                "HANDOFF_VERSION_MISMATCH", "Parent commit is not a 40-character SHA", 409
            ) from exc
        if base_commit != finding.commit:
            raise DomainError(
                "HANDOFF_VERSION_MISMATCH",
                "Finding was verified at a different commit than the parent analysis",
                409,
            )
        self._require_verified(finding)
        return base_commit

    @staticmethod
    def _require_verified(finding: Finding) -> None:
        if finding.lifecycle_status in VERIFIED_LIFECYCLES:
            return
        if finding.lifecycle_status in REPAIRED_LIFECYCLES:
            raise DomainError(
                "HANDOFF_TARGET_ALREADY_REPAIRED",
                "Finding already advanced into a repair flow",
                409,
            )
        raise DomainError(
            "HANDOFF_TARGET_UNVERIFIED",
            (
                "Only a reproduced or statically confirmed finding can start a repair"
                f" (lifecycle is {finding.lifecycle_status},"
                f" reproduction is {finding.reproduction_status})"
            ),
            409,
        )

    def _evidence(self, finding: Finding) -> list[FindingEvidence]:
        if self.store is None:
            raise DomainError(
                "HANDOFF_STORE_REQUIRED",
                "Evidence lookup requires a stored finding",
                409,
            )
        registry = {item.evidence_id: item for item in self.store.evidence()}
        if not finding.evidence_ids:
            raise DomainError("HANDOFF_EVIDENCE_MISSING", "Finding has no retained evidence", 409)
        missing = [item for item in finding.evidence_ids if item not in registry]
        if missing:
            raise DomainError(
                "HANDOFF_EVIDENCE_MISSING",
                "Finding references evidence outside its registry",
                409,
            )
        return [registry[item] for item in finding.evidence_ids]

    @staticmethod
    def _reproduction(finding: Finding, evidence: Sequence[FindingEvidence]) -> ReproductionProof:
        if finding.lifecycle_status == "confirmed_static":
            static = sorted(
                (item for item in evidence if item.kind in STATIC_CONFIRMATION_KINDS),
                key=lambda item: item.evidence_id,
            )
            if not static:
                raise DomainError(
                    "HANDOFF_REPRODUCTION_PROOF_MISSING",
                    "Static confirmation needs scanner or static-analysis evidence",
                    409,
                )
            return ReproductionProof(
                kind="static_confirmation",
                commit=finding.commit,
                evidence_ids=[item.evidence_id for item in static],
            )
        runs = sorted(
            (
                item
                for item in evidence
                if item.kind == "test_run" and item.source_uri.startswith("receipt://")
            ),
            key=lambda item: item.evidence_id,
        )
        if not runs:
            raise DomainError(
                "HANDOFF_REPRODUCTION_PROOF_MISSING",
                "Reproduced finding has no verifier-minted execution receipt",
                409,
            )
        receipt_id = runs[0].source_uri.removeprefix("receipt://")
        if receipt_id != runs[0].content_hash:
            raise DomainError(
                "HANDOFF_REPRODUCTION_PROOF_MISSING",
                "Test evidence and its execution receipt disagree",
                409,
            )
        command: str | None = None
        prefix = "reproduced: "
        if runs[0].description.startswith(prefix):
            command = runs[0].description[len(prefix) :]
        return ReproductionProof(
            kind="reproduced",
            commit=finding.commit,
            evidence_ids=[item.evidence_id for item in runs],
            receipt_id=receipt_id,
            reproduce_command=command or None,
        )

    def _paths(self, finding: Finding, allowed_paths: Sequence[str] | None) -> list[str]:
        derived = sorted(
            {
                self._relative(location.path, "finding location")
                for location in finding.source_locations
                if location.path.strip()
            }
        )
        if not derived:
            raise DomainError(
                "HANDOFF_PATH_DENIED", "Finding has no source location to repair", 403
            )
        candidates = sorted(
            {
                self._relative(item, "allowed path")
                for item in (derived if allowed_paths is None else allowed_paths)
            }
        )
        if not candidates:
            raise DomainError("HANDOFF_PATH_DENIED", "Allowed paths cannot be empty", 403)
        if not set(derived).issubset(candidates):
            raise DomainError(
                "HANDOFF_PATH_DENIED",
                "Allowed paths must cover every finding source location",
                403,
            )
        return candidates

    @staticmethod
    def _relative(raw: str, label: str) -> str:
        value = raw.strip()
        if not value or value.startswith(("/", "~")) or "\\" in value or ":" in value:
            raise DomainError("HANDOFF_PATH_DENIED", f"{label} must be repository-relative", 403)
        path = normalize_path(value)
        try:
            validate_manifest_path(path)
            validate_repo_path(path)
        except (DomainError, ValueError) as exc:
            raise DomainError(
                "HANDOFF_PATH_DENIED",
                f"{label} {path!r} is outside a repository tree",
                403,
            ) from exc
        return path
