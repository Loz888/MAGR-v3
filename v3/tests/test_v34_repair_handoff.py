"""V3.4 R01 investigation-to-repair handoff tests.

A repair may only start from a Finding that A04 reproved at the exact commit the
parent analysis reviewed.  These tests build real verifier receipts, then check
the typed handoff: the ten required fields, deterministic identity and every
refusal path (authorization, unverified target, version mismatch).
"""

from __future__ import annotations

import hashlib
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest

from mgar_v3.domain import DomainError
from mgar_v3.findings.model import Finding, FindingCandidate, FindingEvidence
from mgar_v3.findings.store import FindingStore
from mgar_v3.repair.handoff import (
    ParentAnalysisRun,
    RepairAuthorization,
    RepairHandoff,
    RepairHandoffBuilder,
)
from mgar_v3.repository_analysis.domain import AnalysisLimits
from mgar_v3.scanners.domain import ResourceLimits, ScanLocation
from mgar_v3.scanners.sandbox import CommandPolicy, CommandResult, LocalSandbox
from mgar_v3.verification.receipts import ReceiptJournal
from mgar_v3.verification.verifier import (
    CommandRunner,
    DefectVerifier,
    ReproductionPlan,
    VerificationEnvironment,
)

RUN_ID = "9c1f2a70-4b6d-4e18-8f3a-6d5b2c7a9e01"
PARENT_ID = UUID("3f8b1d20-5c47-4a91-9e02-7d6a4b1c8e55")
REPOSITORY = "octo/v34-python-fixture"
COMMIT = "c" * 40
REPORT_HASH = "d" * 64
ACTOR = "loz888"

PASSING = "def test_ok():\n    assert 1 + 1 == 2\n"
FAILING = "from src.calc import add\n\n\ndef test_add():\n    assert add(2, 2) == 4\n"
SOURCE = "def add(left, right):\n    return left - right\n"


def _repo(tmp_path: Path, files: dict[str, str]) -> Path:
    root = tmp_path / "repo"
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return root


def _runner(root: Path) -> CommandRunner:
    sandbox = LocalSandbox(
        root,
        limits=ResourceLimits(
            cpu_seconds=120,
            memory_bytes=2_147_483_648,
            max_output_bytes=1_048_576,
            max_findings=100,
        ),
        policy=CommandPolicy(),
        environment={
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": str(root),
        },
    )
    return sandbox.run


def _environment() -> VerificationEnvironment:
    return VerificationEnvironment.create(
        repository=REPOSITORY,
        commit=COMMIT,
        sandbox="local",
        python="3.12.16",
        tool_versions={"pytest": "9.2.0"},
        os_name="Linux",
    )


def _store(commit: str = COMMIT) -> FindingStore:
    return FindingStore(run_id=RUN_ID, repository=REPOSITORY, commit=commit)


def _candidate(
    store: FindingStore,
    *,
    path: str = "src/calc.py",
    rule_id: str = "python-semantic-bug",
    classification: str = "defect",
) -> Finding:
    return store.add_candidate(
        FindingCandidate(
            category="logic_error" if classification == "defect" else "dependency",
            classification=classification,  # type: ignore[arg-type]
            severity="error" if classification == "defect" else "warning",
            confidence=0.9,
            summary="fixture candidate for a generic repository",
            source_locations=[ScanLocation(path=path, line=2)],
            rule_id=rule_id,
            origin="investigation",
        )
    )


def _pytest(*targets: str) -> list[str]:
    return [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *targets]


def _verifier(
    tmp_path: Path, store: FindingStore, root: Path
) -> tuple[DefectVerifier, ReceiptJournal]:
    journal = ReceiptJournal(tmp_path / "evidence" / "receipts.jsonl")
    verifier = DefectVerifier(
        store,
        environment=_environment(),
        runner=_runner(root),
        journal=journal,
        baseline_command=_pytest("tests/test_ok.py"),
        baseline_timeout=120,
    )
    return verifier, journal


def _passing_runner() -> CommandRunner:
    result = CommandResult(
        exit_code=0,
        stdout="1 passed",
        stderr="",
        timed_out=False,
        truncated=False,
        duration_seconds=0.01,
    )
    return lambda _command, _timeout: result


def _parent(**updates: object) -> ParentAnalysisRun:
    values: dict[str, object] = {
        "run_id": PARENT_ID,
        "repository": REPOSITORY,
        "commit": COMMIT,
        "status": "succeeded",
        "report_hash": REPORT_HASH,
    }
    values.update(updates)
    return ParentAnalysisRun.model_validate(values)


def _authorization(**updates: object) -> RepairAuthorization:
    values: dict[str, object] = {
        "approved_by": ACTOR,
        "expires_at": datetime.now(UTC) + timedelta(hours=1),
        "allowed_repositories": [REPOSITORY],
        "limits": AnalysisLimits(),
        "allow_repair": True,
    }
    values.update(updates)
    return RepairAuthorization.model_validate(values)


def _builder(store: FindingStore) -> RepairHandoffBuilder:
    return RepairHandoffBuilder(store, allowed_actors=[ACTOR])


def _reproduced_finding(tmp_path: Path) -> tuple[FindingStore, Finding]:
    root = _repo(
        tmp_path,
        {
            "src/calc.py": SOURCE,
            "tests/test_ok.py": PASSING,
            "tests/test_calc.py": FAILING,
        },
    )
    store = _store()
    finding = _candidate(store)
    verifier, _ = _verifier(tmp_path, store, root)
    outcome = verifier.verify(
        ReproductionPlan(
            finding_id=finding.finding_id,
            command=_pytest("tests/test_calc.py"),
            expected_failure_signature="assert 0 == 4",
        )
    )
    assert outcome.status == "reproduced"
    return store, store.get(finding.finding_id)


def _static_finding(
    tmp_path: Path,
    *,
    path: str = "src/calc.py",
    kind: str = "scanner",
) -> tuple[FindingStore, Finding]:
    store = _store()
    finding = _candidate(store, path=path)
    verifier = DefectVerifier(
        store,
        environment=_environment(),
        runner=_passing_runner(),
        journal=ReceiptJournal(tmp_path / "evidence" / "receipts.jsonl"),
        baseline_command=_pytest("tests/test_ok.py"),
    )
    evidence = FindingEvidence(
        evidence_id=hashlib.sha256(f"static:{path}".encode()).hexdigest()[:64],
        kind=kind,  # type: ignore[arg-type]
        source_uri="scanner://fixture/static",
        description="static observation",
        content_hash=hashlib.sha256(b"static").hexdigest(),
        location=ScanLocation(path=path, line=2),
    )
    outcome = verifier.verify_static(
        finding.finding_id, evidence=evidence, rationale="deterministic scanner proof"
    )
    assert outcome.status == "static_confirmed"
    return store, store.get(finding.finding_id)


def _build(
    store: FindingStore,
    finding: Finding,
    **updates: object,
) -> RepairHandoff:
    values: dict[str, object] = {
        "parent": _parent(),
        "objective": "Return the arithmetic sum instead of the difference",
        "budget": AnalysisLimits(),
        "authorization": _authorization(),
    }
    values.update(updates)
    return _builder(store).build(finding.finding_id, **values)  # type: ignore[arg-type]


def test_reproduced_finding_creates_controlled_repair_run(tmp_path: Path) -> None:
    store, finding = _reproduced_finding(tmp_path)
    authorization = _authorization()
    handoff = _build(store, finding, authorization=authorization)

    payload = handoff.model_dump(mode="json")
    for field in (
        "repository",
        "base_commit",
        "finding_id",
        "evidence",
        "reproduction",
        "objective",
        "allowed_paths",
        "execution_budget",
        "authorization",
        "parent_run_id",
    ):
        assert field in payload

    assert handoff.repository == REPOSITORY
    assert handoff.base_commit == COMMIT
    assert handoff.finding_id == finding.finding_id
    assert handoff.parent_run_id == PARENT_ID
    assert handoff.parent_report_hash == REPORT_HASH
    assert handoff.allowed_paths == ["src/calc.py"]
    assert handoff.finding_lifecycle == "reproduced"
    assert handoff.graph_version == "r5-repair-v1"
    assert handoff.read_only is False
    assert handoff.authorization_hash == handoff.authorization.authorization_hash

    assert handoff.reproduction.kind == "reproduced"
    assert handoff.reproduction.commit == COMMIT
    assert handoff.reproduction.receipt_id is not None
    assert len(handoff.reproduction.receipt_id) == 64
    assert "tests/test_calc.py" in (handoff.reproduction.reproduce_command or "")
    assert handoff.reproduction.evidence_ids
    assert handoff.evidence

    assert handoff.handoff_id == handoff.computed_id()
    assert _build(store, finding, authorization=authorization).handoff_id == handoff.handoff_id
    changed = _build(store, finding, authorization=authorization, objective="Use the plus operator")
    assert changed.handoff_id != handoff.handoff_id

    serialized = handoff.model_dump_json()
    for legacy in ("orders.go", "TestPaidRevenueCents", "PaidRevenueCents"):
        assert legacy not in serialized


def test_static_confirmation_never_claims_reproduction(tmp_path: Path) -> None:
    store, finding = _static_finding(tmp_path)
    handoff = _build(store, finding)

    assert handoff.finding_lifecycle == "confirmed_static"
    assert handoff.reproduction.kind == "static_confirmation"
    assert handoff.reproduction.receipt_id is None
    assert handoff.reproduction.reproduce_command is None
    assert handoff.reproduction.evidence_ids == [finding.evidence_ids[0]]


def test_go_repository_uses_the_same_generic_handoff(tmp_path: Path) -> None:
    store, finding = _static_finding(
        tmp_path, path="internal/mathx/calc.go", kind="static_analysis"
    )
    handoff = _build(store, finding)

    assert handoff.allowed_paths == ["internal/mathx/calc.go"]
    serialized = handoff.model_dump_json()
    for legacy in ("orders.go", "go.mod", "TestPaidRevenueCents"):
        assert legacy not in serialized


def test_unverified_and_already_repaired_targets_are_rejected(tmp_path: Path) -> None:
    store = _store()
    suspected = _candidate(store)
    with pytest.raises(DomainError) as unverified:
        _build(store, suspected)
    assert unverified.value.code == "HANDOFF_TARGET_UNVERIFIED"

    store.transition(suspected.finding_id, "investigating")
    with pytest.raises(DomainError) as investigating:
        _build(store, store.get(suspected.finding_id))
    assert investigating.value.code == "HANDOFF_TARGET_UNVERIFIED"

    not_reproduced = _candidate(store, path="src/other.py")
    verifier = DefectVerifier(
        store,
        environment=_environment(),
        runner=_passing_runner(),
        journal=ReceiptJournal(tmp_path / "not-reproduced" / "receipts.jsonl"),
        baseline_command=_pytest("tests/test_ok.py"),
    )
    outcome = verifier.verify(
        ReproductionPlan(
            finding_id=not_reproduced.finding_id,
            command=_pytest("tests/test_calc.py"),
        )
    )
    assert outcome.status == "not_reproduced"
    with pytest.raises(DomainError) as passed:
        _build(store, store.get(not_reproduced.finding_id))
    assert passed.value.code == "HANDOFF_TARGET_UNVERIFIED"

    store2, reproduced = _reproduced_finding(tmp_path)
    repair = store2.transition(
        reproduced.finding_id,
        "fix_proposed",
        suggested_fix="use the plus operator",
    )
    with pytest.raises(DomainError) as repaired:
        _build(store2, repair)
    assert repaired.value.code == "HANDOFF_TARGET_ALREADY_REPAIRED"


def test_version_and_repository_mismatch_are_rejected(tmp_path: Path) -> None:
    store, finding = _reproduced_finding(tmp_path)

    with pytest.raises(DomainError) as version:
        _build(store, finding, parent=_parent(commit="e" * 40))
    assert version.value.code == "HANDOFF_VERSION_MISMATCH"

    with pytest.raises(DomainError) as repository:
        _build(store, finding, parent=_parent(repository="octo/another-repository"))
    assert repository.value.code == "HANDOFF_REPOSITORY_MISMATCH"


def test_repair_requires_explicit_verified_authorization(tmp_path: Path) -> None:
    store, finding = _reproduced_finding(tmp_path)

    with pytest.raises(DomainError) as disabled:
        _build(store, finding, authorization=_authorization(allow_repair=False))
    assert disabled.value.code == "HANDOFF_AUTHORIZATION_DENIED"

    expired = datetime.now(UTC) - timedelta(minutes=1)
    with pytest.raises(DomainError) as stale:
        _build(store, finding, authorization=_authorization(expires_at=expired))
    assert stale.value.code == "HANDOFF_AUTHORIZATION_DENIED"

    with pytest.raises(DomainError) as actor:
        _build(
            store,
            finding,
            authorization=_authorization(approved_by="mallory"),
        )
    assert actor.value.code == "HANDOFF_AUTHORIZATION_DENIED"

    with pytest.raises(DomainError) as repository:
        _build(
            store,
            finding,
            authorization=_authorization(allowed_repositories=["octo/other"]),
        )
    assert repository.value.code == "HANDOFF_AUTHORIZATION_DENIED"

    with pytest.raises(DomainError) as budget:
        _build(
            store,
            finding,
            budget=AnalysisLimits(max_tool_calls=40),
            authorization=_authorization(limits=AnalysisLimits(max_tool_calls=4)),
        )
    assert budget.value.code == "HANDOFF_BUDGET_EXCEEDED"


def test_parent_run_and_evidence_must_be_complete(tmp_path: Path) -> None:
    store, finding = _reproduced_finding(tmp_path)

    with pytest.raises(DomainError) as running:
        _build(store, finding, parent=_parent(status="running"))
    assert running.value.code == "HANDOFF_PARENT_NOT_SETTLED"

    with pytest.raises(DomainError) as no_report:
        _build(store, finding, parent=_parent(report_hash=None))
    assert no_report.value.code == "HANDOFF_PARENT_NOT_SETTLED"

    class _EvidenceLess:
        def get(self, finding_id: str) -> Finding:
            return finding.model_copy(update={"evidence_ids": []})

        def evidence(self) -> list[FindingEvidence]:
            return []

    with pytest.raises(DomainError) as missing:
        _build(_EvidenceLess(), finding)  # type: ignore[arg-type]
    assert missing.value.code == "HANDOFF_EVIDENCE_MISSING"

    class _Dangling:
        def get(self, finding_id: str) -> Finding:
            return finding.model_copy(update={"evidence_ids": ["f" * 64]})

        def evidence(self) -> list[FindingEvidence]:
            return store.evidence()

    with pytest.raises(DomainError) as dangling:
        _build(_Dangling(), finding)  # type: ignore[arg-type]
    assert dangling.value.code == "HANDOFF_EVIDENCE_MISSING"


def test_allowed_paths_cannot_escape_or_hide_the_defect(tmp_path: Path) -> None:
    store, finding = _reproduced_finding(tmp_path)

    for path in ("../etc/passwd", ".git/config", "/etc/passwd"):
        with pytest.raises(DomainError) as denied:
            _build(store, finding, allowed_paths=["src/calc.py", path])
        assert denied.value.code == "HANDOFF_PATH_DENIED"

    with pytest.raises(DomainError) as uncovered:
        _build(store, finding, allowed_paths=["src"])
    assert uncovered.value.code == "HANDOFF_PATH_DENIED"

    handoff = _build(store, finding, allowed_paths=["src/calc.py", "src/util.py"])
    assert handoff.allowed_paths == ["src/calc.py", "src/util.py"]
