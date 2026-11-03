from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from mgar_v3.domain import DomainError
from mgar_v3.routing.domain import Proposals
from mgar_v3.routing.live import RouterPermit

if TYPE_CHECKING:
    from mgar_v3.coordination.engine import Engine, Row


class RouterBudget:
    def __init__(self, engine: "Engine"):
        self.engine = engine

    def get(self, batch_id: UUID, permit: RouterPermit) -> Proposals | None:
        with self.engine.transaction() as conn:
            call = conn.execute(
                "SELECT * FROM app.router_calls WHERE batch_id=%s", (batch_id,)
            ).fetchone()
            if call is None:
                return None
            if call["router_profile_hash"] != permit.profile.profile_hash:
                raise DomainError(
                    "ROUTER_SCOPE_DENIED", "Recorded call belongs to another Router profile"
                )
            if call["status"] != "known":
                raise DomainError("USAGE_UNKNOWN", "Router call cannot be resent")
            if call["result"] is None:
                raise DomainError(
                    "MODEL_RESULT_UNAVAILABLE", "Known failed Router call cannot be resent"
                )
            return Proposals.model_validate(call["result"])

    def reserve(self, batch: "Row", permit: RouterPermit, prompt_hash: str) -> UUID:
        permit.validate(batch["context_hash"])
        if not self.engine.settings.allow_paid:
            raise DomainError(
                "PAID_NOT_AUTHORIZED", "Engine has not enabled paid Router execution", 403
            )
        p, root = permit.profile, permit.root_budget
        with self.engine.transaction() as conn:
            current = conn.execute(
                "SELECT * FROM app.router_batches WHERE batch_id=%s AND project_id=%s FOR UPDATE",
                (batch["batch_id"], self.engine.settings.project_id),
            ).fetchone()
            if current is None or current["status"] != "prepared" or current["mode"] != "live":
                raise DomainError("ROUTER_BATCH_STATE", "Batch cannot reserve a model call")
            if (
                current["expires_at"] <= datetime.now(UTC)
                or current["context_hash"] != batch["context_hash"]
                or current["policy_version"] != self.engine.settings.policy_version
            ):
                raise DomainError(
                    "ROUTER_BATCH_STALE", "Model reservation requires current routing context"
                )
            if conn.execute(
                "SELECT 1 FROM app.router_calls WHERE batch_id=%s", (batch["batch_id"],)
            ).fetchone():
                raise DomainError("MODEL_CALL_ALREADY_RECORDED", "Router step cannot be resent")
            conn.execute(
                "INSERT INTO app.authorization_budgets(profile_hash,limit_micros,call_limit) "
                "VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                (root.profile_hash, root.max_cost_micros, root.max_model_calls),
            )
            budget = conn.execute(
                "UPDATE app.authorization_budgets SET used_calls=used_calls+1,"
                "reserved_micros=reserved_micros+%s WHERE profile_hash=%s AND call_limit=%s "
                "AND limit_micros=%s AND used_calls<call_limit AND "
                "reserved_micros+settled_micros+%s<=limit_micros RETURNING profile_hash",
                (
                    p.reservation_micros,
                    root.profile_hash,
                    root.max_model_calls,
                    root.max_cost_micros,
                    p.reservation_micros,
                ),
            ).fetchone()
            if budget is None:
                raise DomainError(
                    "BUDGET_EXHAUSTED", "Shared model call or cost budget is exhausted", 403
                )
            self.engine.benchmarks.check_global(
                conn, self.engine.settings.repository.lower(), p.reservation_micros
            )
            call_id = uuid4()
            conn.execute(
                "INSERT INTO app.router_calls(call_id,batch_id,budget_profile_hash,"
                "router_profile_hash,status,reserved_micros) VALUES (%s,%s,%s,%s,'reserved',%s)",
                (
                    call_id,
                    batch["batch_id"],
                    root.profile_hash,
                    p.profile_hash,
                    p.reservation_micros,
                ),
            )
            conn.execute(
                "UPDATE app.router_batches SET status='model_running' WHERE batch_id=%s",
                (batch["batch_id"],),
            )
            self.engine.routing._audit(
                conn,
                batch["batch_id"],
                "router_model_reserved",
                call_id=str(call_id),
                prompt_hash=prompt_hash,
                reserved_micros=p.reservation_micros,
            )
            return call_id

    def response(self, call_id: UUID, body: bytes) -> None:
        if len(body) > 262144:
            raise DomainError("MODEL_RESPONSE_LIMIT", "Router response exceeds its byte bound")
        h = self.engine.artifacts.put(body)
        with self.engine.transaction() as conn:
            conn.execute(
                "INSERT INTO app.artifacts(content_hash,byte_count) VALUES (%s,%s) ON CONFLICT "
                "DO NOTHING",
                (h, len(body)),
            )
            conn.execute(
                "UPDATE app.router_calls SET response_hash=%s WHERE call_id=%s", (h, call_id)
            )

    def settle(
        self,
        call_id: UUID,
        cost: int | None,
        usage: dict[str, Any] | None = None,
        result: Proposals | None = None,
    ) -> None:
        with self.engine.transaction() as conn:
            call = conn.execute(
                "SELECT * FROM app.router_calls WHERE call_id=%s FOR UPDATE", (call_id,)
            ).fetchone()
            if call is None:
                raise DomainError("CALL_NOT_FOUND", "Router call was not found", 404)
            if call["status"] == "known":
                if cost != call["settled_micros"]:
                    raise DomainError(
                        "SETTLEMENT_CONFLICT", "Router call already settled differently"
                    )
                return
            if cost is None or type(cost) is not int or not 0 <= cost <= call["reserved_micros"]:
                conn.execute(
                    "UPDATE app.router_calls SET status='unknown' WHERE call_id=%s", (call_id,)
                )
                conn.execute(
                    "UPDATE app.router_batches SET status='usage_unknown',"
                    "reason_code='USAGE_UNKNOWN' WHERE batch_id=%s",
                    (call["batch_id"],),
                )
                return
            conn.execute(
                "UPDATE app.authorization_budgets SET reserved_micros=reserved_micros-%s,"
                "settled_micros=settled_micros+%s WHERE profile_hash=%s",
                (call["reserved_micros"], cost, call["budget_profile_hash"]),
            )
            conn.execute(
                "UPDATE app.router_calls SET status='known',settled_micros=%s,usage=%s,"
                "result=%s WHERE call_id=%s",
                (
                    cost,
                    Jsonb(usage or {}),
                    Jsonb(result.model_dump(mode="json")) if result else None,
                    call_id,
                ),
            )
            self.engine.routing._audit(
                conn,
                call["batch_id"],
                "router_model_settled",
                call_id=str(call_id),
                cost_micros=cost,
            )
