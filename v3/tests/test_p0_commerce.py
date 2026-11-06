import json
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from mgar_v3.commerce.domain import (
    BusinessResult,
    CaseInput,
    CaseMessage,
    Metrics,
    RepairRequest,
    Window,
)
from mgar_v3.commerce.numeric import calculate
from mgar_v3.commerce.setup import setup
from mgar_v3.commerce.view import render
from mgar_v3.domain import DomainError
from mgar_v3.effects.dispatcher import Dispatcher
from mgar_v3.graph.runner import Runner
from mgar_v3.mcp.client import ReadOnlyGitHubMCP
from mgar_v3.rag.domain import ACLUpdate
from mgar_v3.repair.publication import DraftMCPTransport
from mgar_v3.settings import ROOT


@pytest.fixture
def commerce(engine, tmp_path):
    engine.settings.commerce_directory = tmp_path / "commerce"
    setup(engine)
    return engine.commerce


def create(commerce, kind="duplicate_paid", goal="draft_pr", issue=9101):
    policy = json.loads((ROOT / "fixtures/commerce/data.json").read_text())["policy"]
    body = CaseInput(
        template_id=kind,
        goal=goal,
        issue_number=issue,
        window=policy["window"],
        client_request_id=str(uuid4()),
    )
    receipt = commerce.create(body)
    return UUID(receipt["case_id"]), UUID(receipt["run_id"]), body


@pytest.mark.parametrize(
    "kind,expected",
    [
        (
            "duplicate_paid",
            {
                "paid_events": "3",
                "distinct_orders": "2",
                "event_amount": "32.00",
                "dedup_amount": "19.50",
                "inflation_amount": "12.50",
            },
        ),
        (
            "status_filter",
            {
                "paid_events": "1",
                "distinct_orders": "1",
                "event_amount": "10.00",
                "dedup_amount": "10.00",
                "inflation_amount": "0.00",
            },
        ),
        (
            "refunded_status",
            {
                "paid_events": "1",
                "distinct_orders": "1",
                "event_amount": "20.00",
                "dedup_amount": "20.00",
                "inflation_amount": "0.00",
            },
        ),
    ],
)
def test_registered_investigation_and_repair(commerce, kind, expected, tmp_path):
    case_id, run_id, body = create(commerce, kind)
    assert commerce.create(body)["duplicate"] is True
    Runner(commerce.engine).execute(run_id)
    output = commerce.result_view(case_id)
    assert output["result"] and output["result"]["business_status"] == "passed", output
    assert output["result"]["facts"] == expected
    assert output["view"]["rendered"]["can_request_repair"], output["view"]
    request = RepairRequest(
        expected_case_revision=1,
        investigation_result_hash=output["result_hash"],
        client_request_id=str(uuid4()),
    )
    receipt = commerce.start_repair(case_id, request)
    assert commerce.start_repair(case_id, request)["duplicate"]
    repair_id = UUID(receipt["run_id"])
    action_id = Runner(commerce.engine).execute(repair_id)
    output = commerce.result_view(case_id)
    assert action_id is not None, (output, commerce.engine.events(repair_id))
    assert output["result"]["business_status"] == "passed"
    assert (
        "TestCommerceObservedWindow" in output["actions"][0]["payload"]["hidden_receipt"]["tests"]
    )
    assert output["view"]["rendered"]["can_approve"]
    assert output["view"]["delivery"] == "awaiting_approval"
    assert output["actions"][0]["payload"]["business_result_hash"] == output["result_hash"]
    action = output["actions"][0]
    commerce.engine.approve(action_id, action["payload_hash"], "owner")
    remote, path = transport(commerce, repair_id, tmp_path)
    assert Dispatcher(commerce.engine, remote).tick() == action_id
    assert json.loads(path.read_text())["writes"] == ["branch", "commit", "pull_request"]
    final = commerce.result_view(case_id)
    assert final["view"]["rendered"]["status"] == "draft_pr_delivered", final
    assert Dispatcher(commerce.engine, remote).tick() is None


def transport(commerce, run_id, tmp_path, lose_response=None):
    snapshot = commerce.engine.repairs.snapshot(run_id)
    path = tmp_path / "remote.json"
    path.write_text(
        json.dumps(
            {
                "repository": snapshot.repository,
                "branches": {"main": snapshot.base_sha},
                "commits": {
                    snapshot.base_sha: {
                        "sha": snapshot.base_sha,
                        "parents": [],
                        "commit": {"tree": {"sha": snapshot.base_tree}},
                        "files": [],
                        "source_files": snapshot.files,
                        "tree_entries": {
                            p: v.model_dump() for p, v in snapshot.tree_entries.items()
                        },
                    }
                },
                "prs": [],
                "writes": [],
                "lose_response": lose_response,
            }
        )
    )
    client = ReadOnlyGitHubMCP(
        snapshot.repository,
        command=sys.executable,
        args=("-m", "mgar_v3.repair.fixture_server", str(path)),
    )
    return DraftMCPTransport(commerce.engine, client, client), path


def investigated(commerce):
    case_id, run_id, _ = create(commerce)
    Runner(commerce.engine).execute(run_id)
    output = commerce.result_view(case_id)
    return case_id, run_id, output


def proposed(commerce):
    case_id, _, output = investigated(commerce)
    receipt = commerce.start_repair(
        case_id,
        RepairRequest(
            expected_case_revision=1,
            investigation_result_hash=output["result_hash"],
            client_request_id=str(uuid4()),
        ),
    )
    run_id = UUID(receipt["run_id"])
    action_id = Runner(commerce.engine).execute(run_id)
    assert action_id
    return case_id, run_id, commerce.engine.actions(run_id)[0]


@pytest.mark.parametrize("negative", ["missing_sql", "revoked_knowledge"])
def test_sources_missing_or_revoked_make_no_calls_or_actions(commerce, negative):
    case_id, run_id, _ = create(commerce)
    m = commerce.manifest(run_id)
    if negative == "missing_sql":
        from pathlib import Path

        Path(m["reader_path"]).unlink()
    else:
        commerce.engine.knowledge.acl(
            UUID(m["knowledge_document_ids"][0]), ACLUpdate(readers=[], revoked=True)
        )
    Runner(commerce.engine).execute(run_id)
    result = commerce.current_result(case_id, "investigation")
    assert result["payload"]["business_status"] == (
        "needs_input" if negative == "missing_sql" else "denied"
    )
    with commerce.engine.transaction() as conn:
        assert conn.execute("SELECT count(*) AS n FROM app.usage_calls").fetchone()["n"] == 0
        assert conn.execute("SELECT count(*) AS n FROM app.actions").fetchone()["n"] == 0
    with pytest.raises(DomainError):
        commerce.start_repair(
            case_id,
            RepairRequest(
                expected_case_revision=1,
                investigation_result_hash=result["result_hash"],
                client_request_id=str(uuid4()),
            ),
        )


def test_twenty_frozen_views_and_strict_contracts():
    from pydantic import ValidationError

    for example in json.loads(
        (ROOT / "docs/design/p0-commerce-v1/examples/views.json").read_text()
    ):
        view = example["view"]
        assert render(view) == view["rendered"], example["name"]
    for value in [1.0, "NaN", "1.001", "-1.00", "Infinity"]:
        with pytest.raises(ValidationError):
            Metrics(
                paid_events="1",
                distinct_orders="1",
                event_amount=value,
                dedup_amount="1.00",
                inflation_amount="0.00",
            )
    with pytest.raises(ValidationError):
        BusinessResult.model_validate({"status": "supported", "output": {"paid_events": "1"}})


def test_decimal_window_empty_and_conflicting_paid_values():
    window = Window(start=datetime(2026, 11, 5, tzinfo=UTC), end=datetime(2026, 11, 6, tzinfo=UTC))
    rows = [
        {
            "event_id": 1,
            "order_id": "a",
            "status": "paid",
            "amount": "0.10",
            "created_at": window.start.isoformat(),
        },
        {
            "event_id": 2,
            "order_id": "a",
            "status": "paid",
            "amount": "0.10",
            "created_at": window.start.isoformat(),
        },
        {
            "event_id": 3,
            "order_id": "b",
            "status": "paid",
            "amount": "0.20",
            "created_at": window.end.isoformat(),
        },
    ]
    assert calculate(rows, window).model_dump() == {
        "paid_events": "2",
        "distinct_orders": "1",
        "event_amount": "0.20",
        "dedup_amount": "0.10",
        "inflation_amount": "0.10",
    }
    assert calculate([], window).dedup_amount == "0.00"
    rows[1]["amount"] = "0.20"
    with pytest.raises(DomainError, match="Duplicate paid amounts"):
        calculate(rows, window)


def test_idempotence_conflict_and_competing_repair_requests(commerce):
    case_id, run_id, body = create(commerce)
    with pytest.raises(DomainError, match="immutable content"):
        commerce.create(body.model_copy(update={"issue_number": 9999}))
    Runner(commerce.engine).execute(run_id)
    output = commerce.result_view(case_id)

    def request(_):
        try:
            return commerce.start_repair(
                case_id,
                RepairRequest(
                    expected_case_revision=1,
                    investigation_result_hash=output["result_hash"],
                    client_request_id=str(uuid4()),
                ),
            )
        except DomainError as exc:
            return exc.code

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(request, range(2)))
    assert sum(isinstance(r, dict) for r in results) == 1, results
    assert "CASE_STAGE_EXISTS" in results or "BUSINESS_RESULT_REQUIRED" in results


def test_atomic_report_and_business_result_rollback(commerce, monkeypatch):
    case_id, run_id, _ = create(commerce)
    commit = commerce.engine.commit_prepared_report

    def crash(*args, **kwargs):
        commit(*args, **kwargs)
        raise RuntimeError("injected atomic commit failure")

    monkeypatch.setattr(commerce.engine, "commit_prepared_report", crash)
    with pytest.raises(RuntimeError, match="atomic commit failure"):
        Runner(commerce.engine).execute(run_id)
    assert commerce.current_result(case_id, "investigation") is None
    assert commerce.engine.get_run(run_id)["report_hash"] is None
    assert not commerce.engine.actions(run_id)


def test_revocation_after_preparation_redacts_old_pass(commerce):
    case_id, run_id, output = investigated(commerce)
    m = commerce.manifest(run_id)
    commerce.engine.knowledge.acl(
        UUID(m["knowledge_document_ids"][0]), ACLUpdate(readers=[], revoked=True)
    )
    projected = commerce.result_view(case_id)
    assert projected["view"]["rendered"]["status"] == "permission_denied"
    assert projected["result"] is None and not projected["actions"]
    assert (
        commerce.current_result(case_id, "investigation")["payload"]["business_status"] == "passed"
    )
    with pytest.raises(DomainError):
        commerce.artifact_bytes(case_id, output["result"]["artifact_hash"])


def test_unknown_draft_reconciles_readonly_without_resend(commerce, tmp_path):
    case_id, run_id, action = proposed(commerce)
    commerce.engine.approve(action["action_id"], action["payload_hash"], "owner")
    remote, path = transport(commerce, run_id, tmp_path, "pull_request")
    dispatcher = Dispatcher(commerce.engine, remote)
    dispatcher.tick()
    assert commerce.result_view(case_id)["view"]["rendered"]["status"] == "unknown_reconciliation"
    assert dispatcher.tick() is None
    assert dispatcher.reconcile(action["action_id"])
    assert commerce.result_view(case_id)["view"]["rendered"]["status"] == "draft_pr_delivered"
    assert json.loads(path.read_text())["writes"] == ["branch", "commit", "pull_request"]


def test_repair_message_invalidates_old_action_and_starts_new_version(commerce):
    case_id, run_id, action = proposed(commerce)
    receipt = commerce.message(
        case_id,
        CaseMessage(
            body="Recheck the same paid-only policy.",
            expected_case_revision=1,
            client_request_id=str(uuid4()),
        ),
    )
    assert UUID(receipt["run_id"]) != run_id
    assert commerce.case(case_id)["current_revision"] == 2
    assert commerce.engine.actions(run_id)[0]["status"] == "cancelled"
    assert commerce.result_view(case_id)["result"] is None
    with pytest.raises(DomainError):
        commerce.engine.approve(action["action_id"], action["payload_hash"], "owner")


def test_no_artifact_or_sql_io_inside_business_transactions(commerce, monkeypatch):
    import threading
    from contextlib import contextmanager

    from mgar_v3.agents.data import DataReader
    from mgar_v3.evidence.artifacts import ArtifactStore

    local = threading.local()
    violations = []
    transaction = commerce.engine.transaction

    @contextmanager
    def tracked_transaction():
        with transaction() as conn:
            local.depth = getattr(local, "depth", 0) + 1
            try:
                yield conn
            finally:
                local.depth -= 1

    monkeypatch.setattr(commerce.engine, "transaction", tracked_transaction)

    def guard(cls, name):
        original = getattr(cls, name)

        def checked(*args, **kwargs):
            if getattr(local, "depth", 0):
                import traceback

                violations.append(name + "\n" + "".join(traceback.format_stack(limit=15)))
                raise AssertionError(name)
            return original(*args, **kwargs)

        monkeypatch.setattr(cls, name, checked)

    guard(ArtifactStore, "read")
    guard(ArtifactStore, "put")
    guard(DataReader, "query")
    try:
        case_id, run_id, action = proposed(commerce)
    finally:
        assert not violations, "\n".join(violations)
    commerce.engine.approve(action["action_id"], action["payload_hash"], "owner")
    assert commerce.result_view(case_id)["view"]["approval"] == "approved"


def test_http_api_authentication_schema_and_no_store(commerce):
    from fastapi.testclient import TestClient

    from mgar_v3.api import create_app

    e = commerce.engine
    with TestClient(
        create_app(e.settings, e, run_background=False), base_url="http://127.0.0.1"
    ) as client:
        assert client.get("/api/v3/commerce/cases").status_code == 401
        client.headers["X-MGAR-Token"] = e.settings.local_token.get_secret_value()
        templates = client.get("/api/v3/commerce/templates")
        assert templates.status_code == 200 and len(templates.json()) == 3
        assert "database_url" not in templates.text and "reader_path" not in templates.text
        _, _, body = create(commerce, issue=9300)
        value = body.model_copy(
            update={"issue_number": 9301, "client_request_id": str(uuid4())}
        ).model_dump(mode="json")
        assert (
            client.post("/api/v3/commerce/cases", json={**value, "actor": "admin"}).status_code
            == 422
        )
        response = client.post("/api/v3/commerce/cases", json=value)
        assert response.status_code == 202, response.text
        case_id = response.json()["case_id"]
        output = client.get(f"/api/v3/commerce/cases/{case_id}/result")
        assert output.headers["cache-control"] == "no-store"
        assert output.json()["view"]["business"]["status"] == "not_evaluated"
        assert output.json()["result"] is None


@pytest.mark.parametrize("fault", ["missing_prefix", "statement_mismatch"])
def test_historical_quote_failures_remain_failed(commerce, monkeypatch, fault):
    from mgar_v3.commerce.provider import CommerceExperts

    original = CommerceExperts.investigate

    def bad(self, assignment, tools):
        finding = original(self, assignment, tools)
        if assignment.role == "knowledge":
            fact = finding.facts[0]
            text = fact.quote.removeprefix("Observation: ")
            finding = finding.model_copy(
                update={
                    "facts": [
                        fact.model_copy(
                            update={
                                "text": text,
                                "quote": text if fault == "missing_prefix" else fact.quote,
                            }
                        )
                    ]
                }
            )
        return finding

    monkeypatch.setattr(CommerceExperts, "investigate", bad)
    case_id, run_id, _ = create(commerce)
    Runner(commerce.engine).execute(run_id)
    result = commerce.current_result(case_id, "investigation")["payload"]
    assert result["business_status"] == "failed" and not result["facts"]
    assert not commerce.engine.actions(run_id)


def test_unknown_model_result_is_not_evidence_shortage(commerce, monkeypatch):
    from mgar_v3.commerce.provider import CommerceExperts

    original = CommerceExperts.investigate

    def uncertain(self, assignment, tools):
        if assignment.role == "repo":
            raise DomainError("USAGE_UNKNOWN", "Controlled unknown outcome")
        return original(self, assignment, tools)

    monkeypatch.setattr(CommerceExperts, "investigate", uncertain)
    case_id, run_id, _ = create(commerce)
    Runner(commerce.engine).execute(run_id)
    assert (
        commerce.current_result(case_id, "investigation")["payload"]["business_status"] == "unknown"
    )
    assert commerce.result_view(case_id)["view"]["rendered"]["status"] == "unknown_reconciliation"


def test_acl_revoked_between_artifact_preparation_and_business_commit(commerce, monkeypatch):
    case_id, run_id, _ = create(commerce)
    m = commerce.manifest(run_id)
    original = commerce.engine.artifacts.put
    revoked = False

    def revoke_after_prepare(raw):
        nonlocal revoked
        result = original(raw)
        if not revoked and b'"business_status": "passed"' in raw:
            revoked = True
            commerce.engine.knowledge.acl(
                UUID(m["knowledge_document_ids"][0]), ACLUpdate(readers=[], revoked=True)
            )
        return result

    monkeypatch.setattr(commerce.engine.artifacts, "put", revoke_after_prepare)
    Runner(commerce.engine).execute(run_id)
    assert revoked
    result = commerce.current_result(case_id, "investigation")["payload"]
    assert result["business_status"] == "denied"
    assert commerce.engine.get_run(run_id)["report_hash"] is None


def test_old_epoch_and_expired_sql_cannot_be_used_for_repair(commerce, monkeypatch):
    from datetime import UTC, datetime, timedelta

    import mgar_v3.commerce.store as module

    case_id, run_id, output = investigated(commerce)

    class Future(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(UTC) + timedelta(days=1)

    monkeypatch.setattr(module, "datetime", Future)
    with pytest.raises(DomainError, match="current passed"):
        commerce.require_result(run_id)
    monkeypatch.undo()
    with commerce.engine.transaction() as conn:
        conn.execute(
            "UPDATE app.runs SET execution_epoch=execution_epoch+1 WHERE run_id=%s", (run_id,)
        )
    with pytest.raises(DomainError, match="current passed"):
        commerce.require_result(run_id)
    assert commerce.current_result(case_id, "investigation")["result_hash"] == output["result_hash"]


def test_wrong_oid_hash_and_truncated_rows_are_rejected():
    from mgar_v3.commerce.numeric import COLUMNS, observed_rows
    from mgar_v3.domain import digest

    receipt = {
        "columns": [{"name": k, "type_oid": v} for k, v in COLUMNS.items()],
        "rows": [[1, "a", "paid", "0.10", "2026-11-05T00:00:00Z"]],
    }
    receipt["result_hash"] = digest(receipt)
    assert observed_rows(receipt)[0]["amount"] == "0.10"
    receipt["rows"][0][3] = "0.20"
    with pytest.raises(DomainError):
        observed_rows(receipt)
    receipt["columns"][3]["type_oid"] = 25
    receipt["result_hash"] = digest({k: v for k, v in receipt.items() if k != "result_hash"})
    with pytest.raises(DomainError):
        observed_rows(receipt)
    receipt["columns"][3]["type_oid"] = 1700
    receipt["rows"] = [receipt["rows"][0]] * 101
    receipt["result_hash"] = digest({k: v for k, v in receipt.items() if k != "result_hash"})
    with pytest.raises(DomainError, match="exceeds"):
        observed_rows(receipt)


@pytest.mark.parametrize("fault", ["failed", "skipped", "timeout"])
def test_protected_test_failure_cannot_become_approvable(commerce, monkeypatch, fault):
    from mgar_v3.commerce.results import record_candidate

    case_id, _, output = investigated(commerce)
    receipt = commerce.start_repair(
        case_id,
        RepairRequest(
            expected_case_revision=1,
            investigation_result_hash=output["result_hash"],
            client_request_id=str(uuid4()),
        ),
    )
    run_id = UUID(receipt["run_id"])
    run = commerce.engine.begin_run(run_id)
    intake = commerce.engine.prepare_intake(run_id, run["execution_epoch"])
    commerce.engine.acknowledge_intake(run_id, run["execution_epoch"], intake["snapshot_id"])
    log = commerce.engine.repairs.artifact(b"Controlled protected test receipt for rejection")
    proof = {
        "passed": fault != "failed",
        "exit_code": 1 if fault == "failed" else 0,
        "skipped": 1 if fault == "skipped" else 0,
        "stop_reason": "timeout" if fault == "timeout" else None,
        "log_hash": log,
    }
    candidate = {
        "verified": True,
        "diff_hash": commerce.engine.repairs.artifact(b"controlled rejected diff"),
        "public_receipt": proof,
        "hidden_receipt": proof,
    }
    record_candidate(commerce, run_id, run["execution_epoch"], candidate)
    assert (
        commerce.current_result(case_id, "repair_proposal")["payload"]["business_status"]
        == "failed"
    )
    assert not commerce.result_view(case_id)["view"]["rendered"]["can_approve"]


def test_late_delivery_receipt_after_cancel_does_not_restore_case(commerce):
    case_id, run_id, action = proposed(commerce)
    commerce.engine.approve(action["action_id"], action["payload_hash"], "owner")
    claimed = commerce.engine.claim_effect("fixture", ("draft_pr",))
    assert claimed
    commerce.engine.cancel(run_id)
    commerce.engine.effect_result(
        action["action_id"], claimed["lease_token"], external_id="controlled:late-receipt"
    )
    result = commerce.result_view(case_id)
    assert result["view"]["execution"]["status"] == "cancelled"
    assert result["view"]["rendered"]["status"] != "draft_pr_delivered"


def test_current_result_pointer_cannot_rewrite_historical_records(commerce):
    import psycopg

    case_id, _, output = investigated(commerce)
    with pytest.raises(psycopg.errors.RaiseException):
        with commerce.engine.transaction() as conn:
            conn.execute(
                "UPDATE app.business_results SET payload='{}' WHERE case_id=%s", (case_id,)
            )
    assert commerce.current_result(case_id, "investigation")["result_hash"] == output["result_hash"]


def test_sigkill_at_joint_result_commit_recovers_without_duplicate_reads(settings, tmp_path):
    import os
    import subprocess

    from mgar_v3.coordination.engine import Engine
    from mgar_v3.evidence.artifacts import ArtifactStore

    directory = tmp_path / "commerce"
    env = {
        **os.environ,
        "MGAR_V3_DATABASE_URL": settings.database_url.get_secret_value(),
        "MGAR_V3_ARTIFACT_ROOT": str(settings.artifact_root),
        "MGAR_V3_REPOSITORY": settings.repository,
        "MGAR_V3_LOCAL_ACTOR": "owner",
        "MGAR_V3_ALLOWED_ACTORS": '["owner"]',
        "MGAR_V3_ALLOW_PAID": "false",
    }
    worker = subprocess.run(
        [sys.executable, str(ROOT / "tests/fixtures/p0_crash_worker.py"), str(directory)],
        env=env,
        capture_output=True,
        timeout=45,
    )
    assert worker.returncode == -9, worker.stdout.decode() + worker.stderr.decode()
    settings.commerce_directory = directory
    e = Engine(settings, ArtifactStore(settings.artifact_root))
    try:
        run = e.list_runs()[0]
        link = e.commerce.binding(run["run_id"])
        assert e.commerce.current_result(link["case_id"], "investigation") is None
        with e.transaction() as conn:
            reads = conn.execute("SELECT count(*) AS n FROM app.evidence").fetchone()["n"]
        e.recover()
        e.resume(run["run_id"])
        Runner(e).execute(run["run_id"])
        assert (
            e.commerce.current_result(link["case_id"], "investigation")["payload"][
                "business_status"
            ]
            == "passed"
        )
        with e.transaction() as conn:
            assert conn.execute("SELECT count(*) AS n FROM app.evidence").fetchone()["n"] == reads
    finally:
        e.close()


def test_exhausted_invalid_patch_responses_have_explicit_failed_business_result(
    commerce, monkeypatch
):
    from mgar_v3.commerce.provider import CommercePatcher

    case_id, _, output = investigated(commerce)
    receipt = commerce.start_repair(
        case_id,
        RepairRequest(
            expected_case_revision=1,
            investigation_result_hash=output["result_hash"],
            client_request_id=str(uuid4()),
        ),
    )

    def invalid(*args, **kwargs):
        raise DomainError("MODEL_INVALID_RESPONSE", "Controlled invalid patch schema")

    monkeypatch.setattr(CommercePatcher, "propose", invalid)
    run_id = UUID(receipt["run_id"])
    Runner(commerce.engine).execute(run_id)
    result = commerce.current_result(case_id, "repair_proposal")
    assert result is not None and result["payload"]["business_status"] == "failed"
    assert not commerce.engine.actions(run_id)
