"""DEF bindings are separate from the immutable original ABC experiment bindings."""

from datetime import UTC, datetime
from uuid import UUID

from mgar_v3.benchmark.domain import EvaluationAuthorization
from mgar_v3.benchmark.store import Benchmarks
from mgar_v3.coordination.engine import Connection
from mgar_v3.domain import DomainError


class CoordinationBudget(Benchmarks):
    def bind(self, run_id: UUID, manifest_hash: str, case_id: str, arm: str) -> None:
        with self.engine.transaction() as conn:
            run = self.engine._run(conn, run_id)
            grant = conn.execute(
                "SELECT * FROM app.benchmark_grants WHERE manifest_hash=%s", (manifest_hash,)
            ).fetchone()
            if not grant or grant["manifest"].get("schema") not in {
                "coordination-comparison-v1",
                "coordination-comparison-v2",
            }:
                raise DomainError("COORD_GRANT_REQUIRED", "Exact coordination grant required")
            item = next(
                (
                    p
                    for p in grant["manifest"]["profiles"]
                    if p["case_id"] == case_id and p["arm"] == arm
                ),
                None,
            )
            scope = conn.execute(
                "SELECT scope FROM app.mandates WHERE mandate_id=%s", (run["mandate_id"],)
            ).fetchone()
            if (
                arm not in {"D", "E", "F"}
                or not item
                or not scope
                or not scope["scope"].get("read_only")
                or scope["scope"].get("profile_hash") != item["profile_hash"]
            ):
                raise DomainError("COORD_SCOPE_CHANGED", "Run differs from the reviewed profile")
            old = conn.execute(
                "SELECT run_id FROM coordination_eval.bound_runs WHERE manifest_hash=%s "
                "AND case_id=%s AND arm=%s",
                (manifest_hash, case_id, arm),
            ).fetchone()
            if old and old["run_id"] != run_id:
                raise DomainError("COORD_ALREADY_EXECUTED", "A bound slot cannot be replaced")
            conn.execute(
                "INSERT INTO coordination_eval.bound_runs(run_id,manifest_hash,case_id,arm) "
                "VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (run_id, manifest_hash, case_id, arm),
            )

    def reserve(self, conn: Connection, run_id: UUID, amount: int) -> str | None:
        grant = conn.execute(
            "SELECT g.* FROM coordination_eval.bound_runs r JOIN app.benchmark_grants g "
            "USING(manifest_hash) WHERE r.run_id=%s",
            (run_id,),
        ).fetchone()
        if grant is None:
            raise DomainError("COORD_GRANT_REQUIRED", "An experiment call needs its frozen slot")
        auth = EvaluationAuthorization.model_validate(grant["authorization_payload"])
        if auth.expires_at <= datetime.now(UTC):
            raise DomainError("BENCHMARK_AUTH_EXPIRED", "Coordination grant expired")
        if conn.execute(
            "SELECT 1 FROM app.usage_calls WHERE final_budget_hash=%s AND status='unknown'",
            (grant["budget_hash"],),
        ).fetchone():
            raise DomainError("USAGE_UNKNOWN", "No new calls after uncertain usage")
        row = conn.execute(
            "UPDATE app.benchmark_budgets SET used_calls=used_calls+1,"
            "reserved_micros=reserved_micros+%s WHERE budget_hash=%s AND used_calls<call_limit "
            "AND reserved_micros+settled_micros+%s<=limit_micros RETURNING budget_hash",
            (amount, grant["budget_hash"], amount),
        ).fetchone()
        if row is None:
            raise DomainError("BUDGET_EXHAUSTED", "Shared coordination budget exhausted")
        return str(row["budget_hash"])
