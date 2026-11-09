import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mgar_v3.quality_eval import stats, validate  # noqa: E402

DATASET_HASH = "c" * 64
CONFIG_HASH = "d" * 64


def make(arm, case_id, status="PASS", **overrides):
    mapping = {
        "experiment_id": "exp-001",
        "baseline_commit": "1" * 40,
        "candidate_commit": "2" * 40,
        "dataset_hash": DATASET_HASH,
        "config_hash": CONFIG_HASH,
        "case_id": case_id,
        "arm": arm,
        "mode": "deterministic_test",
        "status": status,
        "failure_kind": (
            "business" if status == "FAIL" else "unknown" if status == "UNKNOWN" else None
        ),
        "latency_ms": 100.0,
        "model_calls": 1,
        "usage_source": "fixture",
        "evidence_refs": [],
        "cache_state": "warm",
        "not_run_reason": "not authorized" if status == "NOT_RUN" else None,
    }
    mapping.update(overrides)
    return mapping


def test_wilson_interval_matches_repository_known_vector():
    assert stats.wilson_interval(30, 30) == [0.8865, 1.0]
    assert stats.wilson_interval(0, 0) is None


def test_mcnemar_exact_known_values():
    assert stats.mcnemar_exact_two_sided(0, 0) == 1.0
    assert stats.mcnemar_exact_two_sided(5, 0) == 0.0625
    assert stats.mcnemar_exact_two_sided(1, 3) == 0.625


def test_percentile_linear_interpolation():
    assert stats.percentile([1, 2, 3, 4], 0.5) == 2.5
    assert stats.percentile([1, 2, 3, 4], 0.95) == pytest.approx(3.85)
    assert stats.percentile([7], 0.5) == 7.0
    assert stats.percentile([], 0.5) is None


def test_paired_bootstrap_is_deterministic_and_ordered():
    differences = [50.0, -10.0, 200.0, 0.0, 30.0]
    first = stats.paired_bootstrap_ci(differences, iterations=1000, seed=7)
    second = stats.paired_bootstrap_ci(differences, iterations=1000, seed=7)
    assert first == second
    assert first["n_pairs"] == 5
    assert first["mean_difference"] == pytest.approx(54.0)
    low, high = first["ci95"]
    assert low <= first["mean_difference"] <= high
    assert stats.paired_bootstrap_ci([1.0])["ci95"] is None


def test_summarize_excludes_not_run_and_unknown_from_denominator():
    records = [
        make("A", "c1"),  # PASS
        make("A", "c2", "FAIL"),  # FAIL
        make("A", "c3", "NOT_RUN"),
        make("A", "c4", "UNKNOWN"),
        make("A", "c5"),
    ]
    summary = stats.summarize(records)
    arm = summary["arms"]["A"]
    assert arm["status_counts"] == {"pass": 2, "fail": 1, "not_run": 1, "unknown": 1}
    assert arm["success_rate"]["numerator"] == 2
    assert arm["success_rate"]["denominator"] == 3
    assert "eligible cases" in arm["success_rate"]["scope"]
    assert arm["failure_kinds"] == {"business": 1, "unknown": 1}
    assert arm["not_run_reasons"] == {"not authorized": 1}
    assert summary["not_run_records"] == 1
    assert summary["unknown_records"] == 1
    assert arm["latency_by_cache"]["warm"]["n"] == 3


def test_summarize_is_reproducible_for_identical_input():
    records = [make("A", "c1"), make("A", "c2", "FAIL"), make("B", "c1")]
    first = stats.summarize(records)
    second = stats.summarize(list(records))
    assert first["summary_sha256"] == second["summary_sha256"]
    changed = stats.summarize([make("A", "c1", "FAIL")] + records[1:])
    assert changed["summary_sha256"] != first["summary_sha256"]


def test_summarize_rejects_mixed_experiments():
    records = [make("A", "c1"), make("A", "c2", experiment_id="exp-002")]
    with pytest.raises(validate.ValidationError):
        stats.summarize(records)


def test_compare_reports_paired_success_and_excludes_not_run():
    records = [
        make("A", "c1", "PASS"),
        make("A", "c2", "FAIL"),
        make("A", "c3", "NOT_RUN"),
        make("A", "c4", "PASS"),
        make("C", "c1", "PASS"),
        make("C", "c2", "PASS"),
        make("C", "c3", "PASS"),
        make("C", "c4", "FAIL"),
    ]
    comparison = stats.compare(records, "A", "C", iterations=1000, seed=11)
    assert comparison["coverage"]["pairs_total"] == 4
    assert comparison["coverage"]["pairs_eligible"] == 3
    assert comparison["coverage"]["pairs_excluded"] == 1
    assert comparison["coverage"]["exclusion_reasons"] == {"A=NOT_RUN/C=PASS": 1}
    assert comparison["success"]["both_pass"] == 1
    assert comparison["success"]["both_fail"] == 0
    assert comparison["success"]["only_A"] == 1
    assert comparison["success"]["only_C"] == 1
    assert comparison["success"]["baseline"]["numerator"] == 2
    assert comparison["success"]["candidate"]["numerator"] == 2
    assert comparison["success"]["difference_pp"] == 0.0
    assert comparison["success"]["mcnemar_exact_two_sided_p"] == 1.0
    assert comparison["decision"] == "NOT_EVALUATED_IN_QEV-02"
    assert comparison["latency_difference_by_cache"]["warm"]["n_pairs"] == 3


def test_compare_keeps_failure_records_and_reports_negative_difference():
    records = [
        make("A", "c1", "PASS"),
        make("A", "c2", "PASS"),
        make("C", "c1", "FAIL"),
        make("C", "c2", "PASS"),
    ]
    comparison = stats.compare(records, "A", "C", iterations=1000, seed=3)
    assert comparison["success"]["only_A"] == 1
    assert comparison["success"]["both_fail"] == 0
    assert comparison["success"]["difference_pp"] == pytest.approx(-50.0)
    assert comparison["success"]["mcnemar_exact_two_sided_p"] == 1.0


def test_compare_latency_does_not_mix_cache_states():
    records = [
        make("A", "c1", latency_ms=100.0, cache_state="warm"),
        make("A", "c2", latency_ms=50.0, cache_state="cold"),
        make("C", "c1", latency_ms=40.0, cache_state="warm"),
        make("C", "c2", latency_ms=30.0, cache_state="warm"),
    ]
    comparison = stats.compare(records, "A", "C", iterations=500, seed=5)
    assert comparison["cache_state_mismatch_pairs"] == 1
    assert set(comparison["latency_difference_by_cache"]) == {"warm"}
    assert comparison["latency_difference_by_cache"]["warm"]["n_pairs"] == 1


def test_compare_refuses_invalid_records():
    records = [make("A", "c1"), make("A", "c1")]
    with pytest.raises(validate.ValidationError):
        stats.compare(records, "A", "A")
