"""Independent bounded-business scoring. Never imported by the model provider."""

from collections.abc import Sequence
from decimal import Decimal
from typing import Any


def cross_expected(rows: list[dict[str, Any]]) -> dict[str, str]:
    paid = [r for r in sorted(rows, key=lambda r: r["event_id"]) if r["status"] == "paid"]
    first: dict[str, Decimal] = {}
    for row in paid:
        first.setdefault(row["order_id"], Decimal(row["amount"]))
    return {
        "paid_events": str(len(paid)),
        "distinct_orders": str(len(first)),
        "event_amount": str(sum((Decimal(r["amount"]) for r in paid), Decimal(0))),
        "dedup_amount": str(sum(first.values(), Decimal(0))),
        "implementation_deduplicates": "false",
    }


def repair_expected(rows: list[dict[str, Any]], variant: int) -> int:
    values: dict[str, int] = {}
    for row in rows:
        key, state, amount = row["ID"], row["Status"], row["Amount"]
        if variant == 1:
            values[key] = amount if state == "paid" else 0
        elif state == "paid":
            if variant == 0:
                values[key] = amount
            elif variant == 2:
                if amount > 0:
                    values.setdefault(key, amount)
            elif variant == 3:
                values[key] = values.get(key, 0) + amount
            elif variant == 4:
                values[key] = max(values.get(key, amount), amount)
            else:
                raise ValueError("Unknown repair contract")
    return sum(min(v, 100) if variant == 3 else v for v in values.values())


def hidden_suite(variant: int) -> str:
    import json

    examples = [
        [],
        [{"ID": "a", "Status": "paid", "Amount": -3}],
        [
            {"ID": "a", "Status": "paid", "Amount": 4},
            {"ID": "a", "Status": "cancelled", "Amount": 9},
        ],
        [{"ID": "a", "Status": "paid", "Amount": 0}, {"ID": "a", "Status": "paid", "Amount": 9}],
    ]
    for i in range(24):
        examples.append(
            [
                {
                    "ID": str((j + i) % 3),
                    "Status": ["paid", "cancelled", "pending", "paid"][(j + i) % 4],
                    "Amount": ((i * 17 + j * 23) % 181) - 40,
                }
                for j in range(1 + i % 9)
            ]
        )
    cases = []
    for i, rows in enumerate(examples):
        literals = ",".join(
            "{" + json.dumps(r["ID"]) + "," + json.dumps(r["Status"]) + "," + str(r["Amount"]) + "}"
            for r in rows
        )
        cases.append(
            't.Run("v'
            + str(i)
            + '",func(t *testing.T){ if got:=PaidRevenueCents([]Order{'
            + literals
            + "}); got!="
            + str(repair_expected(rows, variant))
            + ' {t.Fatalf("wrong result %d",got)} })'
        )
    return (
        'package orders\nimport "testing"\nfunc TestR6Hidden(t *testing.T){\n'
        + "\n".join(cases)
        + "\n}\n"
    )


def grade(
    answer: dict[str, Any],
    expected: dict[str, Any],
    sources: list[dict[str, Any]],
    required_roles: Sequence[str],
    repair_passed: bool | None = None,
) -> dict[str, Any]:
    reasons = []
    source_map = {s["id"]: s for s in sources}
    valid = 0
    if answer.get("status") != expected["status"]:
        reasons.append("wrong_status")
    actual = answer.get("values", {})
    if set(actual) != set(expected.get("values", {})):
        reasons.append("wrong_value_fields")
    for key, value in expected.get("values", {}).items():
        try:
            equal = (
                Decimal(actual[key]) == Decimal(value)
                if value not in {"true", "false"}
                else actual[key] == value
            )
            if not equal:
                reasons.append("wrong_value:" + key)
        except (KeyError, ValueError, TypeError, ArithmeticError):
            reasons.append("missing_value:" + key)
    cited = set()
    for ref in answer.get("citations", []):
        source = source_map.get(ref.get("source_id"))
        if not source or not ref.get("quote") or ref["quote"] not in source["text"]:
            reasons.append("invalid_citation")
        else:
            valid += 1
            cited.add(source["role"])
    if (
        expected["status"] == "supported"
        and repair_passed is None
        and not set(required_roles) <= cited
    ):
        reasons.append("missing_source_role")
    if repair_passed is False:
        reasons.append("independent_hidden_tests_failed")
    return {
        "passed": not reasons,
        "reasons": reasons,
        "valid_citations": valid,
        "citations": len(answer.get("citations", [])),
    }
