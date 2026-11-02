from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from mgar_v3.domain import DomainError, digest
from mgar_v3.evaluation.domain import TrialAuthorization
from mgar_v3.live import LiveProfile

if TYPE_CHECKING:
    from mgar_v3.coordination.engine import Engine, Row


class Trials:
    def __init__(self, engine: "Engine"):
        self.engine = engine

    def authorize(self, manifest: dict[str, Any], auth: TrialAuthorization) -> "Row":
        if (
            auth.manifest_hash != digest(manifest)
            or not auth.allow_paid
            or auth.expires_at.tzinfo is None
            or auth.expires_at <= datetime.now(UTC)
            or auth.approved_by.lower() != self.engine.settings.local_actor.lower()
            or auth.additional_call_limit != manifest["additional_call_limit"]
            or auth.predecessor_budget_hash != manifest["predecessor_budget_hash"]
        ):
            raise DomainError(
                "TRIAL_AUTHORIZATION_INVALID",
                "Trial requires an exact, unexpired user call grant",
                403,
            )
        with self.engine.transaction() as conn:
            old = conn.execute(
                "SELECT * FROM app.authorization_budgets WHERE profile_hash=%s FOR UPDATE",
                (auth.predecessor_budget_hash,),
            ).fetchone()
            if not old or old["used_calls"] != old["call_limit"] or old["reserved_micros"]:
                raise DomainError(
                    "TRIAL_PREDECESSOR_CHANGED", "Prior budget is not settled and exhausted"
                )
            if auth.cumulative_cost_limit_micros > old["limit_micros"]:
                raise DomainError(
                    "TRIAL_BUDGET_CHANGED", "Trial cannot increase the earlier monetary ceiling"
                )
            available = auth.cumulative_cost_limit_micros - old["settled_micros"]
            if available <= 0 or old["settled_micros"] != manifest["prior_cost_micros"]:
                raise DomainError(
                    "TRIAL_BUDGET_CHANGED", "Cumulative USD budget or prior cost changed"
                )
            budget_hash = digest(
                {"trial": digest(manifest), "authorization": auth.model_dump(mode="json")}
            )
            existing = conn.execute(
                "SELECT * FROM app.r4_trials WHERE manifest_hash=%s", (digest(manifest),)
            ).fetchone()
            if existing:
                if existing["budget_hash"] != budget_hash:
                    raise DomainError(
                        "TRIAL_GRANT_CHANGED", "Trial already has a different immutable grant"
                    )
                return existing
            conn.execute(
                (
                    "INSERT INTO "
                    "app.authorization_budgets(profile_hash,limit_micros,call_limit) "
                    "VALUES (%s,%s,%s)"
                ),
                (budget_hash, available, auth.additional_call_limit),
            )
            row = conn.execute(
                (
                    "INSERT INTO "
                    "app.r4_trials(trial_id,manifest_hash,manifest,predecessor_budget_"
                    "hash,budget_hash,authorization_payload) VALUES (%s,%s,%s,%s,%s,%s) "
                    "RETURNING *"
                ),
                (
                    uuid4(),
                    digest(manifest),
                    Jsonb(manifest),
                    auth.predecessor_budget_hash,
                    budget_hash,
                    Jsonb(auth.model_dump(mode="json")),
                ),
            ).fetchone()
            assert row is not None
            return row

    def bind(self, trial_id: UUID, profile: LiveProfile, case_id: str, arm: str) -> None:
        with self.engine.transaction() as conn:
            trial = conn.execute(
                "SELECT * FROM app.r4_trials WHERE trial_id=%s", (trial_id,)
            ).fetchone()
            if not trial or arm not in {"multi", "single"}:
                raise DomainError("TRIAL_SCOPE_DENIED", "Trial profile scope is invalid")
            previous = conn.execute(
                ("SELECT trial_id,case_id,arm FROM app.r4_trial_profiles WHERE profile_hash=%s"),
                (profile.profile_hash,),
            ).fetchone()
            if previous and (
                previous["trial_id"] != trial_id
                or previous["case_id"] != case_id
                or previous["arm"] != arm
            ):
                raise DomainError(
                    "TRIAL_PROFILE_SHARED", "A profile cannot silently switch its shared call grant"
                )
            manifest = trial["manifest"]
            frozen = next(
                (p for p in manifest["profiles"] if p["case_id"] == case_id and p["arm"] == arm),
                None,
            )
            if not frozen or frozen["profile"] != profile.model_dump(mode="json"):
                raise DomainError(
                    "TRIAL_PROFILE_CHANGED", "Profile differs from the frozen case and arm"
                )
            conn.execute(
                (
                    "INSERT INTO "
                    "app.authorization_budgets(profile_hash,limit_micros,call_limit) "
                    "VALUES (%s,%s,%s) ON CONFLICT DO NOTHING"
                ),
                (profile.profile_hash, profile.max_cost_micros, profile.max_model_calls),
            )
            conn.execute(
                (
                    "INSERT INTO "
                    "app.r4_trial_profiles(profile_hash,trial_id,case_id,arm) VALUES "
                    "(%s,%s,%s,%s) ON CONFLICT DO NOTHING"
                ),
                (profile.profile_hash, trial_id, case_id, arm),
            )

    def bind_recovery(
        self,
        trial_id: UUID,
        parent_run_id: UUID,
        profile: LiveProfile,
        implementation: str,
        reason: str,
    ) -> None:
        # One bounded recovery profile may share the existing grant. No counter is reset.
        with self.engine.transaction() as conn:
            parent = self.engine._run(conn, parent_run_id)
            mapping = conn.execute(
                (
                    "SELECT p.*,t.manifest,t.authorization_payload,t.budget_hash FROM "
                    "app.r4_trial_profiles p JOIN app.r4_trials t USING(trial_id) "
                    "JOIN app.mandates m ON m.scope->>%s=p.profile_hash WHERE "
                    "m.mandate_id=%s AND p.trial_id=%s"
                ),
                ("profile_hash", parent["mandate_id"], trial_id),
            ).fetchone()
            if not mapping or mapping["arm"] != "multi" or parent["status"] != "failed":
                raise DomainError(
                    "RECOVERY_SCOPE_DENIED", "Only a failed multi-expert arm may use this recovery"
                )
            original = next(
                p["profile"]
                for p in mapping["manifest"]["profiles"]
                if p["case_id"] == mapping["case_id"] and p["arm"] == "multi"
            )
            expected = {**original, "case_id": original["case_id"] + ":recovery-1"}
            if profile.model_dump(mode="json") != expected:
                raise DomainError(
                    "RECOVERY_INPUT_CHANGED",
                    ("Recovery must preserve question, model, data, source and per-run limits"),
                )
            auth = TrialAuthorization.model_validate(mapping["authorization_payload"])
            if auth.expires_at <= datetime.now(UTC):
                raise DomainError("TRIAL_AUTHORIZATION_EXPIRED", "Trial grant expired")
            unresolved = conn.execute(
                ("SELECT 1 FROM app.usage_calls WHERE trial_budget_hash=%s AND status!='known'"),
                (mapping["budget_hash"],),
            ).fetchone()
            if unresolved:
                raise DomainError(
                    "USAGE_UNKNOWN", "Resolve uncertain trial requests before recovery"
                )
            existing = conn.execute(
                (
                    "SELECT profile_hash FROM app.r4_trial_profiles WHERE trial_id=%s "
                    "AND case_id=%s AND arm=%s"
                ),
                (trial_id, mapping["case_id"] + ":recovery-1", "multi"),
            ).fetchone()
            if existing:
                if existing["profile_hash"] != profile.profile_hash:
                    raise DomainError(
                        "RECOVERY_ALREADY_BOUND", "A different recovery already exists"
                    )
                return
            budget = conn.execute(
                ("SELECT * FROM app.authorization_budgets WHERE profile_hash=%s FOR UPDATE"),
                (mapping["budget_hash"],),
            ).fetchone()
            if not budget or budget["call_limit"] - budget["used_calls"] < profile.max_model_calls:
                raise DomainError(
                    "BUDGET_EXHAUSTED", "Existing shared grant cannot cover this bounded recovery"
                )
            conn.execute(
                (
                    "INSERT INTO "
                    "app.authorization_budgets(profile_hash,limit_micros,call_limit) "
                    "VALUES (%s,%s,%s)"
                ),
                (profile.profile_hash, profile.max_cost_micros, profile.max_model_calls),
            )
            conn.execute(
                (
                    "INSERT INTO "
                    "app.r4_trial_profiles(profile_hash,trial_id,case_id,arm) VALUES "
                    "(%s,%s,%s,%s)"
                ),
                (profile.profile_hash, trial_id, mapping["case_id"] + ":recovery-1", "multi"),
            )
            self.engine._event(
                conn,
                parent_run_id,
                "trial_recovery_bound",
                profile_hash=profile.profile_hash,
                trial_id=str(trial_id),
                implementation_sha256=implementation,
                reason=reason,
                frozen_manifest_hash=digest(mapping["manifest"]),
                grant_increased=False,
            )

    def reserve(self, conn: Any, profile_hash: str, amount: int) -> str | None:
        row = conn.execute(
            "SELECT t.* FROM app.r4_trial_profiles p JOIN app.r4_trials t USING(trial_id) "
            "WHERE p.profile_hash=%s",
            (profile_hash,),
        ).fetchone()
        if not row:
            return None
        if conn.execute(
            "SELECT 1 FROM app.usage_calls WHERE trial_budget_hash=%s AND status='unknown'",
            (row["budget_hash"],),
        ).fetchone():
            raise DomainError("USAGE_UNKNOWN", "An unknown trial request requires reconciliation")
        auth = TrialAuthorization.model_validate(row["authorization_payload"])
        if not auth.allow_paid or auth.expires_at <= datetime.now(UTC):
            raise DomainError("TRIAL_AUTHORIZATION_EXPIRED", "Shared trial call grant expired", 403)
        updated = conn.execute(
            (
                "UPDATE app.authorization_budgets SET "
                "used_calls=used_calls+1,reserved_micros=reserved_micros+%s WHERE "
                "profile_hash=%s AND used_calls<call_limit AND "
                "reserved_micros+settled_micros+%s<=limit_micros RETURNING "
                "profile_hash"
            ),
            (amount, row["budget_hash"], amount),
        ).fetchone()
        if not updated:
            raise DomainError(
                "BUDGET_EXHAUSTED", "Shared trial call or cumulative USD budget is exhausted", 403
            )
        return str(row["budget_hash"])
