import difflib
import hashlib
import json
import sys
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from mgar_v3.domain import DomainError
from mgar_v3.effects.dispatcher import Dispatcher
from mgar_v3.graph.runner import Runner
from mgar_v3.live import Authorization, LivePermit, LiveProfile
from mgar_v3.mcp.client import ReadOnlyGitHubMCP
from mgar_v3.repair.domain import GitEntry, RepairInput, RepairScope, Snapshot
from mgar_v3.repair.graph import execute_repair
from mgar_v3.repair.live import DeepSeekPatcher, load_snapshot
from mgar_v3.repair.publication import LiveDraftMCPTransport
from mgar_v3.repair.source import candidate_tree, git_tree, verify_snapshot, working_files
from mgar_v3.repair.workspace import apply_diff, blob, fixture_snapshot


def live_case(
    engine,
    tmp_path,
    call_limit=3,
    spent=0,
    limit=2000000,
    write=True,
    case_label="controlled-native-adapter",
):
    fixture = fixture_snapshot(engine.settings.repository)
    prefix = "src/orders"
    files = {prefix + "/" + p: v for p, v in fixture.files.items()}
    entries = {p: GitEntry(mode="100644", sha=blob(v.encode())) for p, v in files.items()}
    entries["README.md"] = GitEntry(mode="100755", sha=blob(b"unchanged outside repair scope\n"))
    snapshot = Snapshot(
        repository=engine.settings.repository,
        base_sha="a" * 40,
        base_tree=git_tree(entries),
        origin="github_commit",
        files=files,
        editable_paths=[prefix + "/orders.go"],
        expected_tests=fixture.expected_tests,
        workspace_prefix=prefix,
        tree_entries=entries,
    )
    path = tmp_path / "source.json"
    raw = snapshot.model_dump_json().encode()
    path.write_bytes(raw)
    engine.settings.repair_snapshot_path = path
    engine.settings.allow_paid = True
    engine.settings.allow_external_write = True
    engine.settings.deepseek_api_key = SecretStr("fixture-key")
    root = "b" * 64
    with engine.transaction() as conn:
        conn.execute(
            "INSERT INTO app.authorization_budgets(profile_hash,limit_micros,"
            "settled_micros,call_limit) "
            "VALUES (%s,2000000,%s,3) ON CONFLICT DO NOTHING",
            (root, spent),
        )
    profile = LiveProfile(
        investigation_version="r5-repair-v1",
        case_id=case_label,
        project_id=engine.settings.project_id,
        repository=engine.settings.repository,
        issue_number=6001,
        actor="owner",
        question="Count first paid occurrence per ID, including zero/negative integer amounts.",
        source_kind="file",
        source_path=prefix + "/orders.go",
        source_ref=snapshot.base_sha,
        expected_source_hash=hashlib.sha256(files[prefix + "/orders.go"].encode()).hexdigest(),
        model_id="deepseek-flash",
        price_version="controlled-price-bound",
        input_micros_per_million=300000,
        output_micros_per_million=1200000,
        context_tokens=8192,
        max_output_tokens=2048,
        max_cost_micros=20000,
        max_model_calls=call_limit,
        action_type="draft_pr",
        repair=RepairScope(
            snapshot_sha256=hashlib.sha256(raw).hexdigest(),
            base_tree=snapshot.base_tree,
            workspace_prefix=prefix,
            predecessor_budget_hashes=[root],
            cumulative_limit_micros=limit,
        ),
    )
    auth = Authorization(
        profile_hash=profile.profile_hash,
        approved_by="owner",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        allow_paid=True,
        allow_external_write=write,
    )
    engine.settings.repair_profile_path = tmp_path / "profile.json"
    engine.settings.repair_authorization_path = tmp_path / "authorization.json"
    engine.settings.repair_profile_path.write_text(profile.model_dump_json())
    engine.settings.repair_authorization_path.write_text(auth.model_dump_json())
    permit = LivePermit(profile, auth, engine.settings)
    receipt = engine.repairs.create(
        RepairInput(
            project_id=profile.project_id,
            issue_number=profile.issue_number,
            question=profile.question,
            client_request_id=str(uuid4()),
            execution_mode="live",
        ),
        permit,
    )
    return UUID(receipt["run_id"]), permit, snapshot


def patch(snapshot, variant=0):
    # Two development variants, neither uses the production fixture patch provider.
    path = snapshot.editable_paths[0]
    before = snapshot.files[path]
    body = (
        """func PaidRevenueCents(orders []Order) int {
    total := 0
    seen := make(map[string]int)
    for _, order := range orders {
        if order.Status != "paid" { continue }
        _, exists := seen[order.ID]
        if !exists { total += order.Amount; seen[order.ID] = 1 }
    }
    return total
}
"""
        if variant == 0
        else """func PaidRevenueCents(orders []Order) int {
    total := 0
    for i, order := range orders {
        if order.Status != "paid" { continue }
        duplicate := false
        for _, previous := range orders[:i] {
            if previous.ID == order.ID && previous.Status == "paid" { duplicate = true }
        }
        if !duplicate { total += order.Amount }
    }
    return total
}
"""
    )
    after = before[: before.index("func PaidRevenueCents")] + body
    return "".join(
        difflib.unified_diff(
            before.splitlines(True),
            after.splitlines(True),
            fromfile="a/" + path,
            tofile="b/" + path,
        )
    )


def model_client(snapshot, variant=0, invalid_first=False, timeout=False, cancel=None):
    calls = []

    def respond(request):
        body = json.loads(request.content)
        calls.append(body)
        content = json.dumps(body)
        assert "zz_mgar_hidden" not in content and "TestR5Hidden" not in content
        assert "tree_entries" not in content and "fixture-key" not in content
        if cancel:
            cancel()
        if timeout:
            raise httpx.ReadTimeout("controlled timeout")
        answer = (
            "invalid json"
            if invalid_first and len(calls) == 1
            else json.dumps(
                {"diff": patch(snapshot, variant), "explanation": "Controlled HTTP completion"}
            )
        )
        return httpx.Response(
            200,
            json={
                "model": "deepseek-flash",
                "usage": {"prompt_tokens": 100, "completion_tokens": 200},
                "choices": [{"finish_reason": "stop", "message": {"content": answer}}],
            },
        )

    return httpx.Client(transport=httpx.MockTransport(respond)), calls


def remote(engine, permit, snapshot, tmp_path, monkeypatch):
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
            }
        )
    )
    client = ReadOnlyGitHubMCP(
        snapshot.repository,
        command=sys.executable,
        args=("-m", "mgar_v3.repair.fixture_server", str(path)),
    )

    def metadata(repository, sha):
        return json.loads(path.read_text())["commits"][sha], {}

    monkeypatch.setattr("mgar_v3.repair.source.github_commit", metadata)
    return LiveDraftMCPTransport(engine, permit, client, client), path


@pytest.mark.parametrize("variant", [0, 1])
def test_live_adapters_with_controlled_http_real_go_and_stdio(
    engine, tmp_path, monkeypatch, variant
):
    run_id, permit, snapshot = live_case(engine, tmp_path)
    client, calls = model_client(snapshot, variant)
    # The real model adapter is exercised through a controlled HTTP provider, not billed DeepSeek.
    action_id = execute_repair(
        Runner(engine), run_id, DeepSeekPatcher(engine, run_id, 1, permit, client)
    )
    # begin_run assigns epoch 1 to this newly created Run.
    assert action_id is not None and len(calls) == 1
    action = engine.actions(run_id)[0]
    assert action["payload"]["files"][0]["path"] == "src/orders/orders.go"
    assert action["payload"]["tree_sha"] == candidate_tree(
        snapshot, apply_diff(snapshot, patch(snapshot, variant))
    )
    transport, path = remote(engine, permit, snapshot, tmp_path, monkeypatch)
    engine.approve(action_id, action["payload_hash"], "owner")
    assert engine.claim_effect("live", ("draft_pr",), "wrong-profile") is None
    Dispatcher(engine, transport).tick()
    assert engine.actions(run_id)[0]["status"] == "succeeded"
    assert json.loads(path.read_text())["writes"] == ["branch", "commit", "pull_request"]
    assert Dispatcher(engine, transport).tick() is None
    with engine.transaction() as conn:
        assert conn.execute(
            "SELECT used_calls,reserved_micros FROM app.authorization_budgets "
            "WHERE profile_hash=%s",
            (permit.profile.profile_hash,),
        ).fetchone() == {"used_calls": 1, "reserved_micros": 0}


def test_invalid_json_consumes_one_attempt_then_repairs_under_same_budget(engine, tmp_path):
    run_id, permit, snapshot = live_case(engine, tmp_path)
    client, calls = model_client(snapshot, invalid_first=True)
    result = execute_repair(
        Runner(engine), run_id, DeepSeekPatcher(engine, run_id, 1, permit, client)
    )
    assert result and len(calls) == 2
    with engine.transaction() as conn:
        rows = conn.execute(
            "SELECT status,result FROM app.usage_calls ORDER BY step_key"
        ).fetchall()
    assert (
        rows[0]["result"] is None
        and rows[1]["result"]["diff"]
        and all(r["status"] == "known" for r in rows)
    )


def test_unknown_usage_does_not_repeat_model_or_publish(engine, tmp_path):
    run_id, permit, snapshot = live_case(engine, tmp_path)
    client, calls = model_client(snapshot, timeout=True)
    provider = DeepSeekPatcher(engine, run_id, 1, permit, client)
    with pytest.raises(DomainError, match="unknown"):
        execute_repair(Runner(engine), run_id, provider)
    with pytest.raises(DomainError, match="resent"):
        provider.propose(snapshot, permit.profile.question, 0, "same input")
    assert len(calls) == 1 and not engine.actions(run_id)
    engine.recover()
    with pytest.raises(DomainError):
        engine.resume(run_id)


def test_cumulative_predecessor_cost_blocks_before_http(engine, tmp_path):
    run_id, permit, snapshot = live_case(engine, tmp_path, spent=9999, limit=10000)
    client, calls = model_client(snapshot)
    with pytest.raises(DomainError, match="cumulative"):
        execute_repair(Runner(engine), run_id, DeepSeekPatcher(engine, run_id, 1, permit, client))
    assert not calls and not engine.actions(run_id)
    with engine.transaction() as conn:
        assert not conn.execute("SELECT 1 FROM app.usage_calls").fetchone()


def test_model_completion_after_cancel_is_fenced_and_usage_still_settles(engine, tmp_path):
    run_id, permit, snapshot = live_case(engine, tmp_path)
    client, calls = model_client(snapshot, cancel=lambda: engine.cancel(run_id))
    with pytest.raises(DomainError):
        execute_repair(Runner(engine), run_id, DeepSeekPatcher(engine, run_id, 1, permit, client))
    assert (
        len(calls) == 1
        and engine.get_run(run_id)["status"] == "cancelled"
        and not engine.actions(run_id)
    )
    with engine.transaction() as conn:
        assert conn.execute("SELECT status FROM app.usage_calls").fetchone()["status"] == "known"


def test_live_source_and_authorization_scope_tampering_denied(engine, tmp_path):
    _, permit, snapshot = live_case(engine, tmp_path)
    assert load_snapshot(permit) == snapshot
    permit.settings.repair_snapshot_path.write_text(snapshot.model_dump_json() + " ")
    with pytest.raises(DomainError, match="artifact"):
        load_snapshot(permit)
    with pytest.raises(ValidationError):
        LiveProfile.model_validate(
            {**permit.profile.model_dump(), "investigation_version": "r1-quotes-v1"}
        )
    with pytest.raises(ValidationError):
        LiveProfile.model_validate({**permit.profile.model_dump(), "action_type": "issue_comment"})


def test_complete_git_tree_protects_outside_files_and_editable_symlinks(engine, tmp_path):
    _, _, snapshot = live_case(engine, tmp_path)
    files = apply_diff(snapshot, patch(snapshot))
    assert candidate_tree(snapshot, files) != snapshot.base_tree
    assert snapshot.tree_entries["README.md"].mode == "100755"
    tampered = snapshot.model_copy(deep=True)
    tampered.tree_entries["src/orders/orders.go"] = GitEntry(mode="120000", sha="d" * 40)
    with pytest.raises(DomainError):
        verify_snapshot(tampered)
    assert set(working_files(snapshot, files)) == {"go.mod", "orders.go", "orders_test.go"}
    with pytest.raises(DomainError):
        working_files(snapshot, {**files, "outside.go": "package main"})


def test_shared_budget_cannot_be_reset_by_a_new_profile(engine, tmp_path):
    run_id, permit, _ = live_case(engine, tmp_path, spent=1, limit=5000)
    run = engine.begin_run(run_id)
    call = engine.reserve_call(run_id, run["execution_epoch"], "first", permit)
    engine.settle_call(call, 100)
    engine.cancel(run_id)
    next_run, next_permit, _ = live_case(
        engine, tmp_path, spent=1, limit=5000, case_label="second-profile"
    )
    current = engine.begin_run(next_run)
    with pytest.raises(DomainError, match="cumulative"):
        engine.reserve_call(next_run, current["execution_epoch"], "second", next_permit)
    assert permit.profile.profile_hash != next_permit.profile.profile_hash
    with engine.transaction() as conn:
        assert (
            conn.execute("SELECT count(*) AS n FROM app.repair_budget_groups").fetchone()["n"] == 1
        )
        assert conn.execute("SELECT count(*) AS n FROM app.usage_calls").fetchone()["n"] == 1


def test_new_profile_cannot_forget_predecessors_or_change_group_limit(engine, tmp_path):
    run_id, _, _ = live_case(engine, tmp_path, limit=5000)
    engine.cancel(run_id)
    with pytest.raises(DomainError, match="reset"):
        live_case(engine, tmp_path, limit=6000, case_label="attempted-reset")


def test_explicit_live_user_edit_retests_without_a_new_model_call(engine, tmp_path):
    run_id, permit, snapshot = live_case(engine, tmp_path)
    client, calls = model_client(snapshot)
    action = execute_repair(
        Runner(engine), run_id, DeepSeekPatcher(engine, run_id, 1, permit, client)
    )
    old = engine.actions(run_id)[0]
    engine.repairs.revise(action, old["payload_hash"], patch(snapshot, 1), "owner")
    result = Runner(engine).execute(run_id)
    new = engine.actions(run_id)[-1]
    assert result and new["status"] == "proposed" and len(calls) == 1
    assert old["payload_hash"] != new["payload_hash"] and new["payload"]["manual_input_hash"]
    assert new["payload"]["public_receipt"]["passed"] and new["payload"]["hidden_receipt"]["passed"]


@pytest.mark.parametrize("lost", ["branch", "commit"])
def test_partial_native_publication_only_continues_unissued_steps_after_explicit_authorization(
    engine, tmp_path, monkeypatch, lost
):
    run_id, permit, snapshot = live_case(engine, tmp_path)
    client, calls = model_client(snapshot)
    action_id = execute_repair(
        Runner(engine), run_id, DeepSeekPatcher(engine, run_id, 1, permit, client)
    )
    action = engine.actions(run_id)[0]
    outgoing, path = remote(engine, permit, snapshot, tmp_path, monkeypatch)
    state = json.loads(path.read_text())
    state["lose_response"] = lost
    state["minimal_commit"] = True
    path.write_text(json.dumps(state))
    engine.approve(action_id, action["payload_hash"], "owner")
    dispatcher = Dispatcher(engine, outgoing)
    dispatcher.tick()
    assert engine.actions(run_id)[0]["status"] == "outcome_unknown"
    with pytest.raises(DomainError, match="positively reconciled"):
        engine.repairs.continue_publication(action_id, action["payload_hash"], "owner")
    writes = json.loads(path.read_text())["writes"]
    assert dispatcher.tick() is None and not dispatcher.reconcile(action_id)
    assert json.loads(path.read_text())["writes"] == writes
    assert dispatcher.tick() is None
    engine.repairs.continue_publication(action_id, action["payload_hash"], "owner")
    dispatcher.tick()
    assert engine.actions(run_id)[0]["status"] == "succeeded"
    assert json.loads(path.read_text())["writes"] == ["branch", "commit", "pull_request"]
    assert len(calls) == 1


def test_malformed_hunk_receives_public_feedback_and_uses_a_distinct_bounded_attempt(
    engine, tmp_path
):
    import re

    run_id, permit, snapshot = live_case(engine, tmp_path)
    calls = []

    def response(request):
        body = json.loads(request.content)
        calls.append(body)
        text = patch(snapshot)
        if len(calls) == 1:
            text = re.sub(r"(@@ -\d+)(?:,\d+)?( \+\d+)(?:,\d+)? @@", r"\1,999\2,999 @@", text)
        else:
            assert "actual removed/context" in body["messages"][1]["content"]
            assert "TestR5Hidden" not in body["messages"][1]["content"]
        return httpx.Response(
            200,
            json={
                "model": "deepseek-flash",
                "usage": {"prompt_tokens": 100, "completion_tokens": 200},
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": json.dumps({"diff": text, "explanation": "controlled patch"})
                        },
                    }
                ],
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(response))
    assert execute_repair(
        Runner(engine), run_id, DeepSeekPatcher(engine, run_id, 1, permit, client)
    )
    assert len(calls) == 2 and len(engine.actions(run_id)) == 1
