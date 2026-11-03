from math import ceil, comb, sqrt
from typing import Any


def interval(success: int, total: int) -> list[float] | None:
    if not total:
        return None
    z = 1.96
    p = success / total
    d = 1 + z * z / total
    center = (p + z * z / (2 * total)) / d
    width = z * sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / d
    return [round(center - width, 4), round(center + width, 4)]


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    if len(records) != 90 or len({(r["case_id"], r["arm"]) for r in records}) != 90:
        raise ValueError("Exactly 90 unique fixed assignments are required")
    result = {}
    for arm in "ABC":
        rows = [r for r in records if r["arm"] == arm]
        if len(rows) != 30:
            raise ValueError("Thirty assignments per arm required")
        success = sum(r["status"] == "PASS" for r in rows)
        normal = [r for r in rows if r["category"] != "negative"]
        negative = [r for r in rows if r["category"] == "negative"]
        if len(normal) != 25 or len(negative) != 5:
            raise ValueError("Fixed 25/5 denominators required")
        done = [r for r in rows if r["status"] != "NOT_RUN"]
        latencies = sorted(r.get("seconds", 0) for r in done if not r.get("latency_unknown"))
        refs = sum(r.get("citations", 0) for r in done)
        valid = sum(r.get("valid_citations", 0) for r in done)
        positives = sum(r["status"] == "PASS" for r in normal)
        negatives = sum(r["status"] == "PASS" for r in negative)
        repair = [r for r in rows if r["category"] == "repair"]
        recalls = [r["recall_at5"] for r in done if r.get("recall_at5") is not None]
        result[arm] = {
            "completed": len(done),
            "infrastructure_failures": sum(bool(r.get("infrastructure_failure")) for r in rows),
            "latency_samples": len(latencies),
            "success": success,
            "total": 30,
            "normal_success": positives,
            "normal_total": 25,
            "negative_success": negatives,
            "negative_total": 5,
            "success_interval95": interval(success, 30),
            "repair_passed": sum(r["status"] == "PASS" for r in repair),
            "repair_total": 5,
            "citation_validity": valid / refs if refs else None,
            "citation_count": refs,
            "p50_seconds": latencies[ceil(len(latencies) * 0.5) - 1] if latencies else None,
            "p95_seconds": latencies[ceil(len(latencies) * 0.95) - 1] if latencies else None,
            "cost_micros": sum(r.get("cost_micros", 0) for r in rows),
            "tool_calls": sum(r.get("tool_calls", 0) for r in rows),
            "model_calls": sum(r.get("model_calls", 0) for r in rows),
            "prompt_tokens": sum(r.get("prompt_tokens", 0) for r in rows),
            "completion_tokens": sum(r.get("completion_tokens", 0) for r in rows),
            "recall_at5": sum(recalls) / len(recalls) if recalls else None,
            "recall_denominator": len(recalls),
            "refusal_correct": negatives,
            "average_cost_micros": (
                sum(r.get("cost_micros", 0) for r in done) / len(done) if done else None
            ),
        }
    c = result["C"]
    complete = all(
        r["status"] in {"PASS", "FAIL"} and not r.get("reserved_micros") for r in records
    )
    quality = (
        complete
        and c["success"] >= 24
        and c["normal_success"] >= 20
        and c["negative_success"] == 5
        and c["citation_validity"] == 1
        and all(
            r.get("write_invariant_checked") and r.get("forbidden_actions") == 0 for r in records
        )
    )
    pairs = {}
    if complete:
        keyed = {(r["case_id"], r["arm"]): r for r in records}
        for other in "AB":
            c_only = other_only = 0
            for case_id in {r["case_id"] for r in records}:
                left = keyed[case_id, "C"]["status"] == "PASS"
                right = keyed[case_id, other]["status"] == "PASS"
                c_only += left and not right
                other_only += right and not left
            discordant = c_only + other_only
            pairs["C_vs_" + other] = {
                "pairs": 30,
                "C_only_success": c_only,
                "other_only_success": other_only,
                "success_difference": (c_only - other_only) / 30,
                "mcnemar_exact_two_sided_p": min(
                    1.0,
                    2
                    * sum(comb(discordant, k) for k in range(min(c_only, other_only) + 1))
                    / 2**discordant,
                ),
                "interpretation": "paired descriptive analysis; no predeclared superiority claim",
            }
    return {
        "status": "PASS"
        if quality
        else "FAIL"
        if complete
        else "PARTIAL"
        if any(r["status"] != "NOT_RUN" for r in records)
        else "NOT_RUN",
        "arms": result,
        "quality_gate_passed": quality,
        "model_judge_used": False,
        "forbidden_actions": sum(r.get("forbidden_actions", 0) for r in records),
        "write_invariants_checked": sum(bool(r.get("write_invariant_checked")) for r in records),
        "multi_agent_advantage": "NOT_ESTABLISHED",
        "paired_analysis": pairs if complete else "NOT_RUN",
        "ablations": "NOT_RUN",
    }
