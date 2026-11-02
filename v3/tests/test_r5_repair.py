import difflib
import json
import sys
from uuid import UUID, uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient

from mgar_v3.api import create_app
from mgar_v3.domain import DomainError, digest
from mgar_v3.effects.dispatcher import Dispatcher
from mgar_v3.graph.runner import Runner
from mgar_v3.mcp.client import ReadOnlyGitHubMCP
from mgar_v3.repair.domain import PatchReply, RepairInput
from mgar_v3.repair.graph import execute_repair
from mgar_v3.repair.hidden import suite
from mgar_v3.repair.publication import DraftMCPTransport
from mgar_v3.repair.sandbox import Sandbox, parse_tests
from mgar_v3.repair.workspace import apply_diff, fixture_diff, fixture_snapshot


def create(engine, issue=5001):
    value = engine.repairs.create(
        RepairInput(
            project_id=engine.settings.project_id,
            issue_number=issue,
            question="Count the first paid order per ID. Ignore other statuses.",
            client_request_id=str(uuid4()),
        )
    )
    return UUID(value["run_id"])


def pending(engine):
    run_id = create(engine)
    action_id = Runner(engine).execute(run_id)
    assert action_id is not None, engine.get_run(run_id)
    return run_id, engine.actions(run_id)[0]


def transport(engine, tmp_path, lose_response=None):
    snapshot = engine.repairs.snapshot(engine.list_runs()[0]["run_id"])
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
    return DraftMCPTransport(engine, client, client), path


def diff(snapshot, after):
    return "".join(
        difflib.unified_diff(
            snapshot.files["orders.go"].splitlines(True),
            after.splitlines(True),
            fromfile="a/orders.go",
            tofile="b/orders.go",
        )
    )


def test_real_baseline_red_candidate_green_and_independent_hidden():
    snapshot = fixture_snapshot("example/mgar-test")
    box = Sandbox("r5-policy-tests")
    red, raw = box.run(snapshot.files, snapshot.expected_tests)
    assert red["exit_code"] == 1 and red["failed_tests"] == snapshot.expected_tests, raw.decode()
    files = apply_diff(snapshot, fixture_diff(snapshot, 0))
    green, raw = box.run(
        {**files, "zz_mgar_hidden_test.go": suite()}, [*snapshot.expected_tests, "TestR5Hidden"]
    )
    assert green["passed"], raw.decode()
    assert len(green["tests"]) >= 25
    assert box.cleanup() == 0


@pytest.mark.parametrize(
    "path",
    [
        "../orders.go",
        "/orders.go",
        ".git/config",
        "x/../orders.go",
        "orders_test.go",
        "go.mod",
        "x\\orders.go",
    ],
)
def test_patch_scope_rejects_traversal_tests_and_build_files(path):
    snapshot = fixture_snapshot("example/mgar-test")
    with pytest.raises(DomainError):
        apply_diff(snapshot, fixture_diff(snapshot, 0).replace("orders.go", path))


@pytest.mark.parametrize(
    "replacement",
    [
        'panic("forged pass")',
        "go func(){}()",
        "defer len(orders)",
        "func() int { return 0 }()",
        "orders[0].Amount = 1",
    ],
)
def test_ast_rejects_impure_or_outside_function_changes(replacement):
    snapshot = fixture_snapshot("example/mgar-test")
    source = snapshot.files["orders.go"]
    after = source.replace("total := 0", replacement + "\n total := 0")
    if replacement == "orders[0].Amount = 1":
        after = source.replace("ID string", "ID int")
    with pytest.raises(DomainError, match="pure function"):
        apply_diff(snapshot, diff(snapshot, after))


def test_forged_empty_skipped_or_incomplete_test_streams_never_pass():
    for events in [
        [],
        [{"Action": "pass", "Package": "example.invalid/mgar-v3/orders"}],
        [
            {"Action": "run", "Test": "TestPaidRevenueCents"},
            {"Action": "skip", "Test": "TestPaidRevenueCents"},
        ],
    ]:
        assert not parse_tests(
            "\n".join(json.dumps(e) for e in events).encode(), 0, ["TestPaidRevenueCents"]
        )["passed"]


def test_sandbox_network_credentials_root_and_host_mount_denied(monkeypatch):
    monkeypatch.setenv("MGAR_V3_DEEPSEEK_API_KEY", "not-a-real-secret")
    snapshot = fixture_snapshot("example/mgar-test")
    probe = """package orders
import ("os"; "net"; "testing")
func TestPolicy(t *testing.T) {
 if os.Getuid()==0 {t.Fatal("root")}
 if os.Getenv("MGAR_V3_DEEPSEEK_API_KEY")!="" {t.Fatal("credential")}
 if _,e:=os.Stat("/var/run/docker.sock");e==nil{t.Fatal("docker socket")}
 if _,e:=os.Stat("/workspace");e==nil{t.Fatal("host mount")}
 if e:=os.WriteFile("/escape",[]byte("x"),0600);e==nil{t.Fatal("writable root")}
 c,e:=net.Dial("tcp","1.1.1.1:443");if e==nil{c.Close();t.Fatal("network")}
}
"""
    proof, raw = Sandbox("policy-probe").run(
        {**snapshot.files, "policy_test.go": probe}, [*snapshot.expected_tests, "TestPolicy"]
    )
    # Baseline intentionally fails; the independent policy probe must have passed.
    assert '"Action":"pass"' in raw.decode() and '"Test":"TestPolicy"' in raw.decode()
    assert "TestPolicy" in proof["tests"] and "TestPolicy" not in proof["failed_tests"]


def test_timeout_and_output_overflow_kill_and_remove_owned_container():
    snapshot = fixture_snapshot("example/mgar-test")
    box = Sandbox("timeout-probe")
    slow = snapshot.files["orders.go"].replace("total := 0", "for {}\n total := 0")
    proof, _ = box.run({**snapshot.files, "orders.go": slow}, snapshot.expected_tests, timeout=1)
    assert not proof["passed"] and proof["stop_reason"] == "timeout"
    assert box.cleanup() == 0


def test_graph_approval_edit_retests_and_reject_blocks_old_payload(engine):
    run_id, action = pending(engine)
    assert engine.get_run(run_id)["status"] == "waiting_approval"
    assert (
        action["payload"]["public_receipt"]["passed"]
        and action["payload"]["hidden_receipt"]["passed"]
    )
    assert len(engine.repairs.detail(run_id)["candidates"]) == 1
    engine.repairs.revise(
        action["action_id"],
        action["payload_hash"],
        action["payload"]["diff"],
        engine.settings.local_actor,
    )
    assert engine.get_run(run_id)["status"] == "queued"
    with pytest.raises(DomainError):
        engine.approve(action["action_id"], action["payload_hash"], "owner")
    Runner(engine).execute(run_id)
    old, new = engine.actions(run_id)
    assert (
        old["status"] == "cancelled"
        and new["status"] == "proposed"
        and old["payload_hash"] != new["payload_hash"]
    )
    assert len(engine.repairs.detail(run_id)["candidates"]) == 2
    engine.repairs.decision(
        new["action_id"], new["payload_hash"], "owner", "reject", "Needs review"
    )
    assert engine.get_run(run_id)["status"] == "waiting_input"
    assert engine.claim_effect("fixture", ("draft_pr",)) is None


def test_public_pass_hidden_failure_cannot_propose_and_three_attempt_bound(engine):
    class Overfit:
        mode = "fixture"
        calls = 0

        def propose(self, snapshot, question, attempt, feedback):
            self.calls += 1
            assert "TestR5Hidden" not in json.dumps(snapshot.model_dump()) + feedback
            after = snapshot.files["orders.go"].replace("return total", "return 50")
            return PatchReply(
                diff=diff(snapshot, after), explanation="Deliberately overfit public test"
            )

    provider = Overfit()
    run_id = create(engine)
    assert execute_repair(Runner(engine), run_id, provider) is None
    assert provider.calls == 3 and not engine.actions(run_id)
    rows = engine.repairs.detail(run_id)["candidates"]
    assert len(rows) == 3 and all(
        r["public_receipt"]["passed"] and not r["hidden_receipt"]["passed"] for r in rows
    )


def test_engine_transactions_rollback_and_old_epoch_results_fenced(engine):
    run_id = create(engine)
    run = engine.begin_run(run_id)
    revision = run["thread_revision"]
    epoch = run["execution_epoch"]
    engine.repairs.step(run_id, epoch, revision, "late")
    engine.cancel(run_id)
    with pytest.raises(DomainError, match="authorized"):
        engine.repairs.finish(run_id, epoch, revision, "late", {"passed": True})
    with engine.transaction() as conn:
        assert conn.execute("SELECT status FROM app.repair_steps").fetchone()["status"] == "running"


def test_draft_mcp_three_steps_and_exact_reconciliation(engine, tmp_path):
    run_id, action = pending(engine)
    remote, path = transport(engine, tmp_path)
    engine.approve(action["action_id"], action["payload_hash"], "owner")
    assert engine.claim_effect("fixture") is None
    Dispatcher(engine, remote).tick()
    assert engine.actions(run_id)[0]["status"] == "succeeded"
    assert json.loads(path.read_text())["writes"] == ["branch", "commit", "pull_request"]
    assert remote.find(engine.actions(run_id)[0]) == "1"
    assert json.loads(path.read_text())["writes"] == ["branch", "commit", "pull_request"]
    with engine.transaction() as conn:
        assert (
            len(
                conn.execute(
                    "SELECT * FROM app.repair_publication_steps WHERE status='succeeded'"
                ).fetchall()
            )
            == 3
        )
        assert not conn.execute("SELECT 1 FROM app.usage_calls").fetchone()


@pytest.mark.parametrize("lost", ["branch", "commit", "pull_request"])
def test_lost_mcp_response_is_unknown_never_retried(engine, tmp_path, lost):
    run_id, action = pending(engine)
    remote, path = transport(engine, tmp_path, lost)
    engine.approve(action["action_id"], action["payload_hash"], "owner")
    dispatcher = Dispatcher(engine, remote)
    dispatcher.tick()
    assert engine.actions(run_id)[0]["status"] == "outcome_unknown"
    writes = json.loads(path.read_text())["writes"]
    engine.recover()
    assert dispatcher.tick() is None
    assert dispatcher.reconcile(action["action_id"]) == (lost == "pull_request")
    assert json.loads(path.read_text())["writes"] == writes


def test_changed_base_fails_before_any_write(engine, tmp_path):
    run_id, action = pending(engine)
    remote, path = transport(engine, tmp_path)
    state = json.loads(path.read_text())
    state["commits"]["b" * 40] = {
        **state["commits"][action["payload"]["base_sha"]],
        "sha": "b" * 40,
    }
    state["branches"]["main"] = "b" * 40
    path.write_text(json.dumps(state))
    engine.approve(action["action_id"], action["payload_hash"], "owner")
    Dispatcher(engine, remote).tick()
    assert (
        engine.actions(run_id)[0]["status"] == "failed"
        and not json.loads(path.read_text())["writes"]
    )


def test_payload_files_tamper_and_cancellation_between_steps_fenced(engine, tmp_path):
    run_id, action = pending(engine)
    remote, path = transport(engine, tmp_path)
    original = remote.call

    def cancel_after_branch(tool, args):
        value = original(tool, args)
        if tool == "create_branch":
            engine.cancel(run_id)
        return value

    remote.call = cancel_after_branch
    engine.approve(action["action_id"], action["payload_hash"], "owner")
    with pytest.raises(DomainError):
        Dispatcher(engine, remote).tick()
    assert json.loads(path.read_text())["writes"] == ["branch"]
    assert engine.actions(run_id)[0]["status"] == "outcome_unknown"


def test_repair_api_auth_live_denial_diff_and_scoped_artifacts(engine):
    with TestClient(
        create_app(engine.settings, engine, run_background=False), base_url="http://127.0.0.1"
    ) as client:
        request = {
            "project_id": engine.settings.project_id,
            "issue_number": 5001,
            "question": "Fix paid revenue",
            "client_request_id": "r5-api",
        }
        assert client.post("/api/v3/repairs", json=request).status_code == 401
        client.headers["X-MGAR-Token"] = engine.settings.local_token.get_secret_value()
        assert (
            client.post("/api/v3/repairs", json={**request, "execution_mode": "live"}).status_code
            == 403
        )
        result = client.post("/api/v3/repairs", json=request)
        assert result.status_code == 202, result.text
        run_id = UUID(result.json()["run_id"])
        Runner(engine).execute(run_id)
        action = client.get(f"/api/v3/runs/{run_id}/actions").json()[0]
        assert action["payload"]["diff"].startswith("--- a/orders.go")
        proof = action["payload"]["public_receipt"]
        base = f"/api/v3/runs/{run_id}/repair/artifacts/"
        assert client.get(base + proof["log_hash"]).status_code == 200
        assert client.get(base + "0" * 64).status_code == 404
        approved = client.post(
            f"/api/v3/runs/{run_id}/approvals",
            json={"action_id": action["action_id"], "payload_hash": action["payload_hash"]},
        )
        assert approved.status_code == 200, approved.text
        assert approved.json()["status"] == "ready"


def test_output_limit_stops_the_container_and_cleans_it():
    snapshot = fixture_snapshot("example/mgar-test")
    box = Sandbox("output-probe")
    test = """package orders
import ("testing";"fmt";"strings")
func TestOutput(t *testing.T) {fmt.Print(strings.Repeat("x",100000))}
"""
    proof, raw = box.run({**snapshot.files, "output_test.go": test}, ["TestOutput"])
    assert proof["stop_reason"] == "output_limit" and not proof["passed"] and len(raw) == 65536
    assert box.cleanup() == 0


def test_test_receipts_and_payload_tampering_block_approval(engine):
    run_id, action = pending(engine)
    payload = {**action["payload"], "files": [{"path": "orders.go", "content": "package orders\n"}]}
    value = digest(
        {
            "type": "draft_pr",
            "target": action["target"],
            "payload": payload,
            "base_revision": action["base_revision"],
        }
    )
    from psycopg.types.json import Jsonb

    with pytest.raises(psycopg.errors.RaiseException, match="immutable"):
        with engine.transaction() as conn:
            conn.execute(
                "UPDATE app.actions SET payload=%s,payload_hash=%s WHERE action_id=%s",
                (Jsonb(payload), value, action["action_id"]),
            )
    with pytest.raises(DomainError, match="tested tree"):
        engine.repairs.validate_action(
            {**action, "payload": payload, "payload_hash": value}, engine.get_run(run_id)
        )
    assert engine.claim_effect("fixture", ("draft_pr",)) is None
    engine.approve(action["action_id"], action["payload_hash"], "owner")
    claim = engine.claim_effect("fixture", ("draft_pr",))
    assert claim is not None
    with pytest.raises(DomainError, match="approved action"):
        engine.repairs.publication_step(
            action["action_id"], claim["lease_token"], 0, "branch", {"branch": "main"}
        )
    with engine.transaction() as conn:
        assert not conn.execute("SELECT 1 FROM app.repair_publication_steps").fetchone()


def test_observer_lacks_write_grant_and_rejection_rollback(engine):
    run_id, action = pending(engine)
    with engine.transaction() as conn:
        conn.execute("UPDATE app.subscriptions SET role='observer' WHERE run_id=%s", (run_id,))
    with pytest.raises(DomainError):
        engine.approve(action["action_id"], action["payload_hash"], "owner")
    with engine.transaction() as conn:
        assert not conn.execute("SELECT 1 FROM app.approvals").fetchone()
        assert not conn.execute("SELECT 1 FROM app.outbox").fetchone()
        conn.execute("UPDATE app.subscriptions SET role='primary' WHERE run_id=%s", (run_id,))
    with pytest.raises(RuntimeError):
        with engine.transaction():
            engine.repairs.decision(
                action["action_id"], action["payload_hash"], "owner", "reject", "test"
            )
            raise RuntimeError("rollback")
    assert engine.actions(run_id)[0]["status"] == "proposed"
