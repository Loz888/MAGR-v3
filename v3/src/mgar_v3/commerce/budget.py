"""P0-specific frozen grant; every charge remains on the original usage ledger."""

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated, Any
from uuid import UUID

from psycopg.types.json import Jsonb
from pydantic import Field

from mgar_v3.domain import DomainError, Model, digest

if TYPE_CHECKING:
    from mgar_v3.commerce.store import Commerce
    from mgar_v3.coordination.engine import Connection


class Authorization(Model):
    manifest_hash: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    approved_by: str
    expires_at: datetime
    allow_paid: bool
    max_new_calls: Annotated[int, Field(strict=True, gt=0, le=18)]
    max_new_cost_micros: Annotated[int, Field(strict=True, gt=0, le=55296)]


def authorize(store: "Commerce", manifest: dict[str, Any], auth: Authorization) -> str:
    e = store.engine
    if (
        not auth.allow_paid
        or not e.settings.allow_paid
        or digest(manifest) != auth.manifest_hash
        or auth.approved_by.lower() != e.settings.local_actor.lower()
        or auth.expires_at.tzinfo is None
        or auth.expires_at <= datetime.now(UTC)
        or manifest.get("repository") != e.settings.repository.lower()
        or manifest.get("max_new_calls") != auth.max_new_calls
        or manifest.get("max_new_cost_micros") != auth.max_new_cost_micros
    ):
        raise DomainError("COMMERCE_AUTH_REQUIRED", "Exact unexpired P0 grant required", 403)
    with e.transaction() as conn:
        old = conn.execute(
            "SELECT * FROM app.commerce_live_grants WHERE manifest_hash=%s", (auth.manifest_hash,)
        ).fetchone()
        if old:
            if old["authorization_payload"] != auth.model_dump(mode="json"):
                raise DomainError("COMMERCE_GRANT_CHANGED", "A frozen grant cannot be reset")
            return auth.manifest_hash
        cost = e.benchmarks.cost(conn, e.settings.repository.lower())
        if manifest.get("prior_cost_micros") != int(cost["cost_micros"]):
            raise DomainError("COMMERCE_PREDECESSOR_CHANGED", "Historical repository cost changed")
        e.benchmarks.check_global(conn, e.settings.repository.lower(), auth.max_new_cost_micros)
        conn.execute(
            "INSERT INTO app.commerce_live_grants VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                auth.manifest_hash,
                e.settings.repository.lower(),
                e.settings.local_actor.lower(),
                Jsonb(manifest),
                Jsonb(auth.model_dump(mode="json")),
                auth.max_new_calls,
                auth.max_new_cost_micros,
                auth.expires_at,
            ),
        )
    return auth.manifest_hash


def bind(store: "Commerce", conn: "Connection", run_id: UUID) -> None:
    e = store.engine
    run = e._run(conn, run_id)
    if run["execution_mode"] != "live":
        return
    link = store.binding(run_id)
    assert link is not None
    m = store.manifest(run_id)
    grant = conn.execute(
        "SELECT * FROM app.commerce_live_grants WHERE manifest_hash=%s",
        (m.get("live", {}).get("grant_hash"),),
    ).fetchone()
    if (
        not grant
        or grant["expires_at"] <= datetime.now(UTC)
        or grant["actor"] != e.settings.local_actor.lower()
    ):
        raise DomainError("COMMERCE_AUTH_REQUIRED", "No current frozen P0 grant exists", 403)
    spec = link["spec"]
    stage = "repair" if link["stage"] == "repair_proposal" else "investigation"
    expected = {
        "project_id": spec["project_id"],
        "issue_number": spec["issue_number"],
        "template_id": spec["task_kind"],
        "stage": stage,
        "profile_hash": spec["authorization_refs"][stage + "_profile_hash"],
    }
    if not any(
        all(p.get(k) == v for k, v in expected.items())
        for p in grant["manifest"].get("profiles", [])
    ):
        raise DomainError("COMMERCE_SCOPE_DENIED", "Stage is outside the frozen P0 grant", 403)
    old = conn.execute(
        "SELECT * FROM app.commerce_live_runs WHERE manifest_hash=%s AND "
        "project_id=%s AND issue_number=%s AND stage=%s",
        (grant["manifest_hash"], spec["project_id"], spec["issue_number"], stage),
    ).fetchone()
    if old and old["run_id"] != run_id:
        raise DomainError(
            "COMMERCE_ALREADY_EXECUTED", "This stage cannot silently acquire a second Run"
        )
    conn.execute(
        "INSERT INTO app.commerce_live_runs VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
        (
            run_id,
            grant["manifest_hash"],
            spec["project_id"],
            spec["issue_number"],
            stage,
            expected["profile_hash"],
        ),
    )


def reserve(store: "Commerce", conn: "Connection", run_id: UUID, amount: int) -> None:
    if not store.binding(run_id):
        return
    grant = conn.execute(
        "SELECT g.* FROM app.commerce_live_runs r JOIN "
        "app.commerce_live_grants g USING(manifest_hash) WHERE "
        "r.run_id=%s FOR UPDATE OF g",
        (run_id,),
    ).fetchone()
    if not grant or grant["expires_at"] <= datetime.now(UTC):
        raise DomainError("COMMERCE_AUTH_REQUIRED", "A current shared P0 grant is required", 403)
    used = conn.execute(
        "SELECT count(*) AS calls,coalesce(sum(CASE WHEN u.status='known' "
        "AND u.settled_micros IS NOT NULL THEN u.settled_micros ELSE "
        "u.reserved_micros END),0) AS cost FROM app.usage_calls u JOIN "
        "app.commerce_live_runs r USING(run_id) WHERE r.manifest_hash=%s",
        (grant["manifest_hash"],),
    ).fetchone()
    assert used is not None
    if (
        used["calls"] >= grant["max_new_calls"]
        or used["cost"] + amount > grant["max_new_cost_micros"]
    ):
        raise DomainError("BUDGET_EXHAUSTED", "Shared P0 call or cost ceiling exhausted", 403)
