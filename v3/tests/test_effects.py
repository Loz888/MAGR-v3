from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest

from mgar_v3.domain import DomainError
from mgar_v3.effects.dispatcher import Dispatcher
from mgar_v3.graph.runner import Runner


class FakeRemote:
    mode = "fixture"

    def __init__(self):
        self.posts = []
        self.timeout_after_accept = False
        self.hide_lookup = False

    def send(self, action):
        self.posts.append(action["payload"])
        if self.timeout_after_accept:
            raise TimeoutError("accepted, response lost")
        return str(len(self.posts))

    def find(self, action):
        if self.hide_lookup:
            return None
        for i, payload in enumerate(self.posts, 1):
            if payload == action["payload"]:
                return str(i)
        return None


@pytest.fixture
def proposed(engine, trigger):
    receipt = engine.ingest(trigger)
    action_id = Runner(engine).execute(receipt.run_id)
    return receipt, engine.actions(receipt.run_id)[0], action_id


def test_exact_approval_and_concurrent_dispatch_publish_once(engine, proposed):
    receipt, action, action_id = proposed
    with pytest.raises(DomainError, match="different payload"):
        engine.approve(action_id, "0" * 64, "owner")
    engine.approve(action_id, action["payload_hash"], "owner")
    engine.approve(action_id, action["payload_hash"], "owner")
    remote = FakeRemote()
    dispatcher = Dispatcher(engine, remote)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: dispatcher.tick(), range(8)))
    assert len(remote.posts) == 1
    assert engine.get_run(receipt.run_id)["status"] == "succeeded"
    with engine.transaction() as conn:
        assert conn.execute("SELECT count(*) AS n FROM app.approvals").fetchone()["n"] == 1
        assert conn.execute("SELECT count(*) AS n FROM app.outbox").fetchone()["n"] == 1


def test_timeout_after_remote_accept_never_reposts_and_reconciles(engine, proposed):
    receipt, action, action_id = proposed
    engine.approve(action_id, action["payload_hash"], "owner")
    remote = FakeRemote()
    remote.timeout_after_accept = True
    dispatcher = Dispatcher(engine, remote)
    assert dispatcher.tick() == action_id
    assert engine.actions(receipt.run_id)[0]["status"] == "outcome_unknown"
    assert dispatcher.tick() is None
    remote.hide_lookup = True
    assert not dispatcher.reconcile(action_id)
    assert dispatcher.tick() is None
    remote.hide_lookup = False
    assert dispatcher.reconcile(action_id)
    assert len(remote.posts) == 1
    assert engine.get_run(receipt.run_id)["status"] == "succeeded"


def test_input_revision_change_invalidates_proposal(engine, trigger, proposed):
    _, action, action_id = proposed
    engine.ingest(trigger.model_copy(update={"external_event_id": "changed", "body": "new facts"}))
    with pytest.raises(DomainError, match="input version changed"):
        engine.approve(action_id, action["payload_hash"], "owner")


def test_input_change_after_approval_blocks_dispatch(engine, trigger, proposed):
    receipt, action, action_id = proposed
    engine.approve(action_id, action["payload_hash"], "owner")
    engine.ingest(trigger.model_copy(update={"external_event_id": "changed", "body": "new facts"}))
    remote = FakeRemote()
    assert Dispatcher(engine, remote).tick() is None
    assert not remote.posts
    assert engine.actions(receipt.run_id)[0]["status"] == "expired"


def test_proposed_payload_is_immutable_in_postgres(engine, proposed):
    _, _, action_id = proposed
    with pytest.raises(psycopg.errors.RaiseException, match="immutable"):
        with engine.transaction() as conn:
            conn.execute("UPDATE app.actions SET payload='{}' WHERE action_id=%s", (action_id,))


def test_cancel_revokes_queued_effect(engine, proposed):
    receipt, action, action_id = proposed
    engine.approve(action_id, action["payload_hash"], "owner")
    engine.cancel(receipt.run_id)
    remote = FakeRemote()
    assert Dispatcher(engine, remote).tick() is None
    assert not remote.posts
    assert engine.actions(receipt.run_id)[0]["status"] == "cancelled"


def test_cancel_does_not_revive_run_when_inflight_receipt_arrives(engine, proposed):
    receipt, action, action_id = proposed
    engine.approve(action_id, action["payload_hash"], "owner")
    claimed = engine.claim_effect("fixture")
    engine.cancel(receipt.run_id)
    engine.effect_result(action_id, claimed["lease_token"], external_id="late-remote-receipt")
    assert engine.get_run(receipt.run_id)["status"] == "cancelled"
    assert engine.actions(receipt.run_id)[0]["status"] == "succeeded"


def test_observer_cannot_publish_or_hold_primary_authority(engine, proposed):
    _, action, action_id = proposed
    with engine.transaction() as conn:
        conn.execute("UPDATE app.subscriptions SET role='observer'")
    with pytest.raises(DomainError, match="primary write grant"):
        engine.approve(action_id, action["payload_hash"], "owner")


def test_old_target_fencing_token_cannot_approve(engine, proposed):
    _, action, action_id = proposed
    with engine.transaction() as conn:
        conn.execute("UPDATE app.target_write_grants SET fencing_token=fencing_token+1")
    with pytest.raises(DomainError, match="write grant"):
        engine.approve(action_id, action["payload_hash"], "owner")


def test_approval_and_outbox_enqueue_roll_back_together(engine, proposed):
    receipt, action, action_id = proposed
    with engine.transaction() as conn:
        conn.execute(
            "CREATE FUNCTION app.fail_outbox() RETURNS trigger LANGUAGE plpgsql AS $$ "
            "BEGIN RAISE EXCEPTION 'injected outbox failure'; END $$"
        )
        conn.execute(
            "CREATE TRIGGER fail_outbox BEFORE INSERT ON app.outbox "
            "FOR EACH ROW EXECUTE FUNCTION app.fail_outbox()"
        )
    try:
        with pytest.raises(psycopg.errors.RaiseException):
            engine.approve(action_id, action["payload_hash"], "owner")
        assert engine.actions(receipt.run_id)[0]["status"] == "proposed"
        with engine.transaction() as conn:
            assert conn.execute("SELECT count(*) AS n FROM app.approvals").fetchone()["n"] == 0
    finally:
        with engine.transaction() as conn:
            conn.execute("DROP TRIGGER fail_outbox ON app.outbox")
            conn.execute("DROP FUNCTION app.fail_outbox()")
