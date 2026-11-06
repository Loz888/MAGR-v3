"""Independent business checks and atomic attachment to the original Run ledger."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from mgar_v3.agents.domain import Resolution
from mgar_v3.commerce.domain import BusinessResult, Check, Checks, QuoteRef, Status, TestRef, Window
from mgar_v3.commerce.numeric import calculate, observed_rows
from mgar_v3.commerce.provider import catalog
from mgar_v3.domain import DomainError, Verification, digest

if TYPE_CHECKING:
    from mgar_v3.commerce.store import Commerce
    from mgar_v3.coordination.engine import Connection, Row


def base_result(
    store: "Commerce", run_id: UUID, epoch: int, status: Status, code: str | None = None
) -> BusinessResult:
    link = store.binding(run_id)
    assert link is not None
    run = store.engine.get_run(run_id)
    passed = Check(status="passed")
    checks = Checks(scope=passed, evidence=passed, contract=passed, acceptance=passed)
    if status != "passed":
        values = checks.model_dump()
        gate = (
            "scope"
            if status == "denied"
            else "acceptance"
            if code == "PROTECTED_TEST_FAILED"
            else "contract"
            if status == "failed"
            else "evidence"
        )
        values[gate] = {
            "status": "unknown"
            if status == "unknown"
            else "failed"
            if status in {"failed", "denied"}
            else "not_run",
            "reason_codes": [code],
        }
        if gate != "acceptance":
            values["acceptance"] = {"status": "not_run", "reason_codes": []}
        checks = Checks.model_validate(values)
    parent = None
    if link["preceding_result_id"]:
        with store.engine.transaction() as conn:
            row = conn.execute(
                "SELECT result_hash FROM app.business_results WHERE result_id=%s",
                (link["preceding_result_id"],),
            ).fetchone()
            parent = row["result_hash"] if row else None
    # Construct final passed payload only after collecting its complete required proof.
    return BusinessResult.model_construct(
        schema_version="commerce-business-result-v1",
        result_id=uuid4(),
        case_id=link["case_id"],
        case_revision=link["revision"],
        stage=link["stage"],
        run_id=run_id,
        input_revision=run["input_revision"],
        execution_epoch=epoch,
        spec_hash=link["spec_hash"],
        checker_version="commerce-checker-v1.0",
        created_at=datetime.now(UTC),
        business_status=status,
        checks=checks,
        reason_codes=[code] if code else [],
        missing_inputs=[code] if status == "needs_input" and code else [],
        artifact_hash=None,
        facts={},
        quote_refs=[],
        numeric_receipt_hash=None,
        test_receipt_refs=[],
        parent_investigation_result_hash=parent,
    )


def persist(
    store: "Commerce",
    conn: "Connection",
    result: BusinessResult,
    result_hash: str,
    fresh_until: datetime,
) -> "Row":
    e = store.engine
    link = store.binding(result.run_id)
    run = e._run(conn, result.run_id, result.execution_epoch)
    case = store.case(result.case_id)
    if (
        not link
        or case["current_revision"] != result.case_revision
        or case["spec_hash"] != result.spec_hash
        or link["revision"] != case["current_revision"]
        or run["input_revision"] != result.input_revision
        or run["thread_revision"] != result.input_revision
        or run["status"]
        not in ({"running"} if result.business_status == "passed" else {"running", "waiting_input"})
    ):
        raise DomainError("STALE_RESULT", "Result no longer matches the current input")
    m = store.manifest(result.run_id)
    if result.business_status == "passed":
        store.knowledge_scope(
            m
        )  # PG only; actual Artifact/SQL checks were performed before this TX.
    value = result.wire()
    subject = result.artifact_hash or digest(
        {"status": result.business_status, "reasons": result.reason_codes}
    )
    old = conn.execute(
        "SELECT * FROM app.business_results WHERE case_id=%s AND case_revision=%s "
        "AND stage=%s AND run_id=%s AND input_revision=%s AND execution_epoch=%s AND spec_hash=%s "
        "AND artifact_hash=%s AND checker_version=%s",
        (
            result.case_id,
            result.case_revision,
            result.stage,
            result.run_id,
            result.input_revision,
            result.execution_epoch,
            result.spec_hash,
            subject,
            result.checker_version,
        ),
    ).fetchone()
    if old:
        previous = dict(old["payload"])
        proposed = dict(value)
        for field in ("result_id", "created_at"):
            previous.pop(field)
            proposed.pop(field)
        if previous != proposed:
            raise DomainError(
                "BUSINESS_RESULT_CHANGED", "A settled business result cannot be replaced"
            )
        row = old
    else:
        inserted = conn.execute(
            "INSERT INTO app.business_results(result_id,case_id,case_revision,stage,"
            "run_id,input_revision,execution_epoch,spec_hash,artifact_hash,checker_version,"
            "fresh_until,result_hash,payload) VALUES "
            "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
            (
                result.result_id,
                result.case_id,
                result.case_revision,
                result.stage,
                result.run_id,
                result.input_revision,
                result.execution_epoch,
                result.spec_hash,
                subject,
                result.checker_version,
                fresh_until,
                result_hash,
                Jsonb(value),
            ),
        ).fetchone()
        assert inserted is not None
        row = inserted
    assert row is not None
    conn.execute(
        "INSERT INTO app.commerce_current_results(case_id,case_revision,stage,result_id) "
        "VALUES (%s,%s,%s,%s) ON CONFLICT(case_id,case_revision,stage) DO "
        "UPDATE SET result_id=excluded.result_id",
        (result.case_id, result.case_revision, result.stage, row["result_id"]),
    )
    e._event(
        conn,
        result.run_id,
        "business_result_recorded",
        result_id=str(row["result_id"]),
        result_hash=row["result_hash"],
        business_status=result.business_status,
    )
    return row


def commit_failure(store: "Commerce", run_id: UUID, epoch: int, status: Status, code: str) -> None:
    result = BusinessResult.model_validate(base_result(store, run_id, epoch, status, code).wire())
    raw = json.dumps(
        result.wire(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    result_hash = store.engine.repairs.artifact(raw)
    with store.engine.transaction() as conn:
        persist(store, conn, result, result_hash, datetime.now(UTC) + timedelta(seconds=300))
        if status == "needs_input":
            conn.execute(
                "UPDATE app.runs SET status='waiting_input',reason_code=%s WHERE run_id=%s",
                (code, run_id),
            )
        else:
            store.engine.fail_run(run_id, code, epoch)


def record_candidate(store: "Commerce", run_id: UUID, epoch: int, candidate: "Row") -> None:
    """Attach an independent result to every tested attempt, including failures."""
    refs = []
    valid = bool(candidate["verified"])
    for key, kind in (("public_receipt", "public"), ("hidden_receipt", "independent")):
        proof = candidate[key]
        good = bool(
            proof["passed"]
            and proof["exit_code"] == 0
            and not proof["skipped"]
            and proof["stop_reason"] is None
        )
        valid = valid and good
        receipt_hash = store.engine.repairs.artifact(json.dumps(proof, sort_keys=True).encode())
        store.engine.artifacts.read(proof["log_hash"])
        refs.append(
            TestRef(
                kind=kind,
                receipt_hash=receipt_hash,
                status="passed" if good else "failed",
                skipped=proof["skipped"],
            )
        )
    result = base_result(
        store,
        run_id,
        epoch,
        "passed" if valid else "failed",
        None if valid else "PROTECTED_TEST_FAILED",
    )
    result = BusinessResult.model_validate(
        result.model_copy(
            update={
                "artifact_hash": candidate["diff_hash"],
                "test_receipt_refs": refs,
            }
        ).wire()
    )
    content = store.engine.repairs.artifact(json.dumps(result.wire(), sort_keys=True).encode())
    parent = store.current_result(result.case_id, "investigation")
    if not parent or parent["result_hash"] != result.parent_investigation_result_hash:
        raise DomainError("STALE_RESULT", "Repair lost its investigation result binding")
    with store.engine.transaction() as conn:
        persist(store, conn, result, content, parent["fresh_until"])


def commit_investigation(
    store: "Commerce",
    run_id: UUID,
    epoch: int,
    state: dict[str, Any],
    report: bytes,
    resolution: Resolution,
    verdict: Verification,
) -> None:
    e = store.engine
    link = store.binding(run_id)
    assert link is not None
    m = store.manifest(run_id)
    findings = [
        v for key, v in state["findings"].items() if key.startswith(f"r{state['input_revision']}:")
    ]
    errors = [f["error_code"] for f in findings if f.get("error_code")]
    if errors:
        status: Status = (
            "unknown"
            if "USAGE_UNKNOWN" in errors
            else "denied"
            if any(x in {"KNOWLEDGE_REVOKED", "ACTOR_DENIED", "TOOL_SCOPE_DENIED"} for x in errors)
            else "failed"
            if any("MODEL" in x or "QUOTE" in x for x in errors)
            else "needs_input"
        )
        commit_failure(
            store, run_id, epoch, status, "USAGE_UNKNOWN" if status == "unknown" else errors[0]
        )
        return None
    if not verdict.passed or not resolution.complete:
        commit_failure(
            store,
            run_id,
            epoch,
            "failed" if not verdict.passed else "needs_input",
            verdict.reasons[0] if verdict.reasons else "SOURCE_UNAVAILABLE",
        )
        return None
    store.knowledge_scope(m)
    with e.transaction() as conn:
        observed = conn.execute(
            "SELECT safe_payload FROM app.run_events WHERE run_id=%s "
            "AND kind='commerce_rows_observed' AND "
            "safe_payload->>'input_revision'=%s ORDER BY event_id DESC LIMIT "
            "1",
            (run_id, str(state["input_revision"])),
        ).fetchone()
        evidence = conn.execute(
            "SELECT e.*,r.role,r.origin FROM app.evidence e JOIN app.r4_reads r USING(evidence_id) "
            "WHERE e.run_id=%s",
            (run_id,),
        ).fetchall()
    if not observed:
        raise DomainError("NUMERIC_PROVENANCE_REQUIRED", "No actual rows observation")
    read_ids = observed["safe_payload"]
    by_id = {str(row["evidence_id"]): row for row in evidence}
    raw_row = by_id[read_ids["rows_evidence_id"]]
    agg_row = by_id[read_ids["aggregate_evidence_id"]]
    for observation in (raw_row, agg_row):
        if observation["role"] != "data":
            raise DomainError("READ_PROVENANCE", "Numeric inputs must be this Run's data reads")
        e.tools.check_evidence(observation["evidence_id"])
    raw = json.loads(e.artifacts.read(raw_row["content_hash"]))
    aggregate = json.loads(e.artifacts.read(agg_row["content_hash"]))
    rows = observed_rows(raw)
    if digest(rows) != m["data_profile"]["fixture_hash"]:
        raise DomainError("DATA_SOURCE_CHANGED", "Actual complete rows differ from registration")
    metrics = calculate(rows, Window.model_validate(link["spec"]["window"]))
    if aggregate["result_hash"] != digest(
        {k: v for k, v in aggregate.items() if k != "result_hash"}
    ):
        raise DomainError("SQL_HASH", "Aggregate observation changed")
    values = dict(zip([c["name"] for c in aggregate["columns"]], aggregate["rows"][0], strict=True))
    for key in ("paid_events", "distinct_orders", "event_amount"):
        if Decimal(values[key] or 0) != Decimal(getattr(metrics, key)):
            raise DomainError(
                "NUMERIC_EVIDENCE_CONFLICT", "Independent row and SQL metrics disagree"
            )
    quotes = []
    for fact in resolution.verified_facts:
        if not fact.quote:
            continue
        source = by_id.get(str(fact.evidence_id))
        if not source or source["role"] not in {"knowledge", "repo"}:
            raise DomainError("READ_PROVENANCE", "Quote belongs to a different Run or role")
        e.tools.check_evidence(fact.evidence_id)
        content = e.artifacts.read(source["content_hash"]).decode()
        unit = next(
            (
                u
                for u in catalog(content, source["role"]).values()
                if u["text"] == fact.quote == fact.text
            ),
            None,
        )
        if not unit:
            commit_failure(store, run_id, epoch, "failed", "QUOTE_UNIT_MISMATCH")
            return None
        quotes.append(
            QuoteRef(
                source_id=fact.evidence_id,
                content_hash=source["content_hash"],
                unit_kind="knowledge_line" if source["role"] == "knowledge" else "code_lines",
                start_line=unit["start_line"],
                end_line=unit["end_line"],
                unit_hash=unit["unit_hash"],
            )
        )
    receipt = {
        "schema": "commerce-numeric-receipt-v1",
        "run_id": str(run_id),
        "input_revision": state["input_revision"],
        "spec_hash": link["spec_hash"],
        "formula_id": "commerce-revenue-v1",
        "observed_at": raw["observed_at"],
        "rows_evidence_id": read_ids["rows_evidence_id"],
        "rows_result_hash": raw["result_hash"],
        "aggregate_evidence_id": read_ids["aggregate_evidence_id"],
        "aggregate_result_hash": aggregate["result_hash"],
        "rows_hash": digest(rows),
        "operands": [
            {"row": i, "column": j, "value": v, "type_oid": raw["columns"][j]["type_oid"]}
            for i, r in enumerate(raw["rows"])
            for j, v in enumerate(r)
        ],
        "facts": metrics.model_dump(),
    }
    number_hash = e.repairs.artifact(json.dumps(receipt, sort_keys=True).encode())
    report += (
        b"\n\n## Independently checked commerce metrics\n"
        + json.dumps(metrics.model_dump(), indent=2).encode()
    )
    report_hash = e.artifacts.put(report)
    value = base_result(store, run_id, epoch, "passed").model_copy(
        update={
            "artifact_hash": report_hash,
            "facts": metrics.model_dump(),
            "quote_refs": quotes,
            "numeric_receipt_hash": number_hash,
        }
    )
    result = BusinessResult.model_validate(value.wire())
    result_hash = e.repairs.artifact(json.dumps(result.wire(), sort_keys=True).encode())
    fresh = datetime.fromisoformat(raw["observed_at"]) + timedelta(
        seconds=e.settings.r4_sql_ttl_seconds
    )
    if fresh <= datetime.now(UTC):
        raise DomainError("SQL_STALE", "Numeric observations expired before commit")
    with e.transaction() as conn:
        conn.execute(
            "INSERT INTO app.artifacts(content_hash,byte_count) VALUES "
            "(%s,%s) ON CONFLICT DO NOTHING",
            (report_hash, len(report)),
        )
        persist(store, conn, result, result_hash, fresh)
        conn.execute(
            "INSERT INTO app.resolutions(run_id,input_revision,report_hash,payload,verdict) "
            "VALUES (%s,%s,%s,%s,%s) ON CONFLICT(run_id,input_revision) DO NOTHING",
            (
                run_id,
                result.input_revision,
                report_hash,
                Jsonb(resolution.model_dump(mode="json")),
                Jsonb(verdict.model_dump()),
            ),
        )
        e.commit_prepared_report(run_id, epoch, report, verdict, report_hash)
    return None
