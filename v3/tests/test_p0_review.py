"""Real PG and Docker tests for explicit unchanged-proposal evidence renewal."""

import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

import psycopg
import pytest
from psycopg.pq import TransactionStatus
from test_p0_commerce import commerce as commerce
from test_p0_commerce import proposed, transport

from mgar_v3.agents.data import DataReader
from mgar_v3.commerce.review import certificate, recheck
from mgar_v3.domain import DomainError, digest
from mgar_v3.effects.dispatcher import Dispatcher
from mgar_v3.rag.domain import ACLUpdate
from mgar_v3.repair.sandbox import Sandbox


def aged_proposal(commerce, monkeypatch):
    import mgar_v3.commerce.presentation as presentation
    import mgar_v3.commerce.store as store

    commerce.engine.settings.r4_sql_ttl_seconds = 3600
    original = DataReader.query

    def aged(self, *args, **kwargs):
        value = original(self, *args, **kwargs)
        value["observed_at"] = (datetime.now(UTC) - timedelta(seconds=3500)).isoformat()
        value["result_hash"] = digest({k: v for k, v in value.items() if k != "result_hash"})
        return value

    with monkeypatch.context() as setup:
        setup.setattr(DataReader, "query", aged)
        case_id, run_id, action = proposed(commerce)

    class Future(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(UTC) + timedelta(seconds=150)

    monkeypatch.setattr(store, "datetime", Future)
    monkeypatch.setattr(presentation, "datetime", Future)
    return case_id, run_id, action


def test_exact_review_preserves_history_and_delivers_original_payload(
    commerce, monkeypatch, tmp_path
):
    case_id, run_id, action = aged_proposal(commerce, monkeypatch)
    e = commerce.engine
    original_result = commerce.current_result(case_id, "repair_proposal")
    with pytest.raises(DomainError, match="current passed"):
        e.approve(action["action_id"], action["payload_hash"], "owner")

    # File, SQL reader and Go work must never run inside the business transaction.
    def guarded(method):
        def call(*args, **kwargs):
            assert e.connection.info.transaction_status == TransactionStatus.IDLE
            return method(*args, **kwargs)

        return call

    for obj, method in (
        (e.artifacts, "read"),
        (e.artifacts, "put"),
        (DataReader, "query"),
        (Sandbox, "run"),
    ):
        monkeypatch.setattr(obj, method, guarded(getattr(obj, method)))
    check = recheck(commerce, action["action_id"], action["payload_hash"], "owner")
    assert check["status"] == "passed"
    proof = json.loads(e.artifacts.read(check["certificate_hash"]))
    assert proof["model_calls"] == 0 and len(proof["test_receipts"]) == 2
    assert all(r["passed"] and not r["skipped"] for r in proof["test_receipts"])
    assert commerce.current_result(case_id, "repair_proposal") == original_result
    assert commerce.require_result(run_id, action_id=action["action_id"]) == original_result
    import mgar_v3.commerce.review as review_module

    class FarFuture(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(UTC) + timedelta(days=1)

    with monkeypatch.context() as expired:
        expired.setattr(review_module, "datetime", FarFuture)
        with pytest.raises(DomainError):
            e.approve(action["action_id"], action["payload_hash"], "owner")
    # A certificate for an Action does not renew the unqualified historical result.
    with pytest.raises(DomainError):
        commerce.require_result(run_id)
    with e.transaction() as conn:
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            with conn.transaction():
                conn.execute("UPDATE app.commerce_review_checks SET status='failed'")
    e.repairs.renew_review(action["action_id"], action["payload_hash"], "owner")
    assert e.actions(run_id)[0]["payload"] == action["payload"]
    e.approve(action["action_id"], action["payload_hash"], "owner")
    remote, path = transport(commerce, run_id, tmp_path)
    assert Dispatcher(e, remote).tick() == action["action_id"]
    assert json.loads(path.read_text())["writes"] == ["branch", "commit", "pull_request"]
    assert commerce.result_view(case_id)["view"]["rendered"]["status"] == "draft_pr_delivered"
    assert e.actions(run_id)[0]["payload_hash"] == action["payload_hash"]
    with e.transaction() as conn:
        assert conn.execute("SELECT count(*) AS n FROM app.usage_calls").fetchone()["n"] == 0
    with pytest.raises(DomainError):
        recheck(commerce, action["action_id"], action["payload_hash"], "owner")


def test_failed_fresh_tests_are_recorded_and_never_renew(commerce, monkeypatch):
    case_id, run_id, action = aged_proposal(commerce, monkeypatch)
    assert (
        recheck(commerce, action["action_id"], action["payload_hash"], "owner")["status"]
        == "passed"
    )
    original = Sandbox.run

    def fail(self, *args, **kwargs):
        receipt, raw = original(self, *args, **kwargs)
        receipt.update(passed=False, skipped=1)
        return receipt, raw

    monkeypatch.setattr(Sandbox, "run", fail)
    with pytest.raises(DomainError, match="failed fresh tests"):
        recheck(commerce, action["action_id"], action["payload_hash"], "owner")
    result = commerce.current_result(case_id, "repair_proposal")
    assert certificate(commerce, action["action_id"], result) is None
    with commerce.engine.transaction() as conn:
        row = conn.execute(
            "SELECT * FROM app.commerce_review_checks ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        assert row["status"] == "failed"
        assert not conn.execute("SELECT 1 FROM app.approvals").fetchone()
        assert not conn.execute("SELECT 1 FROM app.outbox").fetchone()
    proof = json.loads(commerce.engine.artifacts.read(row["certificate_hash"]))
    assert proof["reason_code"] == "PROTECTED_TEST_FAILED"
    assert proof["test_receipts"][0]["log_hash"]
    with pytest.raises(DomainError):
        commerce.require_result(run_id, action_id=action["action_id"])


def test_changed_observed_data_cannot_reuse_approval(commerce, monkeypatch):
    _, run_id, action = proposed(commerce)
    original = DataReader.query

    def changed(self, *args, **kwargs):
        value = original(self, *args, **kwargs)
        value["rows"][0][1] += "-changed"
        value["result_hash"] = digest({k: v for k, v in value.items() if k != "result_hash"})
        return value

    monkeypatch.setattr(DataReader, "query", changed)
    with pytest.raises(DomainError, match="New observations differ"):
        recheck(commerce, action["action_id"], action["payload_hash"], "owner")
    # A new failed recheck also blocks an otherwise unexpired original business result.
    with pytest.raises(DomainError):
        commerce.require_result(run_id, action_id=action["action_id"])


@pytest.mark.parametrize("fence", ["actor", "hash", "epoch", "input", "acl", "during_test"])
def test_recheck_fences_and_rollback(commerce, monkeypatch, fence):
    case_id, run_id, action = proposed(commerce)
    e = commerce.engine
    actor, payload_hash = "owner", action["payload_hash"]
    if fence == "actor":
        actor = "intruder"
    elif fence == "hash":
        payload_hash = "0" * 64
    elif fence in {"epoch", "input"}:
        with e.transaction() as conn:
            if fence == "epoch":
                conn.execute(
                    "UPDATE app.runs SET execution_epoch=execution_epoch+1 WHERE run_id=%s",
                    (run_id,),
                )
            else:
                conn.execute(
                    "UPDATE app.threads SET revision=revision+1 WHERE thread_id=%s",
                    (e.get_run(run_id)["thread_id"],),
                )
    elif fence == "acl":
        m = commerce.manifest(run_id)
        e.knowledge.acl(UUID(m["knowledge_document_ids"][0]), ACLUpdate(readers=[], revoked=True))
    else:
        original = Sandbox.run

        def advance(self, *args, **kwargs):
            result = original(self, *args, **kwargs)
            with e.transaction() as conn:
                conn.execute(
                    "UPDATE app.runs SET execution_epoch=execution_epoch+1 WHERE run_id=%s",
                    (run_id,),
                )
            return result

        monkeypatch.setattr(Sandbox, "run", advance)
    with pytest.raises(DomainError):
        recheck(commerce, action["action_id"], payload_hash, actor)
    with e.transaction() as conn:
        assert not conn.execute("SELECT 1 FROM app.commerce_review_checks").fetchone()
        assert not conn.execute("SELECT 1 FROM app.approvals").fetchone()
        assert not conn.execute("SELECT 1 FROM app.outbox").fetchone()
    assert (
        commerce.current_result(case_id, "repair_proposal")["payload"]["business_status"]
        == "passed"
    )
