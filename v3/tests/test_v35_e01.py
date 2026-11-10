"""V3.5 E01 end-to-end evaluation harness tests.

The dataset tests are offline.  The two end-to-end tests drive the real
``AuthorizedRepairOrchestrator`` over the pinned R03 sandbox image, so they
require Docker just like the v3.4 sandbox tests.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from mgar_v3.quality_eval.v35 import grader, runner, tasks
from mgar_v3.repair.sandbox_executor import _python_requirements

EXPERIMENT = Path(__file__).resolve().parents[1] / "experiments" / "v35-harness"


def _dev() -> list[tasks.Task]:
    return tasks.load_development(EXPERIMENT)


def test_dataset_covers_three_kinds_and_one_adversarial_task() -> None:
    selected = _dev()
    assert len(selected) == 4
    assert {item.kind for item in selected} == set(tasks.KINDS)
    adverse = [item for item in selected if item.adversarial]
    assert len(adverse) == 1
    assert adverse[0].resume and adverse[0].first_patch
    assert len({item.task_id for item in selected}) == len(selected)


def test_every_task_pins_commit_snapshot_and_hiding() -> None:
    for item in _dev():
        assert len(item.base_commit) == 40
        assert item.base_commit in item.source_url
        assert item.snapshot_digest == tasks.snapshot_digest(item.snapshot_dir)
        assert item.hidden_tests and item.public_targets
        assert set(item.public_files).isdisjoint(item.hidden_files)
        assert set(item.public_targets).isdisjoint(item.hidden_targets)
        assert item.budget["max_attempts"] >= 2
        assert item.success["forbid_publication"] is True
    multi = [item for item in _dev() if item.kind == "multi_file_feature"]
    assert multi and len(multi[0].expected_changed_paths) >= 2


def test_development_loader_never_opens_the_holdout() -> None:
    opened: list[str] = []
    original = Path.read_text

    def traced(self: Path, *args: object, **kwargs: object) -> str:
        opened.append(self.as_posix())
        return original(self, *args, **kwargs)

    Path.read_text = traced  # type: ignore[method-assign]
    try:
        assert tasks.load_development(EXPERIMENT)
    finally:
        Path.read_text = original  # type: ignore[method-assign]
    assert not any("holdout" in path for path in opened)

    holdout = tasks.load_holdout(EXPERIMENT)
    assert holdout and all(item.holdout for item in holdout)
    assert {item.task_id for item in holdout}.isdisjoint(
        item.task_id for item in _dev()
    )


def test_hidden_sources_never_enter_the_visible_snapshot() -> None:
    for item in _dev():
        snapshot = runner.snapshot_for(item)
        assert not any(path.startswith("verification/") for path in snapshot.files)
        for target in item.public_targets:
            assert target in snapshot.files
        for path in item.hidden_files:
            assert "verification/" in path.as_posix()


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("waiting_approval", grader.PASS),
        ("baseline_not_reproduced", grader.NOT_REPRODUCED),
        ("waiting_for_dependencies", grader.INFRASTRUCTURE),
        ("sandbox_error", grader.INFRASTRUCTURE),
        ("exhausted", grader.FAIL),
        ("budget_exhausted", grader.FAIL),
        ("provider_error", grader.FAIL),
        ("deadline_exceeded", grader.FAIL),
        ("cancelled", grader.FAIL),
    ],
)
def test_classify_separates_failure_modes(status: str, expected: str) -> None:
    assert grader.classify({"status": status}) == expected


def test_fixture_summary_never_claims_the_live_gate() -> None:
    summary = grader.summarize(
        [
            {
                "task_id": "t",
                "kind": "known_bug_fix",
                "status": "pass",
                "usage": {"model_calls": 1},
            }
        ],
        mode="fixture",
    )
    assert summary["gate"] == "engineering_gate"
    assert summary["live_agent_gate"] == "NOT_RUN"
    assert summary["success_rate"] == 1.0


def test_report_directory_is_never_replaced(tmp_path: Path) -> None:
    result = {
        "schema": "mgar-v35-eval-result-v1",
        "task_id": "v35-fixture",
        "kind": "known_bug_fix",
        "status": "pass",
        "usage": {"model_calls": 1},
    }
    out = tmp_path / "report"
    runner.write_report([result], out_dir=out, mode="fixture", suite="dev")
    with pytest.raises(FileExistsError):
        runner.write_report([result], out_dir=out, mode="fixture", suite="dev")


def test_type_checking_imports_do_not_block_a_stdlib_workspace() -> None:
    guarded = {
        "src/pkg/mod.py": (
            "from typing import TYPE_CHECKING\n"
            "if TYPE_CHECKING:\n"
            "    import requests\n"
        )
    }
    assert _python_requirements(guarded) == []
    direct = {"src/pkg/mod.py": "import requests\n\n\ndef fetch():\n    return requests\n"}
    assert _python_requirements(direct) == ["requests"]


def test_adversarial_task_uses_real_sandbox_and_retries(tmp_path: Path) -> None:
    task = next(item for item in _dev() if item.adversarial)
    run_dir = tmp_path / task.task_id
    result = runner.run_task(
        task,
        experiment=EXPERIMENT,
        run_dir=run_dir,
        mode="fixture",
        provider=None,
    )
    assert result["status"] == grader.PASS
    assert result["mode"] == "fixture"
    assert result["gate"] == "engineering_gate"
    assert result["proposal"]["attempts"] == 2
    observation = result["adversarial_observation"]
    assert observation["retry_observed"] is True
    assert observation["accepted_attempt"] == 2
    assert observation["first_rejection_stage"] == "public_tests"
    assert len(result["rounds"]) == 2
    for round_ in result["rounds"]:
        assert {
            "reason",
            "tool_calls",
            "observation",
            "patch",
            "test",
            "feedback",
            "retry",
        } <= set(round_)
    assert result["rounds"][0]["retry"] is True
    assert result["rounds"][1]["test"]["hidden"]["status"] == "passed"
    assert result["fix"]["changed_files"] == ["src/click/core.py"]
    assert result["fix"]["unrelated_changes"] == []
    assert result["fix"]["out_of_scope"] == []
    assert result["publication"]["executed"] is False
    assert result["publication"]["correct"] is True
    assert result["usage"]["model_calls"] == 2

    probe = tmp_path / "resume-probe"
    shutil.copytree(run_dir / "ledger", probe / "ledger")
    state = runner.simulate_interrupted_resume(task, run_dir=probe)
    assert state["provider_calls"] == 0
    assert state["status"] != "waiting_approval"


def test_multifile_feature_changes_exactly_two_files(tmp_path: Path) -> None:
    task = next(item for item in _dev() if item.kind == "multi_file_feature")
    result = runner.run_task(
        task,
        experiment=EXPERIMENT,
        run_dir=tmp_path / task.task_id,
        mode="fixture",
        provider=None,
    )
    assert result["status"] == grader.PASS
    assert result["fix"]["changed_files"] == sorted(task.expected_changed_paths)
    assert len(result["fix"]["changed_files"]) == 2
    assert result["fix"]["hidden_passed"] is True
