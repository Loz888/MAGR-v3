from uuid import UUID

import pytest
from test_api import client_for
from test_effects import FakeRemote
from test_r4_experts import prepare

from mgar_v3.domain import DomainError
from mgar_v3.effects.dispatcher import Dispatcher
from mgar_v3.graph.runner import Runner
from mgar_v3.rag.domain import ACLUpdate


def finish(engine, receipt):
    action_id = Runner(engine).execute(receipt.run_id)
    action = engine.actions(receipt.run_id)[0]
    engine.approve(action_id, action["payload_hash"], "owner")
    Dispatcher(engine, FakeRemote()).tick()
    assert engine.get_run(receipt.run_id)["status"] == "succeeded"


def test_only_confirmed_ended_ticket_memories_and_revocation(engine, trigger):
    receipt = prepare(engine, trigger)
    with pytest.raises(DomainError) as exc:
        engine.context.preview_memory(receipt.ticket_id)
    assert exc.value.code == "MEMORY_TICKET_OPEN"
    finish(engine, receipt)
    assert engine.context.memories() == []
    preview = engine.context.preview_memory(receipt.ticket_id)
    with pytest.raises(DomainError):
        engine.context.confirm(receipt.ticket_id, "0" * 64, preview["report_hash"])
    memory = engine.context.confirm(
        receipt.ticket_id, preview["summary_hash"], preview["report_hash"]
    )
    assert (
        engine.context.confirm(receipt.ticket_id, preview["summary_hash"], preview["report_hash"])[
            "memory_id"
        ]
        == memory["memory_id"]
    )
    assert engine.context.memories()[0]["source_ticket_id"] == str(receipt.ticket_id)
    recalled = engine.context.recall("ticket", receipt.ticket_id)
    assert any(item["kind"] == "memory" for item in recalled["items"])
    engine.context.revoke(memory["memory_id"])
    assert not engine.context.memories()


def test_spill_pagination_cursor_scope_and_hash(engine, trigger):
    receipt = prepare(engine, trigger)
    Runner(engine).execute(receipt.run_id)
    fact = next(
        f
        for f in engine.tools.resolution(receipt.run_id)["payload"]["verified_facts"]
        if f["quote"] and "PAID_EVENTS_SQL" in f["quote"]
    )
    eid = UUID(fact["evidence_id"])
    page = engine.context.page("evidence", eid, max_bytes=128)
    assert page["end_offset"] <= 128 and page["continuation"]
    second = engine.context.page("evidence", eid, cursor=page["continuation"], max_bytes=128)
    assert second["offset"] == page["end_offset"]
    with pytest.raises(DomainError):
        engine.context.page("evidence", eid, cursor=page["continuation"] + "x")
    facts = engine.tools.resolution(receipt.run_id)["payload"]["verified_facts"]
    other = UUID(next(f["evidence_id"] for f in facts if f["evidence_id"] != str(eid)))
    with pytest.raises(DomainError):
        engine.context.page("evidence", other, cursor=page["continuation"])
    with pytest.raises(DomainError):
        engine.context.recall("run", receipt.run_id, max_hops=4)


def test_revoked_source_blocks_evidence_report_action_and_publication(engine, trigger, settings):
    receipt = prepare(engine, trigger)
    Runner(engine).execute(receipt.run_id)
    action = engine.actions(receipt.run_id)[0]
    resolution = engine.tools.resolution(receipt.run_id)
    knowledge = next(
        f for f in resolution["payload"]["verified_facts"] if f["quote"] and "去重" in f["quote"]
    )
    with engine.transaction() as conn:
        origin = conn.execute(
            "SELECT origin FROM app.r4_reads WHERE evidence_id=%s", (knowledge["evidence_id"],)
        ).fetchone()["origin"]
    engine.knowledge.acl(UUID(origin["document_id"]), ACLUpdate(revoked=True))
    with pytest.raises(DomainError):
        engine.approve(action["action_id"], action["payload_hash"], "owner")
    with client_for(settings, engine) as client:
        assert client.get(f"/api/v3/runs/{receipt.run_id}/report").status_code >= 400
        assert client.get(f"/api/v3/runs/{receipt.run_id}/actions").status_code >= 400
        assert client.get("/api/v3/evidence/" + knowledge["evidence_id"]).status_code >= 400
        assert client.get("/api/v3/artifacts/" + resolution["report_hash"]).status_code >= 400


def test_context_other_actor_and_run_evidence_contamination_rejected(engine, trigger):
    receipt = prepare(engine, trigger)
    Runner(engine).execute(receipt.run_id)
    engine.settings.allowed_actors = ["owner", "outsider"]
    with pytest.raises(DomainError):
        engine.context.resolve("ticket", receipt.ticket_id, "outsider")
    from mgar_v3.agents.domain import Fact, Finding
    from mgar_v3.agents.verify import verify_findings

    fact = next(
        f
        for f in engine.tools.resolution(receipt.run_id)["payload"]["verified_facts"]
        if f["quote"] and "PAID_EVENTS_SQL" in f["quote"]
    )
    other = engine.ingest(
        trigger.model_copy(update={"issue_number": 2, "external_event_id": "other"})
    )
    finding = Finding(
        task_id="r1:0:knowledge",
        role="knowledge",
        status="succeeded",
        facts=[Fact.model_validate(fact)],
    )
    assert not verify_findings(engine, other.run_id, [finding], ["knowledge"])[1].passed


def test_memory_source_revocation_removes_recall(engine, trigger):
    receipt = prepare(engine, trigger)
    finish(engine, receipt)
    preview = engine.context.preview_memory(receipt.ticket_id)
    engine.context.confirm(receipt.ticket_id, preview["summary_hash"], preview["report_hash"])
    doc = engine.knowledge.documents()[0]["document_id"]
    engine.knowledge.acl(doc, ACLUpdate(revoked=True))
    assert engine.context.memories() == []
