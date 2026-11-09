"""V3.3 A04 independent defect verifier tests.

These tests run real pytest processes against disposable fixture repositories.
The point is that only a verifier-minted, commit-bound receipt can move a
Finding into ``reproduced``; a hypothesis that does not reproduce stays
unverified instead of being reported as a confirmed bug.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from mgar_v3.domain import DomainError
from mgar_v3.findings.model import Finding, FindingCandidate, FindingEvidence
from mgar_v3.findings.store import FindingStore
from mgar_v3.scanners.domain import ResourceLimits, ScanLocation
from mgar_v3.scanners.sandbox import CommandPolicy, CommandResult, LocalSandbox
from mgar_v3.verification.receipts import (
    ReceiptJournal,
    classify_execution,
    execution_receipt,
)
from mgar_v3.verification.verifier import (
    CommandRunner,
    DefectVerifier,
    ReproductionPlan,
    VerificationEnvironment,
)

RUN_ID = "9c1f2a70-4b6d-4e18-8f3a-6d5b2c7a9e01"
REPOSITORY = "octo/a04-fixture"
COMMIT = "b" * 40

PASSING = "def test_ok():\n    assert 1 + 1 == 2\n"
FAILING = "def test_arithmetic():\n    assert 2 + 2 == 5\n"
MISSING_DEPENDENCY = (
    "import definitely_missing_dependency_a04\n\n\ndef test_ok():\n    assert True\n"
)


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


def _store() -> FindingStore:
    return FindingStore(run_id=RUN_ID, repository=REPOSITORY, commit=COMMIT)


def _finding(
    store: FindingStore,
    *,
    classification: str = "defect",
    path: str = "tests/test_calc.py",
) -> Finding:
    return store.add_candidate(
        FindingCandidate(
            category="test_failure" if classification == "defect" else "dependency",
            classification=classification,
            severity="error" if classification == "defect" else "warning",
            confidence=1.0,
            summary="fixture candidate",
            source_locations=[ScanLocation(path=path, line=2)],
            rule_id="python-pytest" if classification == "defect" else "DEP_LOCKFILE_MISSING",
            origin="scanner",
            scanner_ids=["python-pytest"],
        )
    )


def _pytest(*targets: str) -> list[str]:
    return [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *targets]


def _verifier(
    tmp_path: Path,
    store: FindingStore,
    root: Path,
    *,
    baseline_target: str = "tests/test_ok.py",
    journal: ReceiptJournal | None = None,
) -> tuple[DefectVerifier, ReceiptJournal]:
    receipts = journal or ReceiptJournal(tmp_path / "evidence" / "receipts.jsonl")
    verifier = DefectVerifier(
        store,
        environment=_environment(),
        runner=_runner(root),
        journal=receipts,
        baseline_command=_pytest(baseline_target),
    )
    return verifier, receipts


def _plan(
    finding_id: str,
    target: str,
    *,
    signature: str | None = None,
) -> ReproductionPlan:
    return ReproductionPlan(
        finding_id=finding_id,
        command=_pytest(target),
        timeout=120,
        expected_failure_signature=signature,
        rationale="fixture reproduction",
    )


def _result(
    exit_code: int,
    *,
    stdout: str = "",
    stderr: str = "",
    timed_out: bool = False,
) -> CommandResult:
    return CommandResult(
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        timed_out=timed_out,
        truncated=False,
        duration_seconds=0.1,
    )


def test_baseline_is_bound_to_commit_environment_and_command(tmp_path: Path) -> None:
    root = _repo(tmp_path, {"tests/test_ok.py": PASSING})
    verifier, journal = _verifier(tmp_path, _store(), root)

    baseline = verifier.record_baseline()

    assert baseline.classification == "passed"
    assert baseline.environment_healthy is True
    assert baseline.baseline_dirty is False
    receipt = baseline.receipt
    assert receipt.commit == COMMIT
    assert receipt.environment_id == verifier.environment.environment_id
    assert receipt.command == _pytest("tests/test_ok.py")
    assert receipt.reproduce_command == " ".join(receipt.command)
    assert receipt.sandbox == "local"
    assert [item.receipt_id for item in journal.receipts()] == [receipt.receipt_id]
    assert not (root / ".pytest_cache").exists()


def test_real_failure_is_recorded_as_reproduced(tmp_path: Path) -> None:
    root = _repo(tmp_path, {"tests/test_calc.py": FAILING})
    store = _store()
    finding = _finding(store)
    verifier, journal = _verifier(
        tmp_path, store, root, baseline_target="tests/test_calc.py"
    )

    outcome = verifier.verify(
        _plan(finding.finding_id, "tests/test_calc.py", signature="test_arithmetic")
    )

    assert outcome.status == "reproduced"
    assert outcome.classification == "test_failure"
    assert outcome.commit == COMMIT
    current = store.get(finding.finding_id)
    assert current.lifecycle_status == "reproduced"
    assert current.reproduction_status == "reproduced"
    evidence = {item.evidence_id: item for item in store.evidence()}
    kinds = {evidence[item].kind for item in current.evidence_ids if item in evidence}
    assert "test_run" in kinds
    assert outcome.receipt_id in {item.receipt_id for item in journal.receipts()}
    assert (root / "tests" / "test_calc.py").read_text(encoding="utf-8") == FAILING


def test_unreproducible_candidate_is_not_confirmed(tmp_path: Path) -> None:
    root = _repo(tmp_path, {"tests/test_calc.py": PASSING})
    store = _store()
    finding = _finding(store)
    verifier, _ = _verifier(tmp_path, store, root, baseline_target="tests/test_calc.py")

    outcome = verifier.verify(_plan(finding.finding_id, "tests/test_calc.py"))

    assert outcome.status == "not_reproduced"
    current = store.get(finding.finding_id)
    assert current.lifecycle_status == "not_reproduced"
    assert current.reproduction_status == "not_reproduced"


def test_failure_without_the_expected_signature_is_not_reproduced(tmp_path: Path) -> None:
    root = _repo(tmp_path, {"tests/test_calc.py": FAILING})
    store = _store()
    finding = _finding(store)
    verifier, _ = _verifier(tmp_path, store, root, baseline_target="tests/test_calc.py")

    outcome = verifier.verify(
        _plan(finding.finding_id, "tests/test_calc.py", signature="test_something_else")
    )

    assert outcome.status == "signature_mismatch"
    current = store.get(finding.finding_id)
    assert current.lifecycle_status == "investigating"
    assert current.reproduction_status == "attempted"
    assert current.reproduction_status != "reproduced"


def test_environment_error_never_changes_the_finding(tmp_path: Path) -> None:
    root = _repo(tmp_path, {"tests/test_ok.py": PASSING})
    store = _store()
    finding = _finding(store)
    verifier, journal = _verifier(tmp_path, store, root)

    outcome = verifier.verify(_plan(finding.finding_id, "tests/does_not_exist.py"))

    assert outcome.status == "environment_error"
    assert outcome.classification == "environment_error"
    assert store.get(finding.finding_id).lifecycle_status == "investigating"
    assert store.get(finding.finding_id).reproduction_status == "attempted"
    summary = verifier.summary()
    assert summary.reproduced == 0
    assert summary.unverified == 1
    assert summary.complete is False
    assert outcome.receipt_id in {item.receipt_id for item in journal.receipts()}


def test_missing_dependency_is_distinct_from_a_real_failure(tmp_path: Path) -> None:
    root = _repo(
        tmp_path,
        {"tests/test_ok.py": PASSING, "tests/test_missing.py": MISSING_DEPENDENCY},
    )
    store = _store()
    finding = _finding(store)
    verifier, _ = _verifier(tmp_path, store, root)

    outcome = verifier.verify(_plan(finding.finding_id, "tests/test_missing.py"))

    assert outcome.status == "missing_dependency"
    assert outcome.classification == "missing_dependency"
    assert store.get(finding.finding_id).lifecycle_status == "investigating"


def test_static_risk_is_confirmed_statically_never_reproduced(tmp_path: Path) -> None:
    root = _repo(tmp_path, {"tests/test_ok.py": PASSING})
    store = _store()
    finding = _finding(store, classification="risk", path="pyproject.toml")
    verifier, _ = _verifier(tmp_path, store, root)

    denied = verifier.verify(_plan(finding.finding_id, "tests/test_ok.py"))
    assert denied.status == "denied"
    assert denied.reason == "STATIC_ONLY_CONFIRMATION_REQUIRED"
    assert store.get(finding.finding_id).lifecycle_status == "suspected"

    evidence = FindingEvidence(
        evidence_id="d" * 64,
        kind="static_analysis",
        source_uri="dependency://pyproject.toml",
        description="no lock file is committed",
        content_hash="e" * 64,
        location=ScanLocation(path="pyproject.toml"),
    )
    confirmed = verifier.verify_static(
        finding.finding_id,
        evidence=evidence,
        rationale="lock-file absence is fully observable in the commit tree",
    )
    assert confirmed.status == "static_confirmed"
    current = store.get(finding.finding_id)
    assert current.lifecycle_status == "confirmed_static"
    assert current.reproduction_status == "static_only"

    with pytest.raises(DomainError) as wrong_kind:
        verifier.verify_static(
            finding.finding_id,
            evidence=evidence.model_copy(update={"kind": "test_run"}),
            rationale="runtime proof",
        )
    assert wrong_kind.value.code == "STATIC_EVIDENCE_REQUIRED"


def test_agent_cannot_declare_verification_success(tmp_path: Path) -> None:
    root = _repo(tmp_path, {"tests/test_calc.py": FAILING})
    store = _store()
    finding = _finding(store)
    verifier, _ = _verifier(tmp_path, store, root, baseline_target="tests/test_calc.py")
    forged = execution_receipt(
        finding_id=finding.finding_id,
        command=_pytest("tests/test_calc.py"),
        result=_result(1, stdout="1 failed"),
        commit=COMMIT,
        environment_id=verifier.environment.environment_id,
        sandbox="local",
    )

    rejected = verifier.submit_receipt(forged)
    assert rejected.status == "denied"
    assert rejected.reason == "AGENT_SELF_VERIFICATION_DENIED"
    assert store.get(finding.finding_id).lifecycle_status == "suspected"

    mismatched = forged.model_copy(update={"commit": "c" * 40})
    wrong_commit = verifier.submit_receipt(mismatched)
    assert wrong_commit.reason == "RECEIPT_BINDING_MISMATCH"

    outcome = verifier.verify(
        _plan(finding.finding_id, "tests/test_calc.py", signature="test_arithmetic")
    )
    assert outcome.status == "reproduced"
    replay = verifier.submit_receipt(verifier.journal.receipts()[-1])
    assert replay.status == "recorded_existing"
    assert store.get(finding.finding_id).lifecycle_status == "reproduced"


def test_normal_repository_returns_zero_defects(tmp_path: Path) -> None:
    root = _repo(tmp_path, {"tests/test_ok.py": PASSING})
    store = _store()
    verifier, _ = _verifier(tmp_path, store, root)

    baseline = verifier.record_baseline()
    summary = verifier.summary()

    assert baseline.classification == "passed"
    assert summary.zero_defects is True
    assert summary.findings_total == 0
    assert summary.reproduced == 0
    assert summary.unverified == 0
    assert summary.baseline_classification == "passed"
    assert summary.complete is True


def test_unhealthy_baseline_blocks_reproduction(tmp_path: Path) -> None:
    root = _repo(tmp_path, {"tests/test_ok.py": PASSING})
    store = _store()
    finding = _finding(store)
    verifier, _ = _verifier(tmp_path, store, root, baseline_target="tests/missing.py")

    outcome = verifier.verify(_plan(finding.finding_id, "tests/test_ok.py"))

    assert outcome.status == "baseline_unhealthy"
    assert store.get(finding.finding_id).lifecycle_status == "suspected"
    assert verifier.summary().complete is False


def test_classification_separates_environment_dependencies_and_real_failures() -> None:
    failing = _pytest("tests/test_calc.py")
    assert classify_execution(failing, _result(0)) == "passed"
    assert classify_execution(failing, _result(1, stdout="1 failed")) == "test_failure"
    assert classify_execution(failing, _result(5, stdout="no tests ran")) == "no_tests"
    assert classify_execution(failing, _result(4, stderr="error: file not found")) == (
        "environment_error"
    )
    assert classify_execution(
        failing, _result(2, stdout="ModuleNotFoundError: No module named 'x'")
    ) == "missing_dependency"
    assert classify_execution(failing, _result(1, timed_out=True)) == "timeout"
    assert classify_execution(["go", "test", "./..."], _result(1, stdout="--- FAIL")) == (
        "test_failure"
    )
    assert classify_execution(["mystery-tool"], _result(3)) == "command_error"
    assert classify_execution(
        ["mystery-tool"], _result(127, stderr="mystery-tool: command not found")
    ) == "environment_error"


def test_receipts_are_deterministic_and_the_journal_is_append_only(
    tmp_path: Path,
) -> None:
    command = _pytest("tests/test_calc.py")
    result = _result(1, stdout="1 failed")
    first = execution_receipt(
        finding_id="f" * 64,
        command=command,
        result=result,
        commit=COMMIT,
        environment_id="a" * 64,
        sandbox="local",
    )
    second = execution_receipt(
        finding_id="f" * 64,
        command=command,
        result=result,
        commit=COMMIT,
        environment_id="a" * 64,
        sandbox="local",
    )
    assert first.receipt_id == second.receipt_id
    assert first == second

    journal = ReceiptJournal(tmp_path / "receipts.jsonl")
    journal.append(first)
    journal.append(first)
    assert len(journal.receipts()) == 1
    assert journal.owns(first.receipt_id) is True
