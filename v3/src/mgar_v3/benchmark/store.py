from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from psycopg.types.json import Jsonb

from mgar_v3.benchmark.domain import EvaluationAuthorization
from mgar_v3.domain import DomainError, digest

if TYPE_CHECKING:
    from mgar_v3.coordination.engine import Connection, Engine, Row


class Benchmarks:
    def __init__(self, engine: "Engine"):
        self.engine = engine

    def check_global(self, conn: "Connection", repository: str, amount: int) -> None:
        conn.execute(
            "INSERT INTO app.repository_cost_caps(repository) VALUES (%s) ON CONFLICT DO NOTHING",
            (repository,),
        )
        limit = conn.execute(
            "SELECT limit_micros FROM app.repository_cost_caps WHERE repository=%s FOR UPDATE",
            (repository,),
        ).fetchone()
        used = self.cost(conn, repository)
        if used["unknown"]:
            raise DomainError(
                "USAGE_UNKNOWN", "Repository usage must reconcile before more paid calls"
            )
        if not limit or used["cost_micros"] + amount > limit["limit_micros"]:
            raise DomainError(
                "BUDGET_EXHAUSTED", "Cumulative repository USD 2 ceiling exhausted", 403
            )

    @staticmethod
    def cost(conn: "Connection", repository: str) -> "Row":
        row = conn.execute(
            "WITH charges AS (SELECT c.status,c.settled_micros,c.reserved_micros "
            "FROM app.usage_calls c JOIN app.runs r USING(run_id) "
            "JOIN app.tickets t USING(ticket_id) JOIN app.threads h USING(thread_id) "
            "WHERE h.repository=%s UNION ALL "
            "SELECT c.status,c.settled_micros,c.reserved_micros FROM app.router_calls c "
            "JOIN app.router_batches b USING(batch_id) WHERE EXISTS "
            "(SELECT 1 FROM jsonb_array_elements(b.snapshot->'threads') item "
            "JOIN app.threads h ON h.thread_id=(item->>'thread_id')::uuid "
            "WHERE h.repository=%s)) "
            "SELECT count(*) AS calls,coalesce(sum(CASE WHEN status='known' "
            "AND settled_micros IS NOT NULL THEN settled_micros ELSE reserved_micros END),0) "
            "AS cost_micros,count(*) FILTER(WHERE status='unknown' "
            "OR (status='known' AND settled_micros IS NULL)) AS unknown FROM charges",
            (repository, repository),
        ).fetchone()
        assert row is not None
        return row

    def authorize(self, manifest: dict[str, Any], auth: EvaluationAuthorization) -> str:
        if (
            not auth.allow_paid
            or not self.engine.settings.allow_paid
            or auth.manifest_hash != digest(manifest)
            or auth.expires_at.tzinfo is None
            or auth.expires_at <= datetime.now(UTC)
            or auth.approved_by.lower() != self.engine.settings.local_actor.lower()
            or auth.max_new_calls != manifest["max_new_calls"]
            or auth.max_new_cost_micros != manifest["max_new_cost_micros"]
            or manifest["repository"] != self.engine.settings.repository.lower()
        ):
            raise DomainError(
                "BENCHMARK_AUTH_REQUIRED", "Exact unexpired final-evaluation grant required", 403
            )
        with self.engine.transaction() as conn:
            existing = conn.execute(
                "SELECT * FROM app.benchmark_grants WHERE manifest_hash=%s", (auth.manifest_hash,)
            ).fetchone()
            if existing:
                if existing["authorization_payload"] != auth.model_dump(mode="json"):
                    raise DomainError("BENCHMARK_GRANT_CHANGED", "Existing grant cannot be reset")
                return str(existing["budget_hash"])
            self.check_global(conn, manifest["repository"], auth.max_new_cost_micros)
            used = self.cost(conn, manifest["repository"])
            if int(used["cost_micros"]) != manifest["prior_cost_micros"]:
                raise DomainError(
                    "BENCHMARK_PREDECESSOR_CHANGED", "Historical cumulative cost changed"
                )
            budget = digest(
                {"final_manifest": auth.manifest_hash, "grant": auth.model_dump(mode="json")}
            )
            conn.execute(
                "INSERT INTO app.benchmark_budgets(budget_hash,limit_micros,call_limit) "
                "VALUES (%s,%s,%s)",
                (budget, auth.max_new_cost_micros, auth.max_new_calls),
            )
            conn.execute(
                "INSERT INTO app.benchmark_grants(manifest_hash,manifest,auth"
                "orization_payload,budget_hash) "
                "VALUES (%s,%s,%s,%s)",
                (auth.manifest_hash, Jsonb(manifest), Jsonb(auth.model_dump(mode="json")), budget),
            )
            return budget

    def bind(self, run_id: UUID, manifest_hash: str, case_id: str, arm: str) -> None:
        with self.engine.transaction() as conn:
            run = self.engine._run(conn, run_id)
            grant = conn.execute(
                "SELECT * FROM app.benchmark_grants WHERE manifest_hash=%s", (manifest_hash,)
            ).fetchone()
            if not grant:
                raise DomainError("BENCHMARK_AUTH_REQUIRED", "No paid grant exists", 403)
            scope = conn.execute(
                "SELECT scope FROM app.mandates WHERE mandate_id=%s", (run["mandate_id"],)
            ).fetchone()
            item = next(
                (
                    p
                    for p in grant["manifest"]["profiles"]
                    if p["case_id"] == case_id and p["arm"] == arm
                ),
                None,
            )
            if (
                not scope
                or not scope["scope"].get("read_only")
                or not item
                or (scope["scope"].get("profile_hash") != item["profile_hash"])
            ):
                raise DomainError(
                    "BENCHMARK_SCOPE_CHANGED", "Run differs from the frozen case and arm", 403
                )
            old = conn.execute(
                "SELECT * FROM app.benchmark_runs WHERE manifest_hash=%s AND case_id=%s AND arm=%s",
                (manifest_hash, case_id, arm),
            ).fetchone()
            if old and old["run_id"] != run_id:
                raise DomainError(
                    "BENCHMARK_ALREADY_EXECUTED", "Recorded task cannot be silently rerun"
                )
            conn.execute(
                "INSERT INTO app.benchmark_runs(run_id,manifest_hash,case_id,arm) "
                "VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (run_id, manifest_hash, case_id, arm),
            )

    def reserve(self, conn: "Connection", run_id: UUID, amount: int) -> str | None:
        grant = conn.execute(
            "SELECT g.* FROM app.benchmark_runs r JOIN app.benchmark_grants g "
            "USING(manifest_hash) WHERE r.run_id=%s",
            (run_id,),
        ).fetchone()
        if not grant:
            return None
        auth = EvaluationAuthorization.model_validate(grant["authorization_payload"])
        if auth.expires_at <= datetime.now(UTC):
            raise DomainError("BENCHMARK_AUTH_EXPIRED", "Final grant expired")
        if conn.execute(
            "SELECT 1 FROM app.usage_calls WHERE final_budget_hash=%s AND status='unknown'",
            (grant["budget_hash"],),
        ).fetchone():
            raise DomainError("USAGE_UNKNOWN", "Final evaluation usage unknown")
        changed = conn.execute(
            "UPDATE app.benchmark_budgets SET used_calls=used_calls+1,"
            "reserved_micros=reserved_micros+%s WHERE budget_hash=%s AND used_calls<call_limit "
            "AND reserved_micros+settled_micros+%s<=limit_micros RETURNING budget_hash",
            (amount, grant["budget_hash"], amount),
        ).fetchone()
        if not changed:
            raise DomainError("BUDGET_EXHAUSTED", "Final evaluation shared grant exhausted")
        return str(grant["budget_hash"])

    def require_ablation(self, run_id: UUID, ablation: str) -> None:
        with self.engine.transaction() as conn:
            row = conn.execute(
                "SELECT g.manifest FROM app.benchmark_runs r "
                "JOIN app.benchmark_grants g USING(manifest_hash) WHERE run_id=%s",
                (run_id,),
            ).fetchone()
            if not row or row["manifest"].get("ablation") != ablation:
                raise DomainError(
                    "ABLATION_SCOPE_DENIED",
                    "Ablations need a separately frozen benchmark grant",
                    403,
                )
