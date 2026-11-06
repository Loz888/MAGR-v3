"""Exact P0 rules over observed rows, never expected answers or model arithmetic."""

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from mgar_v3.commerce.domain import Metrics, Window
from mgar_v3.domain import DomainError, digest

COLUMNS = {"event_id": 20, "order_id": 25, "status": 25, "amount": 1700, "created_at": 1184}


def observed_rows(result: dict[str, Any]) -> list[dict[str, Any]]:
    if result.get("result_hash") != digest({k: v for k, v in result.items() if k != "result_hash"}):
        raise DomainError("SQL_HASH", "SQL receipt changed")
    columns = result["columns"]
    if {c["name"]: c["type_oid"] for c in columns} != COLUMNS or len(columns) != 5:
        raise DomainError("SQL_CELL", "Commerce requires the five typed source columns")
    if len(result["rows"]) > 100:
        raise DomainError("DATA_LIMIT", "Complete source exceeds this registered scope")
    rows = []
    for cells in result["rows"]:
        if len(cells) != 5:
            raise DomainError("SQL_CELL", "Invalid source row")
        row = {c["name"]: v for c, v in zip(columns, cells, strict=True)}
        if type(row["event_id"]) is not int or not isinstance(row["order_id"], str):
            raise DomainError("SQL_CELL", "Invalid event or order identity")
        try:
            amount = Decimal(row["amount"])
            stamp = datetime.fromisoformat(row["created_at"])
            if (
                not isinstance(row["amount"], str)
                or not amount.is_finite()
                or amount < 0
                or amount != amount.quantize(Decimal("0.01"))
                or stamp.tzinfo is None
                or row["status"] not in {"paid", "pending", "cancelled", "refunded"}
            ):
                raise ValueError("outside contract")
        except (ValueError, TypeError, InvalidOperation):
            raise DomainError(
                "DATA_CONFLICT", "Amount, time or status needs clarification"
            ) from None
        row["amount"] = format(amount, ".2f")
        row["created_at"] = stamp.astimezone(UTC).isoformat().replace("+00:00", "Z")
        rows.append(row)
    if len({r["event_id"] for r in rows}) != len(rows):
        raise DomainError("DATA_CONFLICT", "Event IDs are not unique")
    return sorted(rows, key=lambda r: r["event_id"])


def calculate(rows: list[dict[str, Any]], window: Window) -> Metrics:
    selected = [
        r
        for r in rows
        if r["status"] == "paid"
        and window.start
        <= datetime.fromisoformat(r["created_at"].replace("Z", "+00:00"))
        < window.end
    ]
    first: dict[str, Decimal] = {}
    total = Decimal(0)
    for row in sorted(selected, key=lambda r: r["event_id"]):
        amount = Decimal(row["amount"])
        if row["order_id"] in first and first[row["order_id"]] != amount:
            raise DomainError("DATA_CONFLICT", "Duplicate paid amounts disagree")
        first.setdefault(row["order_id"], amount)
        total += amount
    unique = sum(first.values(), Decimal(0))
    return Metrics(
        paid_events=str(len(selected)),
        distinct_orders=str(len(first)),
        event_amount=f"{total:.2f}",
        dedup_amount=f"{unique:.2f}",
        inflation_amount=f"{total - unique:.2f}",
    )
