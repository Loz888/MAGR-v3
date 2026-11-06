"""Protected integration test rendered from actual typed observations, never model input."""

import json
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import UUID

from mgar_v3.commerce.domain import Window
from mgar_v3.commerce.numeric import calculate
from mgar_v3.domain import DomainError

if TYPE_CHECKING:
    from mgar_v3.coordination.engine import Engine


def case_suite(engine: "Engine", run_id: UUID) -> str:
    link = engine.commerce.binding(run_id)
    if not link:
        return ""
    with engine.transaction() as conn:
        parent = conn.execute(
            "SELECT payload FROM app.business_results WHERE result_id=%s",
            (link["preceding_result_id"],),
        ).fetchone()
    if not parent or not parent["payload"]["numeric_receipt_hash"]:
        raise DomainError("NUMERIC_PROVENANCE_REQUIRED", "Repair requires observed commerce rows")
    receipt = json.loads(engine.artifacts.read(parent["payload"]["numeric_receipt_hash"]))
    cells: dict[int, dict[int, object]] = {}
    for operand in receipt["operands"]:
        cells.setdefault(operand["row"], {})[operand["column"]] = operand["value"]
    columns = ["event_id", "order_id", "status", "amount", "created_at"]
    rows = [
        dict(zip(columns, [r[i] for i in range(5)], strict=True)) for _, r in sorted(cells.items())
    ]
    window = Window.model_validate(link["spec"]["window"])
    # This trusted check is additionally tested against independently frozen acceptance values.
    expected = int(Decimal(calculate(rows, window).dedup_amount) * 100)
    orders = []
    for row in rows:
        stamp = datetime.fromisoformat(str(row["created_at"]))
        if window.start <= stamp < window.end:
            amount = Decimal(str(row["amount"])) * 100
            if amount != amount.to_integral_value() or abs(amount) > 2**53:
                raise DomainError("GO_AMOUNT_RANGE", "Exact cents exceed the portable Go scope")
            orders.append(
                "{"
                + json.dumps(row["order_id"])
                + ","
                + json.dumps(row["status"])
                + ","
                + str(int(amount))
                + "}"
            )
    return (
        "\nfunc TestCommerceObservedWindow(t *testing.T) {\n"
        " rows := []Order{" + ",".join(orders) + "}\n"
        " got := PaidRevenueCents(rows)\n"
        " if got != "
        + str(expected)
        + ' { t.Fatal("commerce observed-window result mismatch") }\n}\n'
    )
