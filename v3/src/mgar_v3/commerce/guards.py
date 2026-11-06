"""Prepare file/SQL proof outside business transactions; recheck mutable authority in SQL."""

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import UUID

from mgar_v3.domain import DomainError

if TYPE_CHECKING:
    from mgar_v3.commerce.store import Commerce
    from mgar_v3.coordination.engine import Connection, Row


def prepare_action(store: "Commerce", action_id: UUID) -> None:
    e = store.engine
    with e.transaction() as conn:
        action, run = e._action(conn, action_id)
        if not store.binding(run["run_id"]):
            return
        conn.execute("DELETE FROM app.commerce_action_validations WHERE action_id=%s", (action_id,))
    store.require_result(run["run_id"], action["payload"].get("diff_hash"))
    store.bound_permit(run["run_id"], external_write=True)
    store.snapshot(run["run_id"])
    reader = store.reader_for_run(run["run_id"])
    if reader is None:
        raise DomainError("DATA_SOURCE_NOT_CONFIGURED", "Registered reader is unavailable")
    reader.query(f"SELECT count(*) FROM {reader.profile['schema']}.orders", [])
    e.repairs.validate_action(action, run)
    with e.transaction() as conn:
        current, latest = e._action(conn, action_id)
        guard_action(store, conn, current, latest, require_lease=False)
        if current["payload_hash"] != action["payload_hash"]:
            raise DomainError("PAYLOAD_CHANGED", "Proposal changed during preparation")
        link = store.binding(run["run_id"])
        assert link is not None
        conn.execute(
            "INSERT INTO app.commerce_action_validations VALUES (%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT(action_id) DO UPDATE SET payload_hash=excluded.payload_hash,"
            "case_revision=excluded.case_revision,input_revision=excluded.input_revision,"
            "execution_epoch=excluded.execution_epoch,valid_until=excluded.valid_until",
            (
                action_id,
                action["payload_hash"],
                link["revision"],
                latest["input_revision"],
                latest["execution_epoch"],
                datetime.now(UTC) + timedelta(seconds=5),
            ),
        )


def guard_action(
    store: "Commerce", conn: "Connection", action: "Row", run: "Row", *, require_lease: bool = True
) -> None:
    link = store.binding(run["run_id"])
    assert link is not None
    result = store.require_result(run["run_id"], action["payload"].get("diff_hash"))
    if (
        link["stage"] != "repair_proposal"
        or action["payload"].get("business_result_hash") != result["result_hash"]
        or action["payload"].get("spec_hash") != result["spec_hash"]
    ):
        raise DomainError("BUSINESS_RESULT_CHANGED", "Action does not bind the current result")
    if (
        require_lease
        and not conn.execute(
            "SELECT 1 FROM app.commerce_action_validations WHERE action_id=%s AND payload_hash=%s "
            "AND case_revision=%s AND input_revision=%s AND "
            "execution_epoch=%s AND valid_until>clock_timestamp()",
            (
                action["action_id"],
                action["payload_hash"],
                link["revision"],
                run["input_revision"],
                run["execution_epoch"],
            ),
        ).fetchone()
    ):
        raise DomainError(
            "BUSINESS_VALIDATION_EXPIRED", "Prepare the current source proof before sending"
        )
