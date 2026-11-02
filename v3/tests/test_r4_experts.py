from concurrent.futures import ThreadPoolExecutor
from uuid import UUID

import psycopg
import pytest
from psycopg import sql
from test_models import live_case  # noqa: F401
from test_r4_knowledge import upload

from mgar_v3.agents.data import provision, validate_sql
from mgar_v3.agents.domain import Fact, Finding
from mgar_v3.agents.verify import cell_text, verify_findings
from mgar_v3.domain import DomainError
from mgar_v3.graph.runner import Runner


def prepare(engine, trigger):
    engine.settings.graph_version = "r4-experts-v1"
    engine.settings.data_profile_path = engine.settings.artifact_root.parent / "data.json"
    provision(engine, engine.settings.data_profile_path)
    upload(engine)
    receipt = engine.ingest(trigger)
    assert receipt.run_id
    return receipt


def test_three_experts_join_and_report_typed_sql(engine, trigger):
    receipt = prepare(engine, trigger)
    action = Runner(engine).execute(receipt.run_id)
    assert action and engine.get_run(receipt.run_id)["status"] == "waiting_approval"
    resolution = engine.tools.resolution(receipt.run_id)
    assert resolution["verdict"]["passed"] and resolution["payload"]["complete"]
    assert set(resolution["payload"]["completed_roles"]) == {"knowledge", "repo", "data"}
    facts = resolution["payload"]["verified_facts"]
    values = [f["cell"]["value"] for f in facts if f["cell"]]
    assert values == [3, 2, "32.00"]
    events = engine.events(receipt.run_id)
    join = next(e["event_id"] for e in events if e["kind"] == "expert_joined")
    assert len([e for e in events if e["kind"] == "expert_completed" and e["event_id"] < join]) == 3
    assert all("Bearer" not in str(e) for e in events)
    with engine.transaction() as conn:
        assert conn.execute("SELECT count(*) AS n FROM app.usage_calls").fetchone()["n"] == 0


def test_missing_knowledge_stops_after_two_supplement_rounds(engine, trigger):
    receipt = prepare(engine, trigger)
    with engine.transaction() as conn:
        conn.execute("DELETE FROM knowledge.active_snapshots")
    assert Runner(engine).execute(receipt.run_id) is None
    run = engine.get_run(receipt.run_id)
    assert run["status"] == "waiting_input"
    events = [e for e in engine.events(receipt.run_id) if e["kind"] == "expert_joined"]
    assert [e["safe_payload"]["round_no"] for e in events] == [0, 1, 2]
    assert not engine.actions(receipt.run_id)


def test_readonly_role_database_denial_and_closed_ast(engine, trigger):
    prepare(engine, trigger)
    reader = engine.tools.reader("owner")
    schema = reader.profile["schema"]
    assert reader.query(f"SELECT count(*) AS n FROM {schema}.orders", [])["rows"] == [[4]]
    bad = [
        f"DELETE FROM {schema}.orders",
        f"SELECT pg_sleep(1) FROM {schema}.orders",
        f"SELECT * FROM {schema}.orders; SELECT 1",
        "SELECT * FROM app.runs",
        f"SELECT * FROM {schema}.orders a JOIN {schema}.orders b ON 1=1",
        f"WITH x AS (SELECT * FROM {schema}.orders) SELECT * FROM x",
    ]
    for statement in bad:
        with pytest.raises(DomainError):
            validate_sql(statement, [], schema)
    with psycopg.connect(reader.dsn.get_secret_value(), autocommit=True) as conn:
        for statement in [
            f"DELETE FROM {schema}.orders",
            "SELECT * FROM app.runs",
            f"CREATE TABLE {schema}.x(id int)",
        ]:
            with pytest.raises(psycopg.Error):
                conn.execute(statement)


def test_role_tokens_fencing_cancellation_and_late_result(engine, trigger):
    receipt = prepare(engine, trigger)
    run = engine.begin_run(receipt.run_id)
    epoch = run["execution_epoch"]
    token = engine.tools.issue(receipt.run_id, epoch, "data-task", "data", run["input_revision"])
    with pytest.raises(DomainError) as exc:
        engine.tools.call(token, "read_repository", {})
    assert exc.value.code == "TOOL_ROLE_DENIED"
    engine.cancel(receipt.run_id)
    with pytest.raises(DomainError):
        engine.tools.call(token, "describe_tables", {})
    grant = {
        "run_id": receipt.run_id,
        "execution_epoch": epoch,
        "project_id": run["project_id"],
        "role": "data",
        "task_id": "data-task",
    }
    with pytest.raises(DomainError):
        engine.tools.record(grant, b"{}", "sql://x", "v1", {"kind": "data"})
    with engine.transaction() as conn:
        assert conn.execute("SELECT count(*) AS n FROM app.evidence").fetchone()["n"] == 0


def test_verifier_typed_value_and_actual_task_origin(engine, trigger):
    receipt = prepare(engine, trigger)
    run = engine.begin_run(receipt.run_id)
    token = engine.tools.issue(
        receipt.run_id, run["execution_epoch"], "t", "data", run["input_revision"]
    )
    schema = engine.tools.call(token, "describe_tables", {})["schema"]
    evidence = engine.tools.call(
        token,
        "query_readonly",
        {"sql": f"SELECT count(*) AS n FROM {schema}.orders", "parameters": []},
    )
    fact = Fact(
        evidence_id=UUID(evidence["evidence_id"]),
        text=cell_text("n", 4, 20),
        cell={"row": 0, "column": 0, "value": 4, "type_oid": 20},
    )
    finding = Finding(task_id="t", role="data", status="succeeded", facts=[fact])
    assert verify_findings(engine, receipt.run_id, [finding], ["data"])[1].passed
    for change in ({"value": True}, {"type_oid": 23}, {"value": "4"}):
        broken = fact.model_copy(update={"cell": fact.cell | change})
        verdict = verify_findings(
            engine, receipt.run_id, [finding.model_copy(update={"facts": [broken]})], ["data"]
        )[1]
        assert not verdict.passed and "SQL_CELL" in verdict.reasons
    wrong = finding.model_copy(update={"task_id": "forged"})
    assert not verify_findings(engine, receipt.run_id, [wrong], ["data"])[1].passed


def test_parallel_tools_share_global_budget(engine, trigger):
    receipt = prepare(engine, trigger)
    run = engine.begin_run(receipt.run_id)
    tokens = [
        engine.tools.issue(
            receipt.run_id, run["execution_epoch"], str(i), "data", run["input_revision"]
        )
        for i in range(3)
    ]
    with engine.transaction() as conn:
        conn.execute(
            "UPDATE app.r4_context SET tool_calls_used=35 WHERE run_id=%s", (receipt.run_id,)
        )

    def call(token):
        try:
            engine.tools.call(token, "describe_tables", {})
            return True
        except DomainError:
            return False

    with ThreadPoolExecutor(max_workers=3) as pool:
        assert sum(pool.map(call, tokens)) == 1


def test_live_experts_require_a_distinct_reviewed_r4_profile(request):
    from mgar_v3.agents.experts import DeepSeekExperts

    engine, permit, _ = request.getfixturevalue("live_case")
    with pytest.raises(DomainError) as exc:
        DeepSeekExperts(engine, permit)
    assert exc.value.code == "R4_LIVE_PROFILE_REQUIRED"


def test_input_arriving_during_expert_reads_requires_a_new_plan(engine, trigger, monkeypatch):
    from mgar_v3.agents.experts import FixtureExperts

    receipt = prepare(engine, trigger)
    original = FixtureExperts.investigate
    sent = False

    def investigate(self, assignment, tools):
        nonlocal sent
        if assignment.role == "repo" and not sent:
            sent = True
            engine.follow_up(receipt.run_id, "follow-up", "Also inspect the distinct order count.")
        return original(self, assignment, tools)

    monkeypatch.setattr(FixtureExperts, "investigate", investigate)
    Runner(engine).execute(receipt.run_id)
    run = engine.get_run(receipt.run_id)
    resolution = engine.tools.resolution(receipt.run_id)
    assert resolution["input_revision"] == run["thread_revision"] == run["input_revision"] == 2
    assert len(engine.actions(receipt.run_id)) == 1
    assert all(
        f["task_id"].startswith("r2:")
        for f in engine.connection.execute(
            (
                "SELECT r.task_id FROM app.r4_reads r JOIN app.evidence e "
                "USING(evidence_id) WHERE e.evidence_id=ANY(%s)"
            ),
            ([f["evidence_id"] for f in resolution["payload"]["verified_facts"]],),
        ).fetchall()
    )


def test_verifier_conflicting_knowledge_versions_and_stale_sql(engine, trigger):
    from mgar_v3.agents.domain import Assignment
    from mgar_v3.agents.experts import FixtureExperts
    from mgar_v3.mcp.r4 import TaskTools

    receipt = prepare(engine, trigger)
    old = engine.knowledge.active()
    new = upload(engine, "# 口径新版本\n业务口径更新，当前统计只包括唯一订单。", version="v2")
    engine.tools.requested_snapshots(receipt.run_id, [old, new])
    run = engine.begin_run(receipt.run_id)
    assignment = Assignment(
        task_id="r1:0:knowledge",
        role="knowledge",
        question="订单统计",
        round_no=0,
        input_revision=1,
        snapshot_ids=[old, new],
    )
    token = engine.tools.issue(
        receipt.run_id, run["execution_epoch"], assignment.task_id, "knowledge", 1
    )
    finding = FixtureExperts().investigate(assignment, TaskTools(engine, token, "knowledge"))
    result, verdict = verify_findings(engine, receipt.run_id, [finding], ["knowledge"])
    assert verdict.passed and result.contradictions and not result.complete


def test_changed_dataset_is_rejected_in_readonly_snapshot(engine, trigger):
    prepare(engine, trigger)
    reader = engine.tools.reader("owner")
    with engine.transaction() as conn:
        conn.execute(
            sql.SQL("UPDATE {}.orders SET amount=99 WHERE event_id=1").format(
                sql.Identifier(reader.profile["schema"])
            )
        )
    with pytest.raises(DomainError) as exc:
        reader.query(f"SELECT count(*) AS n FROM {reader.profile['schema']}.orders", [])
    assert exc.value.code == "DATA_SOURCE_CHANGED"


def test_all_experts_actually_run_in_parallel(engine, trigger, monkeypatch):
    import threading

    from mgar_v3.agents.experts import FixtureExperts

    receipt = prepare(engine, trigger)
    barrier = threading.Barrier(3)
    original = FixtureExperts.investigate

    def investigate(self, assignment, tools):
        barrier.wait(timeout=3)
        return original(self, assignment, tools)

    monkeypatch.setattr(FixtureExperts, "investigate", investigate)
    assert Runner(engine).execute(receipt.run_id)


def test_sql_lock_timeout_is_enforced_by_postgresql(engine, trigger):
    prepare(engine, trigger)
    reader = engine.tools.reader("owner")
    with psycopg.connect(engine.settings.database_url.get_secret_value()) as blocker:
        blocker.execute(
            sql.SQL("LOCK TABLE {}.orders IN ACCESS EXCLUSIVE MODE").format(
                sql.Identifier(reader.profile["schema"])
            )
        )
        with pytest.raises(DomainError) as exc:
            reader.query(f"SELECT count(*) AS n FROM {reader.profile['schema']}.orders", [])
        assert exc.value.code == "SQL_TIMEOUT"


def test_cancel_during_read_prevents_late_expert_commit(engine, trigger, monkeypatch):
    import threading

    receipt = prepare(engine, trigger)
    entered = threading.Event()
    release = threading.Event()
    original = engine.tools.record

    def record(grant, *args, **kwargs):
        if grant["role"] == "data":
            entered.set()
            assert release.wait(timeout=5)
        return original(grant, *args, **kwargs)

    monkeypatch.setattr(engine.tools, "record", record)
    with ThreadPoolExecutor(max_workers=1) as pool:
        work = pool.submit(Runner(engine).execute, receipt.run_id)
        assert entered.wait(timeout=5)
        engine.cancel(receipt.run_id)
        release.set()
        with pytest.raises(DomainError):
            work.result(timeout=5)
    assert engine.get_run(receipt.run_id)["status"] == "cancelled"
    assert not engine.actions(receipt.run_id)
    with engine.transaction() as conn:
        assert not conn.execute("SELECT 1 FROM app.r4_reads WHERE role='data'").fetchone()


def test_one_expert_failure_still_joins_other_selected_results(engine, trigger, monkeypatch):
    from mgar_v3.agents.experts import FixtureExperts

    receipt = prepare(engine, trigger)
    original = FixtureExperts.investigate

    def investigate(self, assignment, tools):
        if assignment.role == "repo":
            raise RuntimeError("controlled expert failure")
        return original(self, assignment, tools)

    monkeypatch.setattr(FixtureExperts, "investigate", investigate)
    assert Runner(engine).execute(receipt.run_id) is None
    assert engine.get_run(receipt.run_id)["status"] == "waiting_input"
    resolution = engine.tools.resolution(receipt.run_id)
    assert set(resolution["payload"]["completed_roles"]) == {"knowledge", "data"}
    with engine.transaction() as conn:
        assert conn.execute("SELECT count(*) AS n FROM app.expert_results").fetchone()["n"] == 5
    assert not engine.actions(receipt.run_id)
