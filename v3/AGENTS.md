# V3 application rules

Use Python 3.12, the locked environment, and the explicit SQL migrations.
Keep remote calls outside database transactions. Route external writes through approved Action/Outbox records and Dispatcher.
Treat an unconfirmed remote write as unknown; reconcile before any further action. Do not automatically retry unknown writes.
Test concurrency, fencing, transaction rollback, cancellation, and process recovery against real PostgreSQL.
Keep fixture and live execution modes explicit. Mechanical evidence validation does not establish semantic correctness.
Use `make lint`, `make typecheck`, and the card's `make verify TASK=...` target; keep raw logs in evidence.
