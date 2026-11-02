"Trusted independent validator; its source and expected cases are never model inputs."

import json
import random


def suite() -> str:
    rng = random.Random(72913)
    cases = [
        [],
        [("x", "pending", 17), ("x", "paid", 29)],
        [("a", "paid", 0), ("a", "paid", 55), ("b", "paid", -4)],
    ]
    for _ in range(25):
        cases.append(
            [
                (
                    str(rng.randrange(5)),
                    rng.choice(["paid", "cancelled", "pending"]),
                    rng.randrange(-5, 90),
                )
                for _ in range(rng.randrange(1, 14))
            ]
        )
    rows = []
    for index, orders in enumerate(cases):
        seen = set()
        expected = 0
        for oid, status, amount in orders:
            if status == "paid" and oid not in seen:
                seen.add(oid)
                expected += amount
        literals = ",".join(
            "{" + json.dumps(oid) + "," + json.dumps(status) + "," + str(amount) + "}"
            for oid, status, amount in orders
        )
        rows.append(
            f'  t.Run("v{index}",func(t *testing.T){{'
            f"if got:=PaidRevenueCents([]Order{{{literals}}});got!={expected}"
            '{t.Fatal("independent contract failed")}})'
        )
    return (
        'package orders\nimport "testing"\nfunc TestR5Hidden(t *testing.T){\n'
        + "\n".join(rows)
        + "\n}\n"
    )
