"""Read-only projection of four authoritative ledgers; no inferred business success."""

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from mgar_v3.commerce.review import certificate, fresh
from mgar_v3.commerce.view import render
from mgar_v3.domain import DomainError

if TYPE_CHECKING:
    from mgar_v3.commerce.store import Commerce


def result_view(store: "Commerce", case_id: UUID) -> dict[str, Any]:
    e = store.engine
    case = store.case(case_id)
    with e.transaction() as conn:
        links = conn.execute(
            "SELECT * FROM app.commerce_case_runs WHERE case_id=%s AND "
            "revision=%s ORDER BY stage DESC",
            (case_id, case["current_revision"]),
        ).fetchall()
    link = links[0]
    run = e.get_run(link["run_id"])
    result = store.current_result(case_id, link["stage"])
    access = "allowed"
    try:
        manifest = store.manifest(run["run_id"])
        store.knowledge_scope(manifest)
    except DomainError as exc:
        access = "revoked" if exc.status == 403 else "unknown"
    actions = []
    if access == "allowed":
        try:
            actions = e.actions(run["run_id"])
        except DomainError:
            try:
                store.knowledge_scope(store.manifest(run["run_id"]))
            except DomainError as exc:
                access = "revoked" if exc.status == 403 else "unknown"
            else:
                access = "unknown"
    action = actions[-1] if actions else None
    with e.transaction() as conn:
        unknown_model = bool(
            conn.execute(
                "SELECT 1 FROM app.usage_calls u JOIN app.commerce_case_runs l "
                "USING(run_id) WHERE l.case_id=%s AND u.status IN "
                "('reserved','unknown')",
                (case_id,),
            ).fetchone()
        )
        primary = bool(
            conn.execute(
                "SELECT 1 FROM app.subscriptions WHERE thread_id=%s AND active AND role='primary'",
                (case["thread_id"],),
            ).fetchone()
        )
        unknown_delivery = bool(
            conn.execute(
                "SELECT 1 FROM app.actions a JOIN app.commerce_case_runs l "
                "USING(run_id) WHERE l.case_id=%s AND a.status='outcome_unknown'",
                (case_id,),
            ).fetchone()
        )
        grant = bool(
            action
            and conn.execute(
                "SELECT 1 FROM app.target_write_grants WHERE target=%s AND active "
                "AND run_id=%s AND execution_epoch=%s AND fencing_token=%s",
                (
                    action["target"],
                    run["run_id"],
                    action["execution_epoch"],
                    action["fencing_token"],
                ),
            ).fetchone()
        )
        approved = bool(
            action
            and conn.execute(
                "SELECT 1 FROM app.approvals WHERE action_id=%s AND payload_hash=%s",
                (action["action_id"], action["payload_hash"]),
            ).fetchone()
        )
    applicable = bool(
        result
        and result["spec_hash"] == case["spec_hash"]
        and result["input_revision"] == run["input_revision"] == run["thread_revision"]
        and result["execution_epoch"] == run["execution_epoch"]
        and fresh(store, result, action["action_id"] if action else None, now=datetime.now(UTC))
        and access == "allowed"
    )
    permit_valid = False
    if access == "allowed":
        try:
            store.permit(manifest, "repair")
            permit_valid = case["spec"]["authorization_refs"]["repair_profile_hash"] is not None
        except (DomainError, OSError, ValueError):
            pass
    binding = bool(
        action
        and result
        and applicable
        and result["payload"]["business_status"] == "passed"
        and action["payload"].get("business_result_hash") == result["result_hash"]
        and action["payload"].get("spec_hash") == case["spec_hash"]
        and action["base_revision"] == run["thread_revision"]
        and action["execution_epoch"] == run["execution_epoch"]
    )
    status = action["status"] if action else None
    expiry = (action.get("review_expires_at") or action["expires_at"]) if action else None
    unexpired = expiry is not None and expiry > datetime.now(UTC)
    approval = (
        "rejected"
        if status == "rejected"
        else "expired"
        if action and not unexpired
        else "approved"
        if approved
        else "pending"
        if action
        else "not_requested"
    )
    delivery = {
        "proposed": "awaiting_approval",
        "ready": "not_started",
        "dispatching": "publishing",
        "succeeded": "delivered",
        "outcome_unknown": "unknown",
        "failed": "failed",
        "cancelled": "cancelled",
        "rejected": "cancelled",
        "expired": "not_started",
    }.get(status or "", "not_requested")
    review = certificate(store, action["action_id"], result) if action and result else None
    view = {
        "schema": "commerce-case-view-v1",
        "case_id": str(case_id),
        "case_revision": case["current_revision"],
        "current_spec_hash": case["spec_hash"],
        "as_of": datetime.now(UTC).isoformat(),
        "goal": case["spec"]["goal"],
        "phase": "repair" if link["stage"] == "repair_proposal" else "investigation",
        "execution": {
            "run_id": str(run["run_id"]),
            "status": run["status"],
            "input_revision": run["input_revision"],
            "epoch": run["execution_epoch"],
        },
        "business": {
            "status": result["payload"]["business_status"] if result else "not_evaluated",
            "result_id": str(result["result_id"]) if result else None,
            "spec_hash": result["spec_hash"] if result else None,
            "applies_to_current_input": applicable,
            "source_access": access,
            "review_certificate_hash": review["certificate_hash"] if review else None,
        },
        "approval": approval,
        "delivery": "not_started"
        if not action and case["spec"]["goal"] == "draft_pr"
        else delivery,
        "uncertainty": "both"
        if unknown_model and unknown_delivery
        else "model"
        if unknown_model
        else "delivery"
        if unknown_delivery
        else "none",
        "current_actor_authorized": True,
        "eligibility": {
            "repair_permit_valid": permit_valid,
            "action_binding_current": binding,
            "approval_unexpired": unexpired,
            "stage_primary_available": not primary,
            "write_grant_current": grant,
        },
    }
    view["rendered"] = render(view)
    # Revoked source payloads are never rendered or returned, even after a historical PASS.
    return {
        "view": view,
        "result": result["payload"] if result and access == "allowed" else None,
        "result_hash": result["result_hash"] if result and access == "allowed" else None,
        "actions": actions if access == "allowed" else [],
    }
