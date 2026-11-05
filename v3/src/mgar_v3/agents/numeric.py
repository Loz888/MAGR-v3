"""Exact calculations over observed typed rows, independent of evaluation oracles."""

from decimal import Decimal, InvalidOperation, localcontext
from typing import Any

from mgar_v3.domain import DomainError


def revenue_metrics(rows: list[dict[str, Any]]) -> dict[str, str]:
    """Count paid events; select the earliest paid event per order, including zero."""
    try:
        if len(rows) > 1000:
            raise ValueError("row limit")
        seen = set()
        paid = []
        for row in rows:
            event, order, status, raw = (
                row["event_id"],
                row["order_id"],
                row["status"],
                row["amount"],
            )
            if (
                type(event) is not int
                or event in seen
                or not isinstance(order, str)
                or not isinstance(status, str)
                or not isinstance(raw, (str, Decimal, int))
                or isinstance(raw, bool)
                or len(str(raw)) > 80
            ):
                raise ValueError("typed row")
            seen.add(event)
            amount = Decimal(raw)
            if not amount.is_finite() or abs(amount.adjusted()) > 50:
                raise ValueError("amount")
            if status == "paid":
                paid.append((event, order, amount))
        # Enough precision for every admitted amount and the bounded sum.
        with localcontext() as context:
            context.prec = 160
            selected: dict[str, tuple[int, Decimal]] = {}
            for event, order, amount in paid:
                if order not in selected or event < selected[order][0]:
                    selected[order] = (event, amount)
            return {
                "paid_events": str(len(paid)),
                "distinct_orders": str(len(selected)),
                "event_amount": format(sum((p[2] for p in paid), Decimal(0)), "f"),
                "dedup_amount": format(sum((p[1] for p in selected.values()), Decimal(0)), "f"),
            }
    except (ValueError, KeyError, TypeError, InvalidOperation):
        raise DomainError(
            "NUMERIC_INPUT_INVALID", "Observed rows are not exact bounded data"
        ) from None
