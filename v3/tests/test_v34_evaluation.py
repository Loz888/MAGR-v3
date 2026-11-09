"""E01 v3.4 real-repository evaluation gates.

These tests assert the offline dataset, grading math and honesty gates. They do
not call a model and they never turn a NOT_RUN arm into a capability score.
"""

import hashlib
import json
import sys
from pathlib import Path

import pytest

V3 = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(V3 / "src"))

from mgar_v3.quality_eval import v34  # noqa: E402
from mgar_v3.quality_eval.manifest import verify_manifest  # noqa: E402
from mgar_v3.quality_eval.record import canonical_digest  # noqa: E402

EXPERIMENT = V3 / "experiments" / "v34-audit-repair"


def load_manifest():
    return json.loads((EXPERIMENT / "manifest.json").read_text(encoding="utf-8"))


def load_records():
    return v34.load_jsonl(EXPERIMENT / "cases.jsonl")


def load_parts():
    manifest = load_manifest()
    tasks, holdout = v34.load_tasks(EXPERIMENT, manifest)
    repositories = json.loads((EXPERIMENT / "repositories.json").read_text(encoding="utf-8"))
    return manifest, tasks, holdout, repositories


def review_task(tasks, task_id="click-sentinel-identity"):
    return next(task for task in tasks if task["task_id"] == task_id)


def answer_from_gold(task, finding):
    return {
        "findings": [
            {
                "file": finding["file"],
                "symbol": finding["symbol"],
                "category": finding["category"],
                "call_chain": list(finding["call_chain"]),
                "quote": finding["quote"],
            }
        ],
        "coverage": {
            "scanned_files": list(task["scope_paths"]),
            "scannable_files": list(task["scope_paths"]),
        },
    }


# ---------------------------------------------------------------- dataset shape


def test_dataset_meets_requirement_floor():
    _, tasks, holdout, repositories = load_parts()
    entries = repositories["repositories"]
    assert len(entries) >= 3
    assert {entry["language"] for entry in entries} == {"python", "go"}
    assert all(entry["url"].startswith("https://github.com/") for entry in entries)
    assert all(entry["license"] for entry in entries)

    all_tasks = tasks + holdout
    kinds = [task["kind"] for task in all_tasks]
    assert kinds.count("review") >= 10
    assert kinds.count("repair") >= 3
    assert kinds.count("negative") >= 2
    assert len({task["repo_id"] for task in all_tasks}) >= 3
    assert len(tasks) == 15 and len(holdout) == 3


def test_every_task_pins_a_real_snapshot():
    _, tasks, holdout, repositories = load_parts()
    commits = {
        entry["repo_id"]: {snapshot["commit"] for snapshot in entry["snapshots"]}
        for entry in repositories["repositories"]
    }
    for task in tasks + holdout:
        assert len(task["commit"]) == 40
        assert task["commit"] in commits[task["repo_id"]]
        assert (EXPERIMENT / task["snapshot_dir"]).is_dir()
        assert task["snapshot_dir"].endswith(task["commit"])


def test_snapshot_files_and_quotes_verify():
    manifest, tasks, holdout, repositories = load_parts()
    assert v34.validate_bundle(EXPERIMENT, manifest, check_files=True) == []
    assert v34.validate_repositories(EXPERIMENT, repositories, check_files=True) == []
    assert (
        v34.validate_tasks(EXPERIMENT, tasks, repositories, holdout=False, check_files=True) == []
    )
    assert (
        v34.validate_tasks(EXPERIMENT, holdout, repositories, holdout=True, check_files=True) == []
    )


def test_negative_tasks_are_verified_absent():
    _, tasks, holdout, _ = load_parts()
    negatives = [task for task in tasks + holdout if task["kind"] == "negative"]
    assert len(negatives) >= 2
    for task in negatives:
        assert task["gold_findings"] == []
        assert task["negative"]["forbidden_patterns"]
    for task in [item for item in negatives if not item["holdout"]]:
        assert v34.verify_task(EXPERIMENT, task)["status"] == "PASS"


# ---------------------------------------------------------------- holdout split


def test_holdout_is_isolated_from_development():
    manifest, tasks, holdout, _ = load_parts()
    dev_ids = {task["task_id"] for task in tasks}
    holdout_ids = {task["task_id"] for task in holdout}
    assert dev_ids.isdisjoint(holdout_ids)
    assert holdout_ids == set(manifest["v34"]["holdout_ids"])
    assert all(task["holdout"] is True for task in holdout)
    assert all(task["holdout"] is False for task in tasks)

    selftest = json.loads((EXPERIMENT / "harness-selftest.json").read_text(encoding="utf-8"))
    assert selftest["holdout_executed"] is False
    assert selftest["holdout_task_count"] == len(holdout)
    assert set(selftest["tasks"]) == dev_ids
    assert selftest["scope"] == "harness_conformance_not_model_capability"


def test_committed_selftest_matches_recomputation():
    manifest = load_manifest()
    committed = json.loads((EXPERIMENT / "harness-selftest.json").read_text(encoding="utf-8"))
    recomputed = v34.run_selftest(EXPERIMENT, manifest)
    assert recomputed == committed
    assert committed["status_counts"] == {"PASS": 15, "FAIL": 0, "SKIPPED": 0}


# ---------------------------------------------------------------- record honesty


def test_live_arms_are_honest():
    records = load_records()
    assert len(records) == 54
    assert {record["arm"] for record in records} == {"A", "B", "C"}
    executed = [record for record in records if record["status"] in {"PASS", "FAIL"}]
    assert executed, "no arm was executed against the provider"
    for record in records:
        assert record["mode"] == "real_model_live"
        if record["status"] == "NOT_RUN":
            assert record["model_calls"] == 0
            assert record["usage_source"] == "not_applicable"
            assert record["not_run_reason"]
            assert record["failure_kind"] is None
            assert record["latency_ms"] is None
            continue
        if record["failure_kind"] == "infrastructure":
            assert record["usage_source"] == "not_applicable"
            assert record["model_calls"] == 0
            assert record["latency_ms"] is None
            continue
        assert record["failure_kind"] in {None, "business"}
        assert record["model_calls"] == 1
        assert record["usage_source"] == "actual_provider_usage"
        assert record["latency_ms"] and record["latency_ms"] > 0
        assert record["prompt_tokens"] > 0
        assert record["completion_tokens"] > 0
        assert record["cost_micros"] > 0
        assert record["input_sha256"] and record["output_sha256"]
        assert record["input_ref"] and record["output_ref"]
        input_path = EXPERIMENT / record["input_ref"]
        output_path = EXPERIMENT / record["output_ref"]
        assert input_path.is_file(), record["input_ref"]
        assert output_path.is_file(), record["output_ref"]
        assert hashlib.sha256(input_path.read_bytes()).hexdigest() == record["input_sha256"]
        content = json.loads(output_path.read_text(encoding="utf-8"))["content"]
        assert hashlib.sha256(content.encode()).hexdigest() == record["output_sha256"]
    assert sum(record["model_calls"] for record in records) == len(executed)
    assert sum(record["cost_micros"] for record in records) <= 2000000


def test_abc_pairing_is_complete():
    _, tasks, holdout, _ = load_parts()
    records = load_records()
    coverage = v34.pair_coverage(records)
    assert coverage["complete"] is True
    assert coverage["pairs"] == len(tasks) + len(holdout)
    all_ids = {task["task_id"] for task in tasks + holdout}
    for arm in ("A", "B", "C"):
        arm_records = [record for record in records if record["arm"] == arm]
        assert {record["case_id"] for record in arm_records} == all_ids


def test_manifest_and_case_files_agree():
    manifest = load_manifest()
    records = load_records()
    assert verify_manifest(manifest, root=V3, check_files=True)["errors"] == []
    assert manifest["dataset"]["count"] == 18
    assert {record["case_id"] for record in records} == set(manifest["dataset"]["case_ids"])
    assert {record["dataset_hash"] for record in records} == {manifest["dataset"]["hash"]}
    assert {record["config_hash"] for record in records} == {manifest["config"]["hash"]}
    assert manifest["authorization"] == {
        "external_writes": False,
        "paid_models": True,
        "budget_usd": 2.0,
        "production_actions": False,
    }
    digest = canonical_digest(
        {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    )
    assert digest == manifest["manifest_sha256"]


def test_config_declares_required_metrics():
    config = json.loads((EXPERIMENT / "config" / "evaluation.json").read_text(encoding="utf-8"))
    required = {
        "finding_precision_at_k",
        "recall_at_budget",
        "unverified_false_positives",
        "source_citation_validity",
        "call_chain_correctness",
        "bug_reproduction_success",
        "patch_validation_success",
        "independent_test_pass",
        "regression_failures",
        "draft_pr_correctness",
        "cost_micros",
        "latency_ms",
        "model_calls",
        "tool_calls",
        "memory_recall_at_k",
        "stale_rejection",
        "contamination_rate",
        "token_reduction",
        "repeated_tool_call_reduction",
        "correctness_with_memory",
        "correctness_without_memory",
        "retrieval_cost",
    }
    assert required <= set(config["metrics"])
    assert config["authorization"]["live_arms_allowed"] is True
    assert config["experiment_budget"]["limit_micros"] == 2000000
    assert set(config["arms"]) == {"A", "B", "C"}
    assert config["arms"]["A"]["memory"] == "disabled"
    assert config["arms"]["C"]["compression"] == "enabled"


# ---------------------------------------------------------------- grading math


def test_review_grading_perfect_answer():
    manifest, tasks, _, _ = load_parts()
    task = review_task(tasks)
    metrics = v34.grade_review(
        task, answer_from_gold(task, task["gold_findings"][0]), experiment_dir=EXPERIMENT
    )
    assert metrics["precision_at_k"] == 1.0
    assert metrics["recall_at_budget"] == 1.0
    assert metrics["unverified_false_positives"] == 0
    assert metrics["source_citation_validity"] == 1.0
    assert metrics["call_chain_correctness"] == 1.0
    assert metrics["scan_coverage"] == 1.0
    assert metrics["passed"] is True


def test_review_grading_penalizes_unverifiable_extra_finding():
    _, tasks, _, _ = load_parts()
    task = review_task(tasks)
    answer = answer_from_gold(task, task["gold_findings"][0])
    answer["findings"].append(
        {
            "file": "src/click/core.py",
            "symbol": "click.core.Command.invoke",
            "category": "shell_command_execution",
            "call_chain": ["click.core.Command.invoke", "subprocess.run"],
            "quote": "this quote does not exist in the pinned file",
        }
    )
    metrics = v34.grade_review(task, answer, experiment_dir=EXPERIMENT)
    assert metrics["unverified_false_positives"] == 1
    assert metrics["precision_at_k"] == 0.5
    assert metrics["source_citation_validity"] == 0.5
    assert metrics["passed"] is False


def test_review_grading_recall_uses_gold_denominator():
    _, tasks, _, _ = load_parts()
    task = review_task(tasks, "pflag-native-int-overflow")
    assert len(task["gold_findings"]) == 2
    metrics = v34.grade_review(
        task, answer_from_gold(task, task["gold_findings"][0]), experiment_dir=EXPERIMENT
    )
    assert metrics["recall_at_budget"] == 0.5
    assert metrics["passed"] is False


def test_negative_grading_rejects_fabricated_finding():
    _, tasks, _, _ = load_parts()
    task = next(item for item in tasks if item["kind"] == "negative")
    coverage = {
        "scanned_files": task["scope_paths"],
        "scannable_files": task["scope_paths"],
    }
    empty = v34.grade_review(
        task,
        {"findings": [], "coverage": coverage},
        experiment_dir=EXPERIMENT,
    )
    assert empty["recall_at_budget"] == 1.0
    assert empty["passed"] is True

    fabricated = v34.grade_review(
        task,
        {
            "findings": [
                {
                    "file": task["negative"]["scope_files"][0],
                    "symbol": "made.up",
                    "category": "shell_command_execution",
                    "call_chain": ["a", "b"],
                    "quote": "not in the file",
                }
            ],
            "coverage": coverage,
        },
        experiment_dir=EXPERIMENT,
    )
    assert fabricated["passed"] is False
    assert fabricated["unverified_false_positives"] == 1


def test_repair_grading_requires_all_binary_gates():
    _, tasks, _, _ = load_parts()
    task = next(item for item in tasks if item["kind"] == "repair")
    good = {
        "bug_reproduction_success": True,
        "patch_validation_success": True,
        "independent_test_pass": True,
        "draft_pr_correctness": True,
        "regression_failures": 0,
        "latency_ms": 1234.0,
        "model_calls": 3,
        "tool_calls": 7,
        "cost_micros": 42,
        "prompt_tokens": 10,
        "completion_tokens": 5,
    }
    metrics = v34.grade_repair(task, good)
    assert metrics["passed"] is True
    assert metrics["tool_calls"] == 7
    for gate in (
        "bug_reproduction_success",
        "patch_validation_success",
        "independent_test_pass",
        "draft_pr_correctness",
    ):
        broken = dict(good, **{gate: False})
        assert v34.grade_repair(task, broken)["passed"] is False
    assert v34.grade_repair(task, dict(good, regression_failures=1))["passed"] is False


def test_memory_metrics_are_computed_from_answer():
    case = {
        "case_id": "mem-1",
        "k": 2,
        "relevant_memory_ids": ["m1", "m2"],
        "stale_memory_ids": ["m3", "m5"],
        "contaminated_memory_ids": ["m4"],
        "baseline_context_tokens": 1000,
        "baseline_repeated_tool_calls": 10,
    }
    answer = {
        "retrieved": [
            {"memory_id": "m1"},
            {"memory_id": "m2"},
            {"memory_id": "m3"},
            {"memory_id": "m4"},
        ],
        "compressed_context_tokens": 600,
        "repeated_tool_calls": 4,
        "retrieval_tokens": 120,
        "retrieval_calls": 2,
        "retrieval_cost_micros": 9,
    }
    metrics = v34.grade_memory(case, answer)
    assert metrics["recall_at_k"] == 1.0
    assert metrics["stale_rejection"] == 0.5
    assert metrics["contamination_rate"] == 0.25
    assert metrics["token_reduction"] == pytest.approx(0.4)
    assert metrics["repeated_tool_call_reduction"] == pytest.approx(0.6)
    assert metrics["retrieval_cost"] == {"tokens": 120, "calls": 2, "cost_micros": 9}


def test_correctness_delta_pairs_by_case():
    records = [
        {"case_id": "c1", "arm": "A", "status": "PASS"},
        {"case_id": "c2", "arm": "A", "status": "FAIL"},
        {"case_id": "c1", "arm": "C", "status": "PASS"},
        {"case_id": "c2", "arm": "C", "status": "PASS"},
    ]
    delta = v34.correctness_delta(records)
    assert delta["without_memory"] == 0.5
    assert delta["with_memory"] == 1.0
    assert delta["difference_pp"] == pytest.approx(50.0)
    assert delta["pairs"] == 2


# ---------------------------------------------------------------- patch safety


def test_patch_application_and_traversal_guard(tmp_path):
    target = tmp_path / "mod.py"
    target.write_text("value = 1\n", encoding="utf-8")
    patch = (
        "diff --git a/mod.py b/mod.py\n"
        "--- a/mod.py\n"
        "+++ b/mod.py\n"
        "@@ -1 +1 @@\n"
        "-value = 1\n"
        "+value = 2\n"
    )
    assert v34.apply_unified_patch(patch, tmp_path) == ["mod.py"]
    assert target.read_text(encoding="utf-8") == "value = 2\n"

    escaping = (
        "diff --git a/../escape.py b/../escape.py\n"
        "--- a/../escape.py\n"
        "+++ b/../escape.py\n"
        "@@ -1 +1 @@\n"
        "-value = 1\n"
        "+value = 2\n"
    )
    with pytest.raises(v34.EvaluationError):
        v34.apply_unified_patch(escaping, tmp_path)
