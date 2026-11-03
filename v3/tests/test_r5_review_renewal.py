import json

import psycopg
import pytest
from test_r5_repair import pending, transport

from mgar_v3.domain import DomainError
from mgar_v3.effects.dispatcher import Dispatcher


def test_expired_unsent_review_needs_fresh_human_decision_and_preserves_exact_payload(
    engine, tmp_path, monkeypatch
):
    # Inject an already expired proposal at creation; never weaken its immutable DB fields.
    monkeypatch.setattr(engine.settings, "approval_ttl_seconds", -1)
    run_id, original = pending(engine)
    monkeypatch.setattr(engine.settings, "approval_ttl_seconds", 3600)
    action_id, exact = original["action_id"], original["payload_hash"]
    with pytest.raises(DomainError, match="expired"):
        engine.approve(action_id, exact, "owner")
    with pytest.raises(DomainError, match="Actor"):
        engine.repairs.renew_review(action_id, exact, "outsider")
    with pytest.raises(DomainError, match="exact"):
        engine.repairs.renew_review(action_id, "0" * 64, "owner")
    with engine.transaction() as conn:
        conn.execute("UPDATE app.target_write_grants SET active=false")
    with pytest.raises(DomainError, match="write grant"):
        engine.repairs.renew_review(action_id, exact, "owner")
    with engine.transaction() as conn:
        conn.execute("UPDATE app.target_write_grants SET active=true")
    renewed = engine.repairs.renew_review(action_id, exact, "owner")
    assert engine.repairs.renew_review(action_id, exact, "owner") == renewed
    current = engine.actions(run_id)[0]
    assert current["expires_at"] == original["expires_at"]
    assert current["payload"] == original["payload"] and current["payload_hash"] == exact
    assert current["review_expires_at"] > current["expires_at"]
    with engine.transaction() as conn:
        assert not conn.execute("SELECT 1 FROM app.outbox").fetchone()
    with pytest.raises(psycopg.errors.RaiseException, match="immutable"):
        with engine.transaction() as conn:
            conn.execute("UPDATE app.action_review_renewals SET actor='forged'")
    remote, path = transport(engine, tmp_path)
    engine.approve(action_id, exact, "owner")
    with pytest.raises(DomainError, match="entirely unsent"):
        engine.repairs.renew_review(action_id, exact, "owner")
    dispatcher = Dispatcher(engine, remote)
    dispatcher.tick()
    assert engine.actions(run_id)[0]["status"] == "succeeded"
    assert dispatcher.tick() is None
    assert json.loads(path.read_text())["writes"] == ["branch", "commit", "pull_request"]
