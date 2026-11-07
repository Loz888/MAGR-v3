"""Explicit operator recheck of exact pending payloads; no models or remote writes.

A separate append-only certificate renews observations, never historical results.
It is usable only for its exact Action/result/input/epoch and expires with the SQL read.
"""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from mgar_v3.commerce.domain import Window
from mgar_v3.commerce.numeric import calculate, observed_rows
from mgar_v3.commerce.provider import catalog
from mgar_v3.domain import DomainError, digest
from mgar_v3.repair.hidden import suite_for_run
from mgar_v3.repair.sandbox import Sandbox
from mgar_v3.repair.source import github_commit, working_files

if TYPE_CHECKING:
    from mgar_v3.commerce.store import Commerce
    from mgar_v3.coordination.engine import Row


def certificate(store: "Commerce", action_id: UUID, result: "Row") -> "Row | None":
    """SQL-only current attestation. A later failure supersedes any earlier PASS."""
    e = store.engine
    with e.transaction() as conn:
        row = conn.execute(
            "SELECT c.*,a.payload_hash AS current_hash,a.run_id,a.base_revision,"
            "a.execution_epoch AS action_epoch,a.payload FROM app.commerce_review_checks c "
            "JOIN app.actions a USING(action_id) WHERE c.action_id=%s "
            "ORDER BY c.created_at DESC,c.check_id DESC LIMIT 1",
            (action_id,),
        ).fetchone()
    if (
        not row
        or row["status"] != "passed"
        or row["fresh_until"] <= datetime.now(UTC)
        or row["payload_hash"] != row["current_hash"]
        or row["result_id"] != result["result_id"]
        or row["run_id"] != result["run_id"]
        or row["case_revision"] != result["case_revision"]
        or row["input_revision"] != result["input_revision"]
        or row["input_revision"] != row["base_revision"]
        or row["execution_epoch"] != result["execution_epoch"]
        or row["execution_epoch"] != row["action_epoch"]
        or row["payload"].get("business_result_hash") != result["result_hash"]
        or row["payload"].get("spec_hash") != result["spec_hash"]
        or row["payload"].get("diff_hash") != result["artifact_hash"]
    ):
        return None
    return row


def fresh(
    store: "Commerce",
    result: "Row",
    action_id: UUID | None = None,
    *,
    now: datetime | None = None,
) -> bool:
    if action_id is not None:
        with store.engine.transaction() as conn:
            latest = conn.execute(
                "SELECT status FROM app.commerce_review_checks WHERE action_id=%s "
                "ORDER BY created_at DESC,check_id DESC LIMIT 1",
                (action_id,),
            ).fetchone()
        if latest and latest["status"] == "failed":
            return False
    return result["fresh_until"] > (now or datetime.now(UTC)) or bool(
        action_id is not None and certificate(store, action_id, result)
    )


def capture(
    store: "Commerce", action_id: UUID, payload_hash: str, actor: str
) -> tuple["Row", "Row", "Row", "Row"]:
    e = store.engine
    if actor.lower() != e.settings.local_actor.lower() or actor.lower() not in {
        a.lower() for a in e.settings.allowed_actors
    }:
        raise DomainError("ACTOR_DENIED", "Only the configured reviewer may recheck", 403)
    with e.transaction() as conn:
        action, run = e._action(conn, action_id)
        link = store.binding(run["run_id"])
        result = store.current_result(link["case_id"], "repair_proposal") if link else None
        mandate = conn.execute(
            "SELECT scope,policy_version FROM app.mandates WHERE mandate_id=%s",
            (run["mandate_id"],),
        ).fetchone()
        if (
            not link
            or link["stage"] != "repair_proposal"
            or link["revision"] != link["current_revision"]
            or not result
            or result["payload"]["business_status"] != "passed"
            or result["spec_hash"] != link["spec_hash"]
            or result["run_id"] != run["run_id"]
            or result["input_revision"] != run["input_revision"]
            or run["input_revision"] != run["thread_revision"]
            or action["base_revision"] != run["input_revision"]
            or result["execution_epoch"] != run["execution_epoch"]
            or action["execution_epoch"] != run["execution_epoch"]
            or run["status"] != "waiting_approval"
            or action["status"] != "proposed"
            or action["action_type"] != "draft_pr"
            or action["payload_hash"] != payload_hash
            or action["payload"].get("business_result_hash") != result["result_hash"]
            or action["payload"].get("spec_hash") != result["spec_hash"]
            or action["payload"].get("diff_hash") != result["artifact_hash"]
        ):
            raise DomainError("STALE_ACTION", "Recheck requires the exact unsent current proposal")
        expected = digest(
            {
                "type": action["action_type"],
                "target": action["target"],
                "payload": action["payload"],
                "base_revision": action["base_revision"],
            }
        )
        if expected != payload_hash:
            raise DomainError("PAYLOAD_CHANGED", "Proposal digest differs from the reviewed hash")
        if any(
            conn.execute(f"SELECT 1 FROM app.{table} WHERE action_id=%s", (action_id,)).fetchone()
            for table in ("outbox", "approvals", "repair_publication_steps")
        ):
            raise DomainError("ACTION_ALREADY_SENT", "Started proposals cannot use recheck")
        grant = conn.execute(
            "SELECT 1 FROM app.target_write_grants WHERE target=%s AND run_id=%s "
            "AND execution_epoch=%s AND fencing_token=%s AND active",
            (action["target"], run["run_id"], run["execution_epoch"], action["fencing_token"]),
        ).fetchone()
        primary = conn.execute(
            "SELECT 1 FROM app.subscriptions WHERE run_id=%s AND thread_id=%s "
            "AND active AND role='primary'",
            (run["run_id"], run["thread_id"]),
        ).fetchone()
        scope = mandate["scope"] if mandate else {}
        if (
            not grant
            or not primary
            or scope.get("read_only")
            or not mandate
            or mandate["policy_version"] != e.settings.policy_version
            or "draft_pr" not in scope.get("actions", [])
            or scope.get("repository") != action["payload"]["repository"]
            or scope.get("issue_number") != action["payload"]["issue_number"]
        ):
            raise DomainError(
                "WRITE_GRANT_DENIED", "Current mandate and primary grant required", 403
            )
        store.knowledge_scope(store.manifest(run["run_id"]))
    return action, run, link, result


def observations(store: "Commerce", run: "Row", link: "Row", result: "Row") -> dict[str, Any]:
    e, run_id = store.engine, run["run_id"]
    m = store.manifest(run_id)
    store.bound_permit(run_id, external_write=True)
    snapshot = store.snapshot(run_id)
    if run["execution_mode"] == "live":
        commit, entries = github_commit(snapshot.repository, snapshot.base_sha)
        if (
            commit["commit"]["tree"]["sha"] != snapshot.base_tree
            or entries != snapshot.tree_entries
        ):
            raise DomainError("SOURCE_CHANGED", "Fresh GitHub objects differ from the proposal")
    with e.transaction() as conn:
        parent = conn.execute(
            "SELECT * FROM app.business_results WHERE result_id=%s", (link["preceding_result_id"],)
        ).fetchone()
    if not parent or parent["result_hash"] != result["payload"]["parent_investigation_result_hash"]:
        raise DomainError("STALE_RESULT", "Investigation provenance changed")
    numeric = json.loads(e.artifacts.read(parent["payload"]["numeric_receipt_hash"]))
    quote_hashes = []
    for quote in parent["payload"]["quote_refs"]:
        e.tools.check_evidence(UUID(quote["source_id"]))
        content = e.artifacts.read(quote["content_hash"]).decode()
        role = "knowledge" if quote["unit_kind"] == "knowledge_line" else "repo"
        if not any(
            u["unit_hash"] == quote["unit_hash"]
            and u["start_line"] == quote["start_line"]
            and u["end_line"] == quote["end_line"]
            for u in catalog(content, role).values()
        ):
            raise DomainError("QUOTE_UNIT_MISMATCH", "Original quote no longer verifies")
        quote_hashes.append(quote["unit_hash"])
    reader = store.reader_for_run(run_id)
    if reader is None:
        raise DomainError("DATA_SOURCE_NOT_CONFIGURED", "Recheck requires the registered reader")
    schema = reader.profile["schema"]
    raw = reader.query(
        f"SELECT event_id,order_id,status,amount,created_at FROM {schema}.orders ORDER BY event_id",
        [],
    )
    rows = observed_rows(raw)
    metrics = calculate(rows, Window.model_validate(link["spec"]["window"]))
    if (
        digest(rows) != m["data_profile"]["fixture_hash"]
        or digest(rows) != numeric["rows_hash"]
        or metrics.model_dump() != parent["payload"]["facts"]
    ):
        raise DomainError(
            "DATA_SOURCE_CHANGED", "New observations differ; a new proposal is required"
        )
    window = link["spec"]["window"]
    aggregate = reader.query(
        "SELECT count(*) AS paid_events,count(DISTINCT order_id) AS distinct_orders,"
        f"sum(amount) AS event_amount FROM {schema}.orders "
        "WHERE status=%s AND created_at >= %s AND created_at < %s",
        ["paid", window["start"], window["end"]],
    )
    if aggregate["result_hash"] != digest(
        {k: v for k, v in aggregate.items() if k != "result_hash"}
    ):
        raise DomainError("SQL_HASH", "Aggregate receipt changed")
    values = dict(zip([c["name"] for c in aggregate["columns"]], aggregate["rows"][0], strict=True))
    if any(
        Decimal(values[k] or 0) != Decimal(getattr(metrics, k))
        for k in ("paid_events", "distinct_orders", "event_amount")
    ):
        raise DomainError("NUMERIC_EVIDENCE_CONFLICT", "Fresh SQL and Decimal calculation disagree")
    return {
        "sql": [raw, aggregate],
        "rows_hash": digest(rows),
        "facts": metrics.model_dump(),
        "quote_hashes": quote_hashes,
        "source_sha": snapshot.base_sha,
        "source_tree": snapshot.base_tree,
    }


def recheck(store: "Commerce", action_id: UUID, payload_hash: str, actor: str) -> "Row":
    """Must be invoked explicitly after the user reviews this unchanged payload."""
    e = store.engine
    action, run, link, result = capture(store, action_id, payload_hash, actor)
    store.no_unknown()
    proof: dict[str, Any] = {
        "schema": "commerce-exact-review-v1",
        "check_id": str(uuid4()),
        "action_id": str(action_id),
        "payload_hash": payload_hash,
        "actor": actor,
        "result_hash": result["result_hash"],
        "spec_hash": result["spec_hash"],
        "case_revision": link["revision"],
        "input_revision": run["input_revision"],
        "execution_epoch": run["execution_epoch"],
        "started_at": datetime.now(UTC).isoformat(),
        "model_calls": 0,
        "test_receipts": [],
    }
    failure = None
    fresh_until = datetime.now(UTC)
    try:
        e.repairs.validate_action(action, run)
        proof["observations"] = observations(store, run, link, result)
        fresh_until = min(
            datetime.fromisoformat(r["observed_at"]) for r in proof["observations"]["sql"]
        ) + timedelta(seconds=e.settings.r4_sql_ttl_seconds)
        with e.transaction() as conn:
            candidate = conn.execute(
                "SELECT * FROM app.repair_candidates WHERE run_id=%s AND input_revision=%s "
                "AND diff_hash=%s AND verified",
                (run["run_id"], run["input_revision"], action["payload"]["diff_hash"]),
            ).fetchone()
        assert candidate is not None
        snapshot = e.repairs.snapshot(run["run_id"])
        work = working_files(snapshot, json.loads(e.artifacts.read(candidate["files_hash"])))
        hidden = {**work, "zz_mgar_hidden_test.go": suite_for_run(e, run["run_id"])}
        box = Sandbox("commerce-review:" + str(action_id))
        for kind, files, expected, original in (
            ("public", work, snapshot.expected_tests, candidate["public_receipt"]),
            (
                "independent",
                hidden,
                [*snapshot.expected_tests, "TestR5Hidden", "TestCommerceObservedWindow"],
                candidate["hidden_receipt"],
            ),
        ):
            receipt, log = box.run(files, expected)
            receipt["log_hash"] = e.repairs.artifact(log)
            proof["test_receipts"].append({"kind": kind, **receipt})
            if (
                not receipt["passed"]
                or receipt["exit_code"] != 0
                or receipt["skipped"]
                or receipt["stop_reason"] is not None
                or receipt["image_id"] != original["image_id"]
                or receipt["workspace_hash"] != original["workspace_hash"]
                or set(receipt["tests"]) != set(original["tests"])
            ):
                raise DomainError("PROTECTED_TEST_FAILED", "Unchanged proposal failed fresh tests")
        if fresh_until <= datetime.now(UTC):
            raise DomainError("SQL_STALE", "Recheck observations expired before completion")
        proof["status"] = "passed"
    except DomainError as exc:
        failure = exc
        proof.update(status="failed", reason_code=exc.code)
    proof["fresh_until"] = fresh_until.isoformat()
    proof["completed_at"] = datetime.now(UTC).isoformat()
    artifact = e.repairs.artifact(json.dumps(proof, sort_keys=True).encode())
    with e.transaction() as conn:
        _, latest, current_link, current_result = capture(store, action_id, payload_hash, actor)
        store.no_unknown()
        if (
            current_result["result_id"] != result["result_id"]
            or current_link["spec_hash"] != link["spec_hash"]
            or latest["execution_epoch"] != run["execution_epoch"]
        ):
            raise DomainError("FENCED", "Proposal changed while being rechecked")
        row = conn.execute(
            "INSERT INTO app.commerce_review_checks VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,"
            "clock_timestamp()) RETURNING *",
            (
                UUID(proof["check_id"]),
                action_id,
                payload_hash,
                result["result_id"],
                artifact,
                proof["status"],
                actor,
                link["revision"],
                run["input_revision"],
                run["execution_epoch"],
                fresh_until,
            ),
        ).fetchone()
        e._event(
            conn,
            run["run_id"],
            "commerce_review_rechecked",
            action_id=str(action_id),
            payload_hash=payload_hash,
            certificate_hash=artifact,
            status=proof["status"],
        )
    assert row is not None
    if failure:
        raise failure
    return row
