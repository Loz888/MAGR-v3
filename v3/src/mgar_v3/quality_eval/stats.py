"""Offline statistics for quality evaluation experiments (QEV-02).

Definitions used consistently here:

* eligible case = status in {PASS, FAIL}. NOT_RUN and UNKNOWN are counted
  separately and never enter a success numerator or an eligible denominator.
* every rate is reported with numerator, denominator and scope.
* latency P50/P95 use linear interpolation.
* paired differences use an exact McNemar test (binary) or a paired bootstrap
  with a fixed seed (continuous).
"""

from __future__ import annotations

from collections import Counter
from math import ceil, comb, floor, sqrt
from random import Random
from typing import Any, Iterable, Mapping, Sequence

from mgar_v3.quality_eval import validate
from mgar_v3.quality_eval.record import CACHE_STATES, canonical_digest

SUMMARY_SCHEMA = "mgar-quality-eval-summary-v1"
COMPARISON_SCHEMA = "mgar-quality-eval-comparison-v1"
BOOTSTRAP_ITERATIONS = 10000
BOOTSTRAP_SEED = 20261108


def wilson_interval(success: int, total: int, z: float = 1.96) -> list[float] | None:
    """Wilson score interval; identical formula to benchmark.metrics.interval."""
    if not total:
        return None
    p = success / total
    d = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / d
    width = z * sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / d
    return [round(centre - width, 4), round(centre + width, 4)]


def mcnemar_exact_two_sided(only_a: int, only_b: int) -> float:
    """Exact two-sided McNemar test on discordant pairs (no continuity correction)."""
    discordant = only_a + only_b
    if not discordant:
        return 1.0
    tail = sum(comb(discordant, k) for k in range(min(only_a, only_b) + 1))
    return min(1.0, 2 * tail / 2**discordant)


def percentile(values: Sequence[float], q: float) -> float | None:
    """Linear-interpolation percentile (numpy 'linear' convention)."""
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * q
    lower = floor(position)
    upper = ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def latency_summary(values: Sequence[float], scope: str) -> dict[str, Any]:
    return {
        "n": len(values),
        "p50_ms": percentile(values, 0.5),
        "p95_ms": percentile(values, 0.95),
        "method": "linear_interpolation",
        "scope": scope,
    }


def rate(success: int, denominator: int, scope: str) -> dict[str, Any]:
    return {
        "numerator": success,
        "denominator": denominator,
        "value": success / denominator if denominator else None,
        "interval95": wilson_interval(success, denominator),
        "scope": scope,
    }


def paired_bootstrap_ci(
    differences: Sequence[float],
    *,
    iterations: int = BOOTSTRAP_ITERATIONS,
    seed: int = BOOTSTRAP_SEED,
    level: float = 0.95,
) -> dict[str, Any]:
    """Paired bootstrap percentile CI for the mean difference (fixed seed)."""
    if len(differences) < 2:
        return {
            "n_pairs": len(differences),
            "mean_difference": (sum(differences) / len(differences)) if differences else None,
            "ci95": None,
            "iterations": iterations,
            "seed": seed,
            "method": "paired_bootstrap_percentile",
            "note": "at least two pairs are required for an interval",
        }
    rng = Random(seed)
    size = len(differences)
    means = []
    for _ in range(iterations):
        total = 0.0
        for _ in range(size):
            total += differences[rng.randrange(size)]
        means.append(total / size)
    alpha = (1 - level) / 2
    return {
        "n_pairs": size,
        "mean_difference": sum(differences) / size,
        "ci95": [percentile(means, alpha), percentile(means, 1 - alpha)],
        "iterations": iterations,
        "seed": seed,
        "method": "paired_bootstrap_percentile",
    }


def _eligible(rows: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [row for row in rows if row["status"] in {"PASS", "FAIL"}]


def summarize_arm(rows: Sequence[Mapping[str, Any]], arm: str) -> dict[str, Any]:
    rows = [row for row in rows if str(row["arm"]) == arm]
    counts = Counter(str(row["status"]) for row in rows)
    eligible = _eligible(rows)
    success = counts["PASS"]

    latency_by_cache: dict[str, Any] = {}
    for state in CACHE_STATES:
        values = [
            float(row["latency_ms"])
            for row in eligible
            if row.get("cache_state", "none") == state and row.get("latency_ms") is not None
        ]
        if values:
            latency_by_cache[state] = latency_summary(
                values,
                scope=f"completed cases (PASS|FAIL) with cache_state={state}",
            )

    total_cost = sum(int(row.get("cost_micros", 0)) for row in rows)
    eligible_cost = sum(int(row.get("cost_micros", 0)) for row in eligible)
    return {
        "arm": arm,
        "records": len(rows),
        "case_count": len({str(row["case_id"]) for row in rows}),
        "status_counts": {
            "pass": counts["PASS"],
            "fail": counts["FAIL"],
            "not_run": counts["NOT_RUN"],
            "unknown": counts["UNKNOWN"],
        },
        "success_rate": rate(
            success,
            len(eligible),
            "eligible cases = status PASS or FAIL; NOT_RUN and UNKNOWN excluded",
        ),
        "failure_rate": rate(
            counts["FAIL"],
            len(eligible),
            "eligible cases = status PASS or FAIL; NOT_RUN and UNKNOWN excluded",
        ),
        "failure_kinds": dict(
            Counter(str(row["failure_kind"]) for row in rows if row.get("failure_kind"))
        ),
        "not_run_reasons": dict(
            Counter(str(row.get("not_run_reason")) for row in rows if row["status"] == "NOT_RUN")
        ),
        "latency_by_cache": latency_by_cache,
        "totals": {
            "model_calls": sum(int(row.get("model_calls", 0)) for row in rows),
            "prompt_tokens": sum(int(row.get("prompt_tokens", 0)) for row in rows),
            "completion_tokens": sum(int(row.get("completion_tokens", 0)) for row in rows),
            "cost_micros": total_cost,
        },
        "usage_sources": dict(Counter(str(row["usage_source"]) for row in rows)),
        "cost_per_completed_case_micros": {
            "numerator": eligible_cost,
            "denominator": len(eligible),
            "value": eligible_cost / len(eligible) if eligible else None,
            "scope": "sum(cost_micros) of eligible cases divided by eligible case count",
        },
    }


def summarize(
    records: Sequence[Mapping[str, Any]],
    *,
    iterations: int = BOOTSTRAP_ITERATIONS,
    seed: int = BOOTSTRAP_SEED,
) -> dict[str, Any]:
    records = list(records)
    validate.require_valid(records)
    for field, label in (
        ("experiment_id", "experiment_id"),
        ("dataset_hash", "dataset_hash"),
        ("config_hash", "config_hash"),
    ):
        values = {str(record[field]) for record in records}
        if len(values) != 1:
            raise validate.ValidationError(
                [f"records disagree on {label}: {sorted(values)}"]
            )

    arms = sorted({str(record["arm"]) for record in records})
    payload: dict[str, Any] = {
        "schema": SUMMARY_SCHEMA,
        "experiment_id": records[0]["experiment_id"],
        "dataset_hash": records[0]["dataset_hash"],
        "config_hash": records[0]["config_hash"],
        "record_count": len(records),
        "case_count": len({str(record["case_id"]) for record in records}),
        "arms": {arm: summarize_arm(records, arm) for arm in arms},
        "not_run_records": sum(1 for record in records if record["status"] == "NOT_RUN"),
        "unknown_records": sum(1 for record in records if record["status"] == "UNKNOWN"),
        "method": {
            "interval": "wilson_95",
            "percentile": "linear_interpolation",
            "bootstrap_iterations": iterations,
            "bootstrap_seed": seed,
            "excluded_from_denominator": ["NOT_RUN", "UNKNOWN"],
        },
    }
    payload["summary_sha256"] = canonical_digest(payload)
    return payload


def compare(
    records: Sequence[Mapping[str, Any]],
    baseline: str,
    candidate: str,
    *,
    iterations: int = BOOTSTRAP_ITERATIONS,
    seed: int = BOOTSTRAP_SEED,
) -> dict[str, Any]:
    records = list(records)
    meta = validate.require_comparable(records, baseline, candidate)
    index = {
        (str(record["arm"]), str(record["case_id"]), int(record.get("repeat_index", 0))): record
        for record in records
        if str(record["arm"]) in {baseline, candidate}
    }
    pair_keys = sorted(
        (case_id, repeat)
        for arm, case_id, repeat in index
        if arm == baseline and (candidate, case_id, repeat) in index
    )

    eligible: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = []
    exclusions = Counter()
    for case_id, repeat in pair_keys:
        left = index[baseline, case_id, repeat]
        right = index[candidate, case_id, repeat]
        if left["status"] in {"PASS", "FAIL"} and right["status"] in {"PASS", "FAIL"}:
            eligible.append((left, right))
        else:
            exclusions[f"{baseline}={left['status']}/{candidate}={right['status']}"] += 1

    both_pass = both_fail = only_baseline = only_candidate = 0
    for left, right in eligible:
        left_pass = left["status"] == "PASS"
        right_pass = right["status"] == "PASS"
        both_pass += left_pass and right_pass
        both_fail += (not left_pass) and (not right_pass)
        only_baseline += left_pass and not right_pass
        only_candidate += right_pass and not left_pass

    denominator = len(eligible)
    baseline_rate = both_pass + only_baseline
    candidate_rate = both_pass + only_candidate
    mcnemar_p = mcnemar_exact_two_sided(only_baseline, only_candidate)

    latency_by_cache: dict[str, Any] = {}
    matched = [
        (left, right)
        for left, right in eligible
        if left.get("cache_state", "none") == right.get("cache_state", "none")
    ]
    cache_mismatch = len(eligible) - len(matched)
    for state in CACHE_STATES:
        differences = [
            float(right["latency_ms"]) - float(left["latency_ms"])
            for left, right in matched
            if left.get("cache_state", "none") == state
            and left.get("latency_ms") is not None
            and right.get("latency_ms") is not None
        ]
        if differences:
            summary = paired_bootstrap_ci(differences, iterations=iterations, seed=seed)
            summary["scope"] = (
                f"eligible pairs with cache_state={state}; candidate minus baseline latency_ms"
            )
            latency_by_cache[state] = summary

    cost_differences = [
        float(right.get("cost_micros", 0)) - float(left.get("cost_micros", 0))
        for left, right in eligible
    ]
    cost_difference = paired_bootstrap_ci(cost_differences, iterations=iterations, seed=seed)
    cost_difference["scope"] = "eligible pairs; candidate minus baseline cost_micros per case"

    payload: dict[str, Any] = {
        "schema": COMPARISON_SCHEMA,
        "experiment_id": meta["experiment_id"],
        "dataset_hash": meta["dataset_hash"],
        "config_hash": meta["config_hash"],
        "baseline": baseline,
        "candidate": candidate,
        "case_count": meta["case_count"],
        "coverage": {
            "pairs_total": len(pair_keys),
            "pairs_eligible": denominator,
            "pairs_excluded": len(pair_keys) - denominator,
            "exclusion_reasons": dict(exclusions),
            "scope": "all paired (case_id, repeat_index) keys present in both arms",
        },
        "success": {
            "baseline": rate(
                baseline_rate,
                denominator,
                "eligible paired cases where both arms have status PASS or FAIL",
            ),
            "candidate": rate(
                candidate_rate,
                denominator,
                "eligible paired cases where both arms have status PASS or FAIL",
            ),
            "difference_pp": (
                (candidate_rate - baseline_rate) / denominator * 100 if denominator else None
            ),
            "both_pass": both_pass,
            "both_fail": both_fail,
            f"only_{baseline}": only_baseline,
            f"only_{candidate}": only_candidate,
            "mcnemar_exact_two_sided_p": mcnemar_p,
            "method": "exact McNemar on discordant pairs; no continuity correction",
        },
        "latency_difference_by_cache": latency_by_cache,
        "cache_state_mismatch_pairs": cache_mismatch,
        "cost_difference": cost_difference,
        "decision": "NOT_EVALUATED_IN_QEV-02",
        "decision_note": (
            "QEV-02 provides measurement infrastructure only; no rollout threshold "
            "was predeclared and no recommendation is made here"
        ),
    }
    payload["comparison_sha256"] = canonical_digest(payload)
    return payload
