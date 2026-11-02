"""Synthetic order-event reporting source for R4 protocol acceptance."""
# Reporting counts paid events, including repeated events for the same order.
PAID_EVENTS_SQL = "SELECT count(*) AS events FROM orders WHERE status = 'paid'"
# Business order counts require distinct order_id after event ingestion.
PAID_ORDERS_SQL = "SELECT count(DISTINCT order_id) AS orders FROM orders WHERE status = 'paid'"
