"""V3.4 R04 iterative repair and independent verification tests.

The loop must reproduce a failing baseline, accept only bounded structured
edits inside the authorized worktree, run the public tests, then run a hidden
verification suite the provider never sees.  A candidate that fails any stage
is reverted, turned into structured feedback and retried within the attempt and
test-run budgets.  Only a fully verified candidate becomes a Repair Proposal;
every failure reason is written to Episodic Memory.  These tests use the real
pinned R03 image produced by ``make sandbox-build``.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable

import pytest
from pydantic import ValidationError

from mgar_v3.repair.repair_agent import (
    MemoryEpisodeLog,
    PatchPlan,
    RepairAgent,
    RepairBudget,
    RepairPrompt,
    RepairRequest,
)
from mgar_v3.repair.worktree import FileEdit

CALC_BUG = "def add(a, b):\n    return a - b\n"
CALC_FIXED = "def add(a, b):\n    return a + b\n"
CALC_PUBLIC_ONLY = "def add(a, b):\n    return a + b if a == 2 and b == 2 else a - b\n"
CALC_WRONG = "def add(a, b):\n    return a + b + 1\n"
PY_TEST = "from src.calc import add\n\n\ndef test_add():\n    assert add(2, 2) == 4\n"
PY_HIDDEN = (
    "from src.calc import add\n\n\n"
    "def test_add_negative():\n"
    "    assert add(-3, -4) == -7\n\n\n"
    "def test_add_zero():\n"
    "    assert add(0, 5) == 5\n"
)
PY_FILES = {"src/calc.py": CALC_BUG, "tests/test_calc.py": PY_TEST}
PY_VERIFICATION = {"verification/test_calc_hidden.py": PY_HIDDEN}
PY_VERIFICATION_TESTS = [
    "verification/test_calc_hidden.py::test_add_negative",
    "verification/test_calc_hidden.py::test_add_zero",
]

GO_MOD = "module example.invalid/r04fixture\n\ngo 1.27\n"
GO_BUG = "package mathx\n\nfunc Sub(a, b int) int {\n\treturn a + b\n}\n"
GO_FIXED = "package mathx\n\nfunc Sub(a, b int) int {\n\treturn a - b\n}\n"
GO_TEST = (
    'package mathx\n\nimport "testing"\n\n'
    "func TestSub(t *testing.T) {\n\tif Sub(5, 2) != 3 {\n\t\tt.Fatalf(\"unexpected\")\n\t}\n}\n"
)
GO_HIDDEN = (
    'package mathx\n\nimport "testing"\n\n'
    "func TestSubNegative(t *testing.T) {\n\tif Sub(-1, -2) != 1 {\n"
    "\t\tt.Fatalf(\"unexpected\")\n\t}\n}\n"
)
GO_FILES = {
    "go.mod": GO_MOD,
    "internal/mathx/calc.go": GO_BUG,
    "internal/mathx/calc_test.go": GO_TEST,
}
GO_VERIFICATION = {"internal/mathx/hidden_test.go": GO_HIDDEN}
GO_VERIFICATION_TESTS = ["TestSubNegative"]

PlanFactory = Callable[[RepairPrompt], PatchPlan]


class ScriptedProvider:
    """Deterministic provider: one plan factory per attempt, no model call."""

    def __init__(self, *plans: PlanFactory) -> None:
        self._plans = list(plans)
        self.prompts: list[RepairPrompt] = []

    def propose(self, prompt: RepairPrompt) -> PatchPlan:
        self.prompts.append(prompt)
        if not self._plans:
            raise AssertionError("provider received an unexpected extra attempt")
        return self._plans.pop(0)(prompt)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _replace(path: str, content: str, summary: str = "Apply the minimal repair") -> PlanFactory:
    def plan(prompt: RepairPrompt) -> PatchPlan:
        return PatchPlan(
            summary=summary,
            edits=[
                FileEdit(
                    path=path,
                    operation="update",
                    content=content,
                    expected_sha256=_sha(prompt.files[path]),
                )
            ],
        )

    return plan


def _create(path: str, content: str) -> PlanFactory:
    def plan(_prompt: RepairPrompt) -> PatchPlan:
        return PatchPlan(
            summary="Create a file",
            edits=[FileEdit(path=path, operation="create", content=content)],
        )

    return plan


def _python_request(
    *,
    files: dict[str, str] | None = None,
    budget: RepairBudget | None = None,
    allowed_paths: list[str] | None = None,
    frozen_paths: list[str] | None = None,
) -> RepairRequest:
    return RepairRequest(
        repository="example/r04-fixture",
        objective="Fix the add function so the public and independent tests agree",
        language="python",
        files=dict(files if files is not None else PY_FILES),
        allowed_paths=allowed_paths if allowed_paths is not None else ["src/calc.py"],
        frozen_paths=frozen_paths or [],
        verification_files=dict(PY_VERIFICATION),
        verification_tests=list(PY_VERIFICATION_TESTS),
        budget=budget or RepairBudget(),
    )


def _go_request() -> RepairRequest:
    return RepairRequest(
        repository="example/r04-go-fixture",
        objective="Fix Sub so the public and independent Go tests agree",
        language="go",
        files=dict(GO_FILES),
        allowed_paths=["internal/mathx/calc.go"],
        frozen_paths=["internal/mathx/calc_test.go"],
        verification_files=dict(GO_VERIFICATION),
        verification_tests=list(GO_VERIFICATION_TESTS),
    )


def _statuses(memory: MemoryEpisodeLog) -> list[str]:
    return [episode.status for episode in memory.episodes]


def test_python_repair_loops_from_public_failure_to_verified_proposal() -> None:
    memory = MemoryEpisodeLog()
    provider = ScriptedProvider(
        _replace("src/calc.py", CALC_WRONG),
        _replace("src/calc.py", CALC_FIXED),
    )
    report = RepairAgent(memory=memory).run(_python_request(), provider)

    assert report.status == "succeeded"
    assert report.baseline is not None
    assert report.baseline.status == "test_failed"
    assert report.baseline.failed_tests == ["tests/test_calc.py::test_add"]
    assert report.test_runs == 4
    assert len(report.attempts) == 2

    first, second = report.attempts
    assert first.accepted is False
    assert first.public_receipt is not None
    assert first.public_receipt.status == "test_failed"
    assert first.feedback is not None
    assert first.feedback.stage == "public_tests"
    assert first.feedback.failed_tests == ["tests/test_calc.py::test_add"]
    assert second.accepted is True
    assert second.verification_receipt is not None
    assert second.verification_receipt.status == "passed"

    proposal = report.proposal
    assert proposal is not None
    assert proposal.verified is True
    assert proposal.base_tree == report.base_tree
    assert proposal.tested_tree == second.tree_after
    assert proposal.attempts_used == 2
    assert [item.path for item in proposal.changed_files] == ["src/calc.py"]
    assert "+    return a + b" in proposal.diff
    assert proposal.public_receipt.status == "passed"
    assert set(PY_VERIFICATION_TESTS) <= set(proposal.verification_receipt.tests)
    assert proposal.verification_receipt.image_id is not None
    assert proposal.verification_receipt.image_id.startswith("sha256:")
    assert len(proposal.verification_receipt.log_sha256) == 64
    assert len(proposal.proposal_id) == 64

    assert provider.prompts[0].feedback == []
    assert provider.prompts[1].feedback[0].stage == "public_tests"
    assert "verification/test_calc_hidden.py" not in provider.prompts[0].files
    assert _statuses(memory) == ["public_tests_failed", "succeeded"]
    assert memory.episodes[0].failed_tests == ["tests/test_calc.py::test_add"]


def test_hidden_verification_rejects_a_public_test_only_fix() -> None:
    memory = MemoryEpisodeLog()
    provider = ScriptedProvider(
        _replace("src/calc.py", CALC_PUBLIC_ONLY),
        _replace("src/calc.py", CALC_FIXED),
    )
    report = RepairAgent(memory=memory).run(_python_request(), provider)

    assert report.status == "succeeded"
    assert len(report.attempts) == 2
    rejected = report.attempts[0]
    assert rejected.accepted is False
    assert rejected.public_receipt is not None
    assert rejected.public_receipt.status == "passed"
    assert rejected.verification_receipt is not None
    assert rejected.verification_receipt.status == "test_failed"
    assert rejected.feedback is not None
    assert rejected.feedback.stage == "verification"
    assert rejected.feedback.failed_tests == []
    assert "hidden" not in rejected.feedback.message.lower()

    assert provider.prompts[1].feedback[0].stage == "verification"
    assert "verification/test_calc_hidden.py" not in provider.prompts[1].files
    assert _statuses(memory) == ["verification_failed", "succeeded"]
    assert report.proposal is not None
    assert _sha(CALC_FIXED) == report.proposal.changed_files[0].sha256


def test_go_repair_uses_the_same_loop_and_independent_suite() -> None:
    memory = MemoryEpisodeLog()
    provider = ScriptedProvider(_replace("internal/mathx/calc.go", GO_FIXED))
    report = RepairAgent(memory=memory).run(_go_request(), provider)

    assert report.status == "succeeded"
    assert report.language == "go"
    assert report.baseline is not None
    assert report.baseline.status == "test_failed"
    assert report.baseline.failed_tests == ["TestSub"]
    proposal = report.proposal
    assert proposal is not None
    assert proposal.verification_receipt.adapter == "go-test-json-v1"
    assert "TestSubNegative" in proposal.verification_receipt.tests
    assert "-	return a + b" in proposal.diff
    assert "+	return a - b" in proposal.diff
    assert _statuses(memory) == ["succeeded"]


def test_missing_reproducible_baseline_never_produces_a_proposal() -> None:
    memory = MemoryEpisodeLog()
    provider = ScriptedProvider()
    report = RepairAgent(memory=memory).run(
        _python_request(files={"src/calc.py": CALC_FIXED, "tests/test_calc.py": PY_TEST}),
        provider,
    )

    assert report.status == "baseline_not_reproduced"
    assert report.stage == "baseline"
    assert report.proposal is None
    assert report.attempts == []
    assert report.test_runs == 1
    assert report.baseline is not None
    assert report.baseline.status == "passed"
    assert provider.prompts == []
    assert _statuses(memory) == ["baseline_not_reproduced"]


def test_scope_protected_and_frozen_paths_are_rejected_before_tests() -> None:
    memory = MemoryEpisodeLog()
    provider = ScriptedProvider(
        lambda _prompt: PatchPlan(
            summary="Rewrite dependency metadata",
            edits=[FileEdit(path="pyproject.toml", operation="create", content="[project]\n")],
        ),
        _replace("tests/test_calc.py", "def test_add():\n    assert True\n"),
        _replace("src/calc.py", CALC_FIXED),
    )
    request = _python_request(
        allowed_paths=["src/calc.py", "tests/test_calc.py", "pyproject.toml"],
        frozen_paths=["tests/test_calc.py"],
    )
    report = RepairAgent(memory=memory).run(request, provider)

    assert report.status == "succeeded"
    assert len(report.attempts) == 3
    protected, frozen, accepted = report.attempts
    assert protected.feedback is not None
    assert protected.feedback.code == "PATCH_PROTECTED"
    assert protected.public_receipt is None
    assert frozen.feedback is not None
    assert frozen.feedback.code == "REPAIR_FROZEN_PATH"
    assert frozen.public_receipt is None
    assert accepted.accepted is True
    assert report.test_runs == 3
    assert _statuses(memory) == ["patch_rejected", "patch_rejected", "succeeded"]


def test_exhausted_repair_never_publishes_a_bad_patch() -> None:
    memory = MemoryEpisodeLog()
    provider = ScriptedProvider(
        _replace("src/calc.py", CALC_WRONG),
        _replace("src/calc.py", CALC_WRONG),
    )
    request = _python_request(budget=RepairBudget(max_attempts=2))
    report = RepairAgent(memory=memory).run(request, provider)

    assert report.status == "exhausted"
    assert report.stage == "public_tests"
    assert report.proposal is None
    assert len(report.attempts) == 2
    assert all(not attempt.accepted for attempt in report.attempts)
    assert report.reason.startswith("REPAIR_EXHAUSTED")
    assert report.test_runs == 3
    assert _statuses(memory) == [
        "public_tests_failed",
        "public_tests_failed",
        "exhausted",
    ]
    assert all(episode.attempt >= 0 for episode in memory.episodes)
    assert _sha(CALC_BUG) not in report.reason


def test_test_run_budget_stops_the_loop_before_a_verification_run() -> None:
    memory = MemoryEpisodeLog()
    provider = ScriptedProvider(_replace("src/calc.py", CALC_WRONG))
    request = _python_request(
        budget=RepairBudget(max_attempts=3, max_test_runs=2),
    )
    report = RepairAgent(memory=memory).run(request, provider)

    assert report.status == "budget_exhausted"
    assert report.proposal is None
    assert len(report.attempts) == 1
    assert report.test_runs == 2
    assert _statuses(memory) == ["public_tests_failed", "budget_exhausted"]


def test_unsafe_dependency_installation_waits_without_running_the_model() -> None:
    memory = MemoryEpisodeLog()
    provider = ScriptedProvider()
    files = dict(PY_FILES)
    files["src/calc.py"] = "import requests\n\n\ndef add(a, b):\n    return a - b\n"
    report = RepairAgent(memory=memory).run(_python_request(files=files), provider)

    assert report.status == "waiting_for_dependencies"
    assert report.baseline is not None
    assert report.baseline.status == "waiting_for_dependencies"
    assert report.proposal is None
    assert provider.prompts == []
    assert _statuses(memory) == ["waiting_for_dependencies"]


def test_deadline_and_request_shape_are_enforced() -> None:
    ticks = iter([0.0, 100.0, 200.0])
    report = RepairAgent(clock=lambda: next(ticks)).run(
        _python_request(budget=RepairBudget(deadline_seconds=10)), ScriptedProvider()
    )
    assert report.status == "deadline_exceeded"
    assert report.baseline is None
    assert report.test_runs == 0

    with pytest.raises(ValidationError):
        RepairRequest(
            repository="example/r04-fixture",
            objective="Fix the defect",
            language="python",
            files=dict(PY_FILES),
            allowed_paths=["src/calc.py"],
            verification_files={"src/calc.py": "x = 1\n"},
            verification_tests=["test_x"],
        )
    with pytest.raises(ValidationError):
        RepairRequest(
            repository="example/r04-fixture",
            objective="Fix the defect",
            language="python",
            files=dict(PY_FILES),
            allowed_paths=["src/calc.py"],
            verification_files={},
            verification_tests=["test_x"],
        )
