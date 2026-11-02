from uuid import uuid4

import psycopg
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg.rows import dict_row

from mgar_v3.domain import Claim, Investigation
from mgar_v3.evidence.verifier import verify
from mgar_v3.graph.runner import Runner


def test_fixture_langgraph_ends_at_exact_payload_approval(engine, trigger, database):
    receipt = engine.ingest(trigger)
    action_id = Runner(engine).execute(receipt.run_id)
    run = engine.get_run(receipt.run_id)
    assert run["status"] == "waiting_approval"
    assert engine.artifacts.read(run["report_hash"]).startswith(b"# MGAR investigation")
    actions = engine.actions(receipt.run_id)
    assert len(actions) == 1 and actions[0]["action_id"] == action_id
    assert actions[0]["status"] == "proposed"
    assert "Execution mode: fixture" in actions[0]["payload"]["body"]
    with psycopg.connect(
        database, autocommit=True, row_factory=dict_row, options="-c search_path=checkpoints,public"
    ) as conn:
        checkpoint = PostgresSaver(conn).get_tuple(
            {"configurable": {"thread_id": str(receipt.run_id)}}
        )
        assert checkpoint is not None
        assert checkpoint.checkpoint["channel_values"]["verification"]["passed"]
    with engine.transaction() as conn:
        row = conn.execute("SELECT * FROM app.mailbox").fetchone()
        assert row["delivered_at"] and row["read_at"] and row["consumed_at"]
        assert conn.execute("SELECT count(*) AS n FROM app.outbox").fetchone()["n"] == 0


def test_forged_or_cross_run_evidence_does_not_verify(engine, trigger):
    first = engine.ingest(trigger)
    Runner(engine).execute(first.run_id)
    evidence_id = engine.events(first.run_id)[2]["safe_payload"]["evidence_id"]
    from uuid import UUID

    row = engine.get_evidence(first.run_id, UUID(evidence_id))
    assert row is not None
    second = engine.ingest(
        trigger.model_copy(update={"external_event_id": "other", "issue_number": 2})
    )
    result = Investigation(
        claims=[Claim(statement="forged", quote="forged", evidence_id=row["evidence_id"])]
    )
    assert not verify(engine, second.run_id, result).passed
    assert not verify(engine, first.run_id, result).passed
    unknown = Investigation(claims=[Claim(statement="x", quote="x", evidence_id=uuid4())])
    assert not verify(engine, first.run_id, unknown).passed


def test_modified_source_artifact_is_rejected_by_independent_verifier(engine, trigger):
    receipt = engine.ingest(trigger)
    Runner(engine).execute(receipt.run_id)
    with engine.transaction() as conn:
        evidence = conn.execute("SELECT * FROM app.evidence").fetchone()
    engine.artifacts.path(evidence["content_hash"]).write_bytes(b"tampered")
    result = Investigation(
        claims=[Claim(statement="tampered", quote="tampered", evidence_id=evidence["evidence_id"])]
    )
    assert not verify(engine, receipt.run_id, result).passed
