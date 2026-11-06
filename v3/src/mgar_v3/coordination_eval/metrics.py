"""Independent reporting: expected answers are never loaded by the execution path."""

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from mgar_v3.coordination_eval.provider import write_json
from mgar_v3.coordination_eval.store import DATA
from mgar_v3.settings import ROOT


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    pos = (len(ordered) - 1) * q
    left = int(pos)
    right = min(left + 1, len(ordered) - 1)
    return round(ordered[left] + (ordered[right] - ordered[left]) * (pos - left), 6)


def duration(start: str, end: str) -> float:
    result = (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()
    if result < 0:
        raise ValueError("Observed timestamp moved backwards; do not clip latency")
    return result


def grade(raw: dict[str, Any], oracle: dict[str, Any]) -> dict[str, Any]:
    events = {e["event_key"]: e for e in raw["events"]}
    runs = {r["run_id"]: r for r in raw["runs"]}
    normal = []
    citation_valid, citation_total = 0, 0
    start_latency, completion_latency, dispatch_latency = [], [], []
    for event in events.values():
        if event["completed_at"]:
            dispatch_latency.append(duration(event["received_at"], event["completed_at"]))
        mailbox = next((m for m in raw["mailbox"] if m["event_id"] == event["event_id"]), None)
        if mailbox and mailbox["read_at"]:
            start_latency.append(duration(event["received_at"], mailbox["read_at"]))
    for key, required in oracle["required_quotes"].items():
        event = events.get(key)
        run = runs.get(event["run_id"]) if event else None
        calls = [c for c in raw["calls"] if run and c["run_id"] == run["run_id"]]
        claims = [cl for c in calls if c.get("result") for cl in c["result"].get("claims", [])]
        own_evidence = {
            e["evidence_id"]: e for e in raw["evidence"] if run and e["run_id"] == run["run_id"]
        }
        valid_claims = []
        for claim in claims:
            citation_total += 1
            evidence = own_evidence.get(claim.get("evidence_id"))
            source = raw["source_artifacts"].get(evidence["content_hash"]) if evidence else None
            valid = bool(
                source
                and evidence is not None
                and claim["quote"] in source
                and hashlib.sha256(source.encode()).hexdigest() == evidence["content_hash"]
            )
            valid_claims.append(valid)
            citation_valid += valid
        mailbox = [
            m
            for m in raw["mailbox"]
            if run and m["run_id"] == run["run_id"] and m["kind"] == "user"
        ]
        actual_keys = [m["external_event_id"].rsplit(":", 1)[-1] for m in mailbox]
        expected_keys = oracle["expected_user_mailbox"][key]
        consumed = bool(mailbox) and all(m["consumed_at"] for m in mailbox)
        quote_pass = any(
            c.get("statement") == required and c.get("quote") == required for c in claims
        )
        success = bool(
            run
            and run["status"] == "succeeded"
            and run["outcome"] == "internal_report"
            and quote_pass
            and bool(valid_claims)
            and all(valid_claims)
            and actual_keys == expected_keys
            and consumed
        )
        normal.append(
            {
                "event_key": key,
                "success": success,
                "required_quote_present": quote_pass,
                "user_mailbox_keys": actual_keys,
                "all_user_input_consumed": consumed,
            }
        )
        if run and event:
            done = next(
                (
                    e
                    for e in raw["run_events"]
                    if e["run_id"] == run["run_id"] and e["kind"] == "readonly_report_completed"
                ),
                None,
            )
            if done:
                completion_latency.append(duration(event["received_at"], done["created_at"]))
    expected_routes = oracle["expected_routes"]
    route_success = sum(
        k in events and events[k]["action"] == action for k, action in expected_routes.items()
    )
    negative = sum(
        events.get(k, {}).get("action") == expected_routes[k] for k in ("e07", "e08", "e09")
    )
    known = [c for c in raw["calls"] if c["status"] == "known"]
    unknown = [c for c in raw["calls"] if c["status"] != "known"]
    tokens = {
        k: sum(int(c.get("usage", {}).get(k, 0)) for c in known)
        for k in (
            "prompt_tokens",
            "completion_tokens",
            "prompt_cache_hit_tokens",
            "prompt_cache_miss_tokens",
        )
    }
    duplicates = sum(a["kind"] == "duplicate" for a in raw["audit"])
    lost = sum(k not in events or events[k]["action"] is None for k in expected_routes)
    route_durations = []
    for call in known:
        if not call["step_key"].startswith("coord-route:"):
            continue
        reserved = next(
            (
                e
                for e in raw["run_events"]
                if e["kind"] == "model_reserved"
                and e["safe_payload"].get("call_id") == call["call_id"]
            ),
            None,
        )
        settled = next(
            (
                e
                for e in raw["run_events"]
                if e["kind"] == "model_settled"
                and e["safe_payload"].get("call_id") == call["call_id"]
            ),
            None,
        )
        if reserved and settled:
            route_durations.append(duration(reserved["created_at"], settled["created_at"]))
    return {
        "trial_id": raw["trial"]["id"],
        "arm": raw["trial"]["arm"],
        "cache": raw["trial"]["cache"],
        "repetition": raw["trial"]["repetition"],
        "normal": normal,
        "normal_passed": sum(x["success"] for x in normal),
        "normal_total": 2,
        "negative_passed": negative,
        "negative_total": 3,
        "route_passed": route_success,
        "route_total": len(expected_routes),
        "physical_deliveries": sum(a["kind"] == "physical_delivery" for a in raw["audit"]),
        "duplicates": duplicates,
        "duplicate_count_correct": duplicates == oracle["expected_duplicates"],
        "lost_accepted_events": lost,
        "forbidden_effects": raw["forbidden_effects"],
        "citation_valid": citation_valid,
        "citation_total": citation_total,
        "calls": len(raw["calls"]),
        "router_calls": sum(c["step_key"].startswith("coord-route:") for c in raw["calls"]),
        "worker_calls": sum(not c["step_key"].startswith("coord-route:") for c in raw["calls"]),
        "tokens": tokens,
        "unknown": len(unknown),
        "cost_micros": sum(int(c["settled_micros"] or 0) for c in known),
        "reserved_micros": sum(int(c["reserved_micros"]) for c in unknown),
        "batch_sizes": [len(b["event_keys"]) for b in raw["batches"]],
        "latencies": {
            "event_to_dispatch": dispatch_latency,
            "event_to_worker_start_or_intake": start_latency,
            "router_model": route_durations,
            "task_completion": completion_latency,
        },
        "cache_observations": [a["details"] for a in raw["audit"] if a["kind"] == "source_cache"],
        "prewarm": [a["details"] for a in raw["audit"] if a["kind"] == "cache_prewarm"],
        "recovery": [
            a
            for a in raw["audit"]
            if a["kind"] in {"known_router_killpoint", "old_epoch_fenced", "known_router_reused"}
        ],
        "replay_seconds": [
            b["replay_seconds"] for b in raw["batches"] if b["replay_seconds"] is not None
        ],
    }


def summarize(rows: list[dict[str, Any]], *, planned: bool = False) -> dict[str, Any]:
    arms = {}
    for arm in ("D", "E", "F"):
        group = [r for r in rows if r["arm"] == arm]
        stats: dict[str, Any] = {"trials": len(group)}
        for key in (
            "normal_passed",
            "normal_total",
            "negative_passed",
            "negative_total",
            "route_passed",
            "route_total",
            "calls",
            "router_calls",
            "worker_calls",
            "cost_micros",
            "unknown",
            "reserved_micros",
            "citation_valid",
            "citation_total",
            "lost_accepted_events",
            "forbidden_effects",
            "duplicates",
            "physical_deliveries",
        ):
            stats[key] = sum(r[key] for r in group)
        stats["tokens"] = {
            k: sum(r["tokens"][k] for r in group)
            for k in (
                "prompt_tokens",
                "completion_tokens",
                "prompt_cache_hit_tokens",
                "prompt_cache_miss_tokens",
            )
        }
        stats["latency_seconds"] = {}
        for name in (
            "event_to_dispatch",
            "event_to_worker_start_or_intake",
            "router_model",
            "task_completion",
        ):
            values = [v for r in group for v in r["latencies"][name]]
            stats["latency_seconds"][name] = {
                "n": len(values),
                "p50": percentile(values, 0.5),
                "p95": percentile(values, 0.95),
            }
        stats["batch_sizes"] = [v for r in group for v in r["batch_sizes"]]
        if planned:
            stats.update(
                {
                    "planned_trials": 4,
                    "not_run_trials": 4 - len(group),
                    "normal_total": 8,
                    "negative_total": 12,
                    "route_total": 36,
                }
            )
        arms[arm] = stats
    return arms


def report(directory: Path) -> dict[str, Any]:
    # Report reads frozen outputs; no model invocation and no imported execution controller.
    manifest = json.loads((directory / "manifest.json").read_text())
    for p, expected in manifest["protected_business_hashes"].items():
        if hashlib.sha256((ROOT / p).read_bytes()).hexdigest() != expected:
            raise ValueError("Original A/B/C result or failure record changed")
    oracle = json.loads((DATA / "oracle.json").read_text())
    if (
        hashlib.sha256((DATA / "oracle.json").read_bytes()).hexdigest()
        != manifest["source_hashes"]["eval/coordination-v1/oracle.json"]
    ):
        raise ValueError("Independent frozen oracle changed")
    records = [
        grade(json.loads((directory / (t["id"] + ".json")).read_text()), oracle)
        for t in manifest["trials"]
        if (directory / (t["id"] + ".json")).exists()
    ]
    complete = len(records) == len(manifest["trials"])
    safe = complete and all(
        not r["lost_accepted_events"]
        and not r["forbidden_effects"]
        and not r["unknown"]
        and r["duplicate_count_correct"]
        for r in records
    )
    result = {
        "schema": "coordination-comparison-report-v1",
        "execution_gate": "PASS" if complete else "INCOMPLETE",
        "safety_gate": "PASS" if safe else "NOT_PASSED",
        "completed_trials": len(records),
        "total_trials": len(manifest["trials"]),
        "arms": {
            k: v
            for k, v in summarize(records, planned=True).items()
            if k in manifest.get("arms", ["D", "E", "F"])
        },
        "trials": records,
        "original_ABC": {"PASS": 87, "FAIL": 3, "regraded": False},
        "not_applicable": {
            "Recall@5": "No retrieval-search task in this fixed Worker workload",
            "hidden_patch_tests": "No patch tasks; original A/B/C repair scores unchanged",
        },
        "limitations": [
            "Two repetitions, developer-visible synthetic workload, not a blind holdout",
            "D omits human thinking time",
            "Local cache covers only fixed Worker source; provider/PG cache not forced cold",
            "Experiment admission-router prompt differs from product cold-start Router prompt",
            "No GitHub business publication, no multi-node or superiority claim",
        ],
    }
    write_json(directory / "results.json", result)
    return result
