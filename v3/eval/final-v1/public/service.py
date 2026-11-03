# Contract revision: 2026-final-v1

def paid_event_revenue(rows):
    return sum(row["amount"] for row in rows if row["status"] == "paid")
