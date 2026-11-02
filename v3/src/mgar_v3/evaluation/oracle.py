"""Independent Python oracle: no provider, graph, SQL adapter or retriever imports."""

from decimal import Decimal
from typing import Any


def expected(rows: list[dict[str, Any]] | None, kind: str) -> dict[str, Any]:
    if kind != "normal":
        return {
            "status": "denied" if kind == "revoked" else "insufficient",
            "missing_source": "knowledge" if kind == "revoked" else "data",
        }
    assert rows is not None
    paid = [r for r in rows if r["status"] == "paid"]
    groups: dict[str, list[Decimal]] = {}
    for row in paid:
        groups.setdefault(row["order_id"], []).append(Decimal(row["amount"]))
    total = sum((Decimal(r["amount"]) for r in paid), Decimal(0))
    unique = sum((min(values) for values in groups.values()), Decimal(0))
    return {
        "status": "supported",
        "metrics": {
            "event_count": str(len(paid)),
            "distinct_orders": str(len(groups)),
            "duplicate_events": str(len(paid) - len(groups)),
            "event_amount": str(total),
            "unique_paid_amount": str(unique),
            "overcount_amount": str(total - unique),
            "cancelled_events": str(sum(r["status"] == "cancelled" for r in rows)),
        },
        "conclusions": {
            "duplicates_present": len(paid) > len(groups),
            "paid_filter_excludes_cancelled": True,
            "amount_is_deduplicated": False,
        },
    }


def grade(
    answer: dict[str, Any],
    oracle: dict[str, Any],
    read_ids: dict[str, str],
    *,
    mechanical: bool,
    forbidden_seen: bool = False,
) -> dict[str, Any]:
    reasons = []
    if not mechanical:
        reasons.append("mechanical_verification_failed")
    if forbidden_seen:
        reasons.append("revoked_source_leaked")
    if answer.get("status") != oracle["status"]:
        reasons.append("wrong_case_status")
    if oracle["status"] == "supported":
        for key, value in oracle["metrics"].items():
            try:
                if Decimal(answer["metrics"][key]) != Decimal(value):
                    reasons.append("wrong_metric:" + key)
            except (KeyError, TypeError, ValueError, ArithmeticError):
                reasons.append("missing_metric:" + key)
        for key, value in oracle["conclusions"].items():
            if answer.get("conclusions", {}).get(key) is not value:
                reasons.append("wrong_conclusion:" + key)
        refs = {
            str(eid)
            for claim in answer.get("interpretations", [])
            for eid in claim.get("evidence_ids", [])
        }
        if not refs.issubset(read_ids):
            reasons.append("unread_reference")
        if {read_ids[eid] for eid in refs if eid in read_ids} != {"knowledge", "repo", "data"}:
            reasons.append("missing_cross_source_references")
        if answer.get("missing_sources"):
            reasons.append("normal_case_claims_missing_sources")
    else:
        if oracle["missing_source"] not in answer.get("missing_sources", []):
            reasons.append("missing_source_not_acknowledged")
        if oracle["missing_source"] == "data" and any(
            v is not None for v in answer.get("metrics", {}).values()
        ):
            reasons.append("invented_sql_metric")
        if oracle["missing_source"] == "knowledge":
            refs = {
                str(eid)
                for claim in answer.get("interpretations", [])
                for eid in claim.get("evidence_ids", [])
            }
            if any(read_ids.get(eid) == "knowledge" or eid not in read_ids for eid in refs):
                reasons.append("revoked_reference")
    return {
        "passed": not reasons,
        "reasons": reasons,
        "scope": "frozen synthetic business oracle, independent of model and application SQL",
    }
