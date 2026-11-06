from typing import TYPE_CHECKING
from uuid import UUID

from mgar_v3.commerce.provider import CommerceExperts, CommercePatcher
from mgar_v3.commerce.results import commit_failure
from mgar_v3.domain import DomainError

if TYPE_CHECKING:
    from mgar_v3.commerce.store import Commerce
    from mgar_v3.graph.runner import Runner


def execute(store: "Commerce", owner: "Runner", run_id: UUID) -> UUID | None:
    e = store.engine
    link = store.binding(run_id)
    assert link is not None
    try:
        m = store.manifest(run_id)
        store.knowledge_scope(m)
        store.snapshot(run_id)
        reader = store.reader_for_run(run_id)
        if reader is None:
            raise DomainError("DATA_SOURCE_NOT_CONFIGURED", "Registered data source is missing")
        count = reader.query(
            f"SELECT count(*) AS events FROM {reader.profile['schema']}.orders", []
        )
        if count["rows"][0][0] > 100:
            raise DomainError("DATA_LIMIT", "Complete data exceeds the registered size")
        permit = store.bound_permit(run_id)
    except DomainError as exc:
        run = e.begin_run(run_id)
        owner.execution_epoch = run["execution_epoch"]
        intake = e.prepare_intake(run_id, owner.execution_epoch)
        e.acknowledge_intake(run_id, owner.execution_epoch, intake["snapshot_id"])
        commit_failure(
            store,
            run_id,
            owner.execution_epoch,
            "denied" if exc.status == 403 or exc.code == "KNOWLEDGE_REVOKED" else "needs_input",
            exc.code,
        )
        return None
    try:
        if link["stage"] == "repair_proposal":
            from mgar_v3.repair.graph import execute_repair

            action = execute_repair(owner, run_id, CommercePatcher(e, run_id, permit))
            current = e.get_run(run_id)
            result = store.current_result(link["case_id"], "repair_proposal")
            if (
                action is None
                and current["status"] == "waiting_input"
                and (
                    not result
                    or result["input_revision"] != current["input_revision"]
                    or result["execution_epoch"] != current["execution_epoch"]
                )
            ):
                code = current["reason_code"] or "REPAIR_EXHAUSTED"
                commit_failure(
                    store,
                    run_id,
                    owner.execution_epoch,
                    "needs_input" if code == "BASELINE_NOT_REPRODUCED" else "failed",
                    code,
                )
            return action
        from mgar_v3.graph.experts import execute_experts

        return execute_experts(owner, run_id, CommerceExperts(e, run_id, permit))
    except DomainError as exc:
        if exc.code in {"STALE_INPUT", "STALE_RESULT"}:
            e.requeue_input(run_id, owner.execution_epoch)
            return None
        if exc.code in {"FENCED", "RUN_NOT_QUEUED"}:
            raise
        commit_failure(
            store,
            run_id,
            owner.execution_epoch,
            "unknown"
            if exc.code == "USAGE_UNKNOWN"
            else "denied"
            if exc.status == 403
            else "needs_input"
            if exc.code in {"DATA_CONFLICT", "SQL_STALE", "DATA_SOURCE_CHANGED"}
            else "failed",
            exc.code,
        )
        if exc.code == "USAGE_UNKNOWN":
            raise
        return None
