"""Owner-supplied hourly billing reconciliation; no invented per-call token counts."""

import json
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from mgar_v3.coordination.engine import Engine
from mgar_v3.domain import DomainError, Model
from mgar_v3.live import LiveProfile


class HourlyReceipt(Model):
    bill_id: str
    start: datetime
    end: datetime
    model: str
    credential_alias: str
    currency: Literal["CNY"]
    request_count: int = Field(gt=0)
    input_cache_hit_tokens: int = Field(ge=0)
    input_cache_miss_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cache_hit_unit_price: Decimal = Field(gt=0)
    cache_miss_unit_price: Decimal = Field(gt=0)
    output_unit_price: Decimal = Field(gt=0)
    paid_amount: Decimal = Field(ge=0)
    complete_scope: Literal[True]
    approved_by: str
    source: Literal["user_supplied_provider_bill"]

    @model_validator(mode="after")
    def valid(self) -> Self:
        if (
            self.start.tzinfo is None
            or self.end.tzinfo is None
            or self.end - self.start != timedelta(hours=1)
        ):
            raise ValueError("One explicit timezone-aware provider billing hour required")
        computed = (
            self.input_cache_hit_tokens * self.cache_hit_unit_price
            + self.input_cache_miss_tokens * self.cache_miss_unit_price
            + self.output_tokens * self.output_unit_price
        )
        if computed != self.paid_amount:
            raise ValueError("Provider token charges differ from paid invoice amount")
        return self


def reconcile_rejected(
    engine: Engine, receipt: HourlyReceipt, call_id: UUID, archive_hash: str
) -> dict[str, Any]:
    """Release one definite rejection only when a complete bill matches all known usage.

    HTTP 400 alone establishes neither free usage nor a refund. This narrow path
    requires one unknown call, its stored invalid-request response, and a complete
    provider hour whose request count AND token totals equal the known calls.
    """
    receipt = HourlyReceipt.model_validate(receipt.model_dump())
    if receipt.approved_by.lower() != engine.settings.local_actor.lower():
        raise DomainError("BILLING_RECONCILIATION_DENIED", "Owner receipt required")
    engine.artifacts.read(archive_hash)  # immutable owner-supplied original, outside transaction
    raw = receipt.model_dump_json(indent=2).encode()
    receipt_hash = engine.artifacts.put(raw)
    with engine.transaction() as conn:
        response = conn.execute(
            "SELECT safe_payload FROM app.run_events WHERE kind='model_response_recorded' "
            "AND safe_payload->>'call_id'=%s",
            (str(call_id),),
        ).fetchall()
    if len(response) != 1 or response[0]["safe_payload"].get("http_status") != 400:
        raise DomainError("BILLING_SCOPE_MISMATCH", "One definite HTTP 400 required")
    response_hash = response[0]["safe_payload"]["content_hash"]
    body = json.loads(engine.artifacts.read(response_hash))
    if not isinstance(body, dict) or body.get("error", {}).get("type") != "invalid_request_error":
        raise DomainError("BILLING_SCOPE_MISMATCH", "Stored invalid-request error required")
    with engine.transaction() as conn:
        previous = conn.execute(
            "SELECT safe_payload FROM app.run_events WHERE kind='rejected_usage_reconciled' "
            "AND safe_payload->>'call_id'=%s",
            (str(call_id),),
        ).fetchone()
        if previous:
            if (
                previous["safe_payload"]["receipt_hash"],
                previous["safe_payload"]["archive_hash"],
            ) != (receipt_hash, archive_hash):
                raise DomainError("BILLING_SCOPE_MISMATCH", "Different reconciliation evidence")
            return dict(previous["safe_payload"])
        calls = conn.execute(
            "SELECT c.* FROM app.usage_calls c JOIN app.run_events e ON "
            "e.safe_payload->>'call_id'=c.call_id::text AND e.kind='model_reserved' "
            "WHERE e.created_at>=%s AND e.created_at<%s FOR UPDATE OF c",
            (receipt.start, receipt.end),
        ).fetchall()
        unknown = [c for c in calls if c["status"] != "known"]
        known = [c for c in calls if c["status"] == "known"]
        if (
            len(unknown) != 1
            or unknown[0]["call_id"] != call_id
            or len(known) != receipt.request_count
        ):
            raise DomainError(
                "BILLING_SCOPE_MISMATCH", "Complete hour must exclude only this rejection"
            )
        call = unknown[0]
        run = engine._run(conn, call["run_id"])
        if run["status"] not in {"failed", "cancelled"} or call["final_budget_hash"] is None:
            raise DomainError("BILLING_SCOPE_MISMATCH", "Stopped final evaluation required")
        if any(c["final_budget_hash"] != call["final_budget_hash"] for c in calls):
            raise DomainError("BILLING_SCOPE_MISMATCH", "Mixed billing scope")
        grant = conn.execute(
            "SELECT manifest FROM app.benchmark_grants WHERE budget_hash=%s",
            (call["final_budget_hash"],),
        ).fetchone()
        assert grant is not None
        if {p["profile"]["model_id"] for p in grant["manifest"]["profiles"]} != {receipt.model}:
            raise DomainError("BILLING_SCOPE_MISMATCH", "Mixed provider models")
        totals = [0, 0, 0]
        for row in known:
            u = row["usage"] or {}
            hit = u.get(
                "prompt_cache_hit_tokens",
                u.get("prompt_tokens_details", {}).get("cached_tokens", 0),
            )
            prompt, output = u.get("prompt_tokens"), u.get("completion_tokens")
            miss = u.get(
                "prompt_cache_miss_tokens",
                prompt - hit if type(prompt) is int and type(hit) is int else None,
            )
            if (
                any(type(n) is not int or n < 0 for n in (hit, miss, prompt, output))
                or hit + miss != prompt
            ):
                raise DomainError("BILLING_SCOPE_MISMATCH", "Known calls need exact usage")
            totals = [a + b for a, b in zip(totals, (hit, miss, output), strict=True)]
        if totals != [
            receipt.input_cache_hit_tokens,
            receipt.input_cache_miss_tokens,
            receipt.output_tokens,
        ]:
            raise DomainError("BILLING_SCOPE_MISMATCH", "Invoice has unattributed usage")
        summary = {
            "call_id": str(call_id),
            "receipt_hash": receipt_hash,
            "archive_hash": archive_hash,
            "response_hash": response_hash,
            "provider_requests": receipt.request_count,
            "known_requests": len(known),
            "settled_micros": 0,
            "basis": "complete_provider_hour_matches_all_known_usage_excludes_definite_rejection",
            "per_call_tokens_unknown": True,
            "no_resend": True,
            "call_count_refunded": False,
        }
        engine.settle_call(call_id, 0, summary)
        conn.execute(
            "INSERT INTO app.artifacts(content_hash,byte_count) VALUES (%s,%s) "
            "ON CONFLICT DO NOTHING",
            (receipt_hash, len(raw)),
        )
        engine._event(conn, run["run_id"], "rejected_usage_reconciled", **summary)
        conn.execute(
            "UPDATE app.runs SET reason_code='BILLING_RECONCILED_PROVIDER_REJECTION' "
            "WHERE run_id=%s",
            (run["run_id"],),
        )
        return summary


def reconcile(engine: Engine, receipt: HourlyReceipt, call_ids: list[UUID]) -> dict[str, Any]:
    if (
        receipt.approved_by.lower() != engine.settings.local_actor.lower()
        or len(set(call_ids)) != 2
    ):
        raise DomainError(
            "BILLING_RECONCILIATION_DENIED", "Owner and exactly two scoped calls required"
        )
    raw = receipt.model_dump_json(indent=2).encode()
    receipt_hash = engine.artifacts.put(raw)
    with engine.transaction() as conn:
        previous = conn.execute(
            "SELECT safe_payload FROM app.run_events WHERE "
            "kind='aggregate_usage_reconciled' AND safe_payload->>'receipt_hash'=%s",
            (receipt_hash,),
        ).fetchone()
        if previous:
            if set(previous["safe_payload"]["call_ids"]) != {str(c) for c in call_ids}:
                raise DomainError(
                    "BILLING_SCOPE_MISMATCH", "Receipt already belongs to other calls"
                )
            return dict(previous["safe_payload"])
        calls = conn.execute(
            "SELECT * FROM app.usage_calls WHERE call_id=ANY(%s) FOR UPDATE", (call_ids,)
        ).fetchall()
        if (
            len(calls) != 2
            or any(c["status"] != "unknown" for c in calls)
            or len({(c["run_id"], c["profile_hash"], c["final_budget_hash"]) for c in calls}) != 1
            or calls[0]["final_budget_hash"] is None
        ):
            raise DomainError(
                "BILLING_SCOPE_MISMATCH", "Unknown calls must share one Run and final budget"
            )
        run = engine._run(conn, calls[0]["run_id"])
        if run["status"] not in {"failed", "cancelled"}:
            raise DomainError("BILLING_SCOPE_MISMATCH", "No live worker may own these requests")
        grant = conn.execute(
            "SELECT manifest FROM app.benchmark_grants WHERE budget_hash=%s",
            (calls[0]["final_budget_hash"],),
        ).fetchone()
        assert grant is not None
        profile = next(
            (
                LiveProfile.model_validate(p["profile"])
                for p in grant["manifest"]["profiles"]
                if p["profile_hash"] == calls[0]["profile_hash"]
            ),
            None,
        )
        if profile is None or profile.model_id != receipt.model:
            raise DomainError(
                "BILLING_SCOPE_MISMATCH", "Provider model differs from frozen profile"
            )
        identities = {
            (
                p["profile"]["model_id"],
                p["profile"]["input_micros_per_million"],
                p["profile"]["output_micros_per_million"],
            )
            for p in grant["manifest"]["profiles"]
        }
        if identities != {
            (profile.model_id, profile.input_micros_per_million, profile.output_micros_per_million)
        }:
            raise DomainError("BILLING_SCOPE_MISMATCH", "Mixed provider models or prices")
        starts = conn.execute(
            "SELECT count(*) AS n FROM app.run_events WHERE run_id=%s "
            "AND kind='model_reserved' AND safe_payload->>'call_id'=ANY(%s) "
            "AND created_at>=%s AND created_at<%s",
            (run["run_id"], [str(x) for x in call_ids], receipt.start, receipt.end),
        ).fetchone()
        if not starts or starts["n"] != 2:
            raise DomainError(
                "BILLING_SCOPE_MISMATCH", "Requests fall outside provider billing hour"
            )
        known = conn.execute(
            "SELECT DISTINCT c.call_id,c.usage FROM app.usage_calls c "
            "JOIN app.run_events e ON e.run_id=c.run_id AND e.kind='model_reserved' "
            "AND e.safe_payload->>'call_id'=c.call_id::text WHERE c.status='known' "
            "AND c.final_budget_hash=%s AND e.created_at>=%s AND e.created_at<%s",
            (calls[0]["final_budget_hash"], receipt.start, receipt.end),
        ).fetchall()
        if receipt.request_count != len(known) + 2:
            raise DomainError(
                "BILLING_SCOPE_MISMATCH", "Invoice request count is not fully attributable"
            )
        hits = misses = outputs = 0
        for row in known:
            u = row["usage"]
            if any(type(u.get(k)) is not int for k in ("prompt_tokens", "completion_tokens")):
                raise DomainError("BILLING_SCOPE_MISMATCH", "Known calls lack individual usage")
            cached = u.get(
                "prompt_cache_hit_tokens",
                u.get("prompt_tokens_details", {}).get("cached_tokens", 0),
            )
            hits += cached
            misses += u.get("prompt_cache_miss_tokens", u["prompt_tokens"] - cached)
            outputs += u["completion_tokens"]
        hit_delta = receipt.input_cache_hit_tokens - hits
        miss_delta = receipt.input_cache_miss_tokens - misses
        output_delta = receipt.output_tokens - outputs
        if min(hit_delta, miss_delta, output_delta) < 0:
            raise DomainError(
                "BILLING_SCOPE_MISMATCH", "Invoice totals are below known ledger usage"
            )
        numerator = (
            hit_delta + miss_delta
        ) * profile.input_micros_per_million + output_delta * profile.output_micros_per_million
        # Two original per-call ceil operations can exceed a group ceil by at most one microdollar.
        upper = (numerator + 999999) // 1000000 + (1 if numerator else 0)
        if upper > min(c["reserved_micros"] for c in calls):
            raise DomainError(
                "BILLING_SCOPE_MISMATCH", "Aggregate cannot prove each call stays under its cap"
            )
        allocations = [upper // 2, upper - upper // 2]
        for call, allocated in zip(
            sorted(calls, key=lambda c: str(c["call_id"])), allocations, strict=True
        ):
            engine.settle_call(
                call["call_id"],
                allocated,
                {
                    "basis": "provider_hourly_aggregate_cost_allocation",
                    "receipt_hash": receipt_hash,
                    "per_call_tokens_unknown": True,
                    "allocated_upper_micros": allocated,
                },
                None,
            )
        summary = {
            "receipt_hash": receipt_hash,
            "call_ids": [str(x) for x in call_ids],
            "known_requests": len(known),
            "provider_requests": receipt.request_count,
            "group_prompt_tokens": hit_delta + miss_delta,
            "group_cache_hit_tokens": hit_delta,
            "group_cache_miss_tokens": miss_delta,
            "provider_hour_start": receipt.start.isoformat(),
            "provider_hour_end": receipt.end.isoformat(),
            "group_completion_tokens": output_delta,
            "group_upper_micros": upper,
            "currency": "USD_FROZEN_UPPER_BOUND",
            "invoice_currency": receipt.currency,
            "per_call_usage": "unknown; costs allocated within one Run",
            "basis": "owner-supplied complete provider hourly bill; no FX conversion",
            "no_resend": True,
            "approved_by": receipt.approved_by,
        }
        conn.execute(
            "INSERT INTO app.artifacts(content_hash,byte_count) VALUES (%s,%s) "
            "ON CONFLICT DO NOTHING",
            (receipt_hash, len(raw)),
        )
        engine._event(conn, run["run_id"], "aggregate_usage_reconciled", **summary)
        conn.execute(
            "UPDATE app.runs SET reason_code='BILLING_RECONCILED_RESPONSE_LOST' WHERE run_id=%s",
            (run["run_id"],),
        )
        return summary
