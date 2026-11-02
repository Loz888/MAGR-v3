from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr

from mgar_v3.coordination.engine import COMMAND, Engine
from mgar_v3.domain import DomainError, digest
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.live import Authorization, LivePermit, LiveProfile
from mgar_v3.models.deepseek import DeepSeekInvestigator


@pytest.fixture
def live_case(settings, trigger, request):
    limits = getattr(request, "param", {})
    configuration = settings.model_copy(
        update={
            "allow_paid": True,
            "allow_external_write": True,
            "deepseek_api_key": SecretStr("fixture-key-not-a-real-secret"),
        }
    )
    profile = LiveProfile(
        case_id="r1-fixture-model",
        project_id="orders-demo",
        repository="example/mgar-test",
        issue_number=1,
        actor="owner",
        question="Quote documented symptoms",
        source_path="README.md",
        source_ref="a" * 40,
        model_id="fixture-chat",
        price_version="fixture-v1",
        input_micros_per_million=1000000,
        output_micros_per_million=1000000,
        context_tokens=32768,
        max_cost_micros=limits.get("max_cost_micros", 100000),
        max_model_calls=limits.get("max_model_calls", 1),
    )
    authorization = Authorization(
        profile_hash=profile.profile_hash,
        approved_by="owner",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        allow_paid=True,
        allow_external_write=True,
    )
    permit = LivePermit(profile, authorization, configuration)
    engine = Engine(configuration, ArtifactStore(configuration.artifact_root))
    event = trigger.model_copy(
        update={"execution_mode": "live", "body": COMMAND + profile.question}
    )
    receipt = engine.ingest(event, permit)
    run = engine.begin_run(receipt.run_id)
    yield engine, permit, run
    engine.close()


@pytest.mark.parametrize(
    "live_case",
    [
        {"max_model_calls": 1},
        {"max_model_calls": 3, "max_cost_micros": 200000},
    ],
    indirect=True,
)
def test_atomic_global_reservations_enforce_profile_call_limit(live_case):
    engine, permit, run = live_case

    def reserve(index):
        try:
            return engine.reserve_call(
                run["run_id"], run["execution_epoch"], f"step-{index}", permit
            )
        except DomainError:
            return None

    with ThreadPoolExecutor(max_workers=6) as pool:
        calls = [call for call in pool.map(reserve, range(12)) if call]
    assert len(calls) == permit.profile.max_model_calls
    for call in calls:
        engine.settle_call(call, None)
    with engine.transaction() as conn:
        budget = conn.execute("SELECT * FROM app.authorization_budgets").fetchone()
        assert budget["reserved_micros"] == len(calls) * permit.profile.reservation_micros
        assert budget["settled_micros"] == 0 and budget["used_calls"] == len(calls)
        assert budget["call_limit"] == permit.profile.max_model_calls


@pytest.mark.parametrize(
    "live_case", [{"max_model_calls": 3, "max_cost_micros": 50000}], indirect=True
)
def test_total_cost_can_stop_calls_before_count_limit(live_case):
    engine, permit, run = live_case
    call = engine.reserve_call(run["run_id"], run["execution_epoch"], "first", permit)
    engine.settle_call(call, None)
    with pytest.raises(DomainError) as error:
        engine.reserve_call(run["run_id"], run["execution_epoch"], "second", permit)
    assert error.value.code == "BUDGET_EXHAUSTED"
    with engine.transaction() as conn:
        budget = conn.execute("SELECT * FROM app.authorization_budgets").fetchone()
        local = conn.execute("SELECT * FROM app.budgets").fetchone()
        assert budget["used_calls"] == 1 and budget["call_limit"] == 3
        assert (
            budget["reserved_micros"]
            == local["reserved_micros"]
            == permit.profile.reservation_micros
        )


def test_changing_call_limit_invalidates_previous_profile_authorization(live_case):
    engine, permit, _ = live_case
    changed = permit.profile.model_copy(update={"max_model_calls": 3})
    assert changed.profile_hash != permit.profile.profile_hash
    with pytest.raises(DomainError, match="stale or mismatched"):
        LivePermit(changed, permit.authorization, engine.settings).validate()


def test_unchanged_single_call_profile_retains_legacy_authorization_hash(live_case):
    _, permit, _ = live_case
    original = permit.profile.model_dump(mode="json")
    original.pop("max_model_calls")
    assert permit.profile.max_model_calls == 1
    assert permit.profile.profile_hash == digest(original)
    permit.validate()


@pytest.mark.parametrize(
    "live_case", [{"max_model_calls": 3, "max_cost_micros": 200000}], indirect=True
)
def test_call_limit_is_shared_across_runs_and_survives_settlement(live_case, trigger):
    engine, permit, first = live_case
    first_call = engine.reserve_call(first["run_id"], first["execution_epoch"], "first", permit)
    engine.settle_call(first_call, 1)
    engine.cancel(first["run_id"])
    event = trigger.model_copy(
        update={
            "external_event_id": "new-run-same-profile",
            "execution_mode": "live",
            "body": COMMAND + permit.profile.question,
        }
    )
    second_receipt = engine.ingest(event, permit)
    second = engine.begin_run(second_receipt.run_id)
    for step in ["second", "third"]:
        call = engine.reserve_call(second["run_id"], second["execution_epoch"], step, permit)
        engine.settle_call(call, 1)
    with pytest.raises(DomainError) as error:
        engine.reserve_call(second["run_id"], second["execution_epoch"], "fourth", permit)
    assert error.value.code == "BUDGET_EXHAUSTED"
    with engine.transaction() as conn:
        budget = conn.execute("SELECT * FROM app.authorization_budgets").fetchone()
        assert budget["used_calls"] == budget["call_limit"] == 3
        assert budget["settled_micros"] == 3 and budget["reserved_micros"] == 0
        assert conn.execute("SELECT count(*) AS n FROM app.usage_calls").fetchone()["n"] == 3


def test_model_success_settles_exact_usage_and_reuses_result_without_resend(live_case):
    engine, permit, run = live_case
    evidence_id = uuid4()
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "usage": {"prompt_tokens": 100, "completion_tokens": 20},
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": '{"claims":[{"statement":"Observation",'
                            '"quote":"Observation","evidence_id":"'
                            + str(evidence_id)
                            + '"}],"open_questions":[]}'
                        },
                    }
                ],
            },
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        model = DeepSeekInvestigator(engine, permit, client)
        result = model.investigate(
            run["run_id"],
            permit.profile.question,
            "Observation",
            evidence_id,
            execution_epoch=run["execution_epoch"],
        )
        assert result.claims[0].evidence_id == evidence_id
        assert (
            model.investigate(
                run["run_id"],
                permit.profile.question,
                "Observation",
                evidence_id,
                execution_epoch=run["execution_epoch"],
            )
            == result
        )
        assert len(requests) == 1
    with engine.transaction() as conn:
        budget = conn.execute("SELECT * FROM app.authorization_budgets").fetchone()
        assert budget["settled_micros"] == 120 and budget["reserved_micros"] == 0


@pytest.mark.parametrize(
    "response,reason",
    [
        ({"usage": None}, "USAGE_UNKNOWN"),
        (
            {"usage": {"prompt_tokens": 10, "completion_tokens": 3}, "choices": []},
            "MODEL_INVALID_RESPONSE",
        ),
        (
            {
                "usage": {"prompt_tokens": 10, "completion_tokens": 3},
                "choices": [{"finish_reason": "length"}],
            },
            "MODEL_OUTPUT_LIMIT",
        ),
    ],
)
def test_usage_and_output_failures_are_distinguished(live_case, response, reason):
    engine, permit, run = live_case
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=response))
    ) as client:
        with pytest.raises(DomainError) as error:
            DeepSeekInvestigator(engine, permit, client).investigate(
                run["run_id"],
                permit.profile.question,
                "Observation",
                uuid4(),
                execution_epoch=run["execution_epoch"],
            )
        assert error.value.code == reason
    with engine.transaction() as conn:
        call = conn.execute("SELECT * FROM app.usage_calls").fetchone()
        assert call["status"] == ("unknown" if reason == "USAGE_UNKNOWN" else "known")
    recorded = [e for e in engine.events(run["run_id"]) if e["kind"] == "model_response_recorded"]
    assert len(recorded) == 1
    import json

    assert (
        json.loads(engine.artifacts.read(recorded[0]["safe_payload"]["content_hash"])) == response
    )


def test_environment_flag_alone_cannot_authorize_live_ingress(engine, trigger):
    engine.settings = engine.settings.model_copy(update={"allow_paid": True})
    with pytest.raises(DomainError, match="reviewed profile"):
        engine.ingest(trigger.model_copy(update={"execution_mode": "live"}))


def test_changed_or_expired_profile_fails_before_network(live_case):
    engine, permit, _ = live_case
    changed = permit.profile.model_copy(update={"issue_number": 2})
    with pytest.raises(DomainError, match="stale or mismatched"):
        LivePermit(changed, permit.authorization, engine.settings).validate()


def test_real_mcp_write_and_readback_protocol_with_controlled_server(live_case, tmp_path):
    import json
    import sys
    from pathlib import Path

    from mgar_v3.effects.github import GitHubMCPTransport
    from mgar_v3.mcp.client import ReadOnlyGitHubMCP

    engine, permit, _ = live_case
    backend = tmp_path / "comments.json"
    server = Path(__file__).parent / "fixtures/github_write_server.py"
    writer = ReadOnlyGitHubMCP(
        "example/mgar-test", command=sys.executable, args=(str(server), str(backend))
    )
    reader = ReadOnlyGitHubMCP(
        "example/mgar-test", command=sys.executable, args=(str(server), str(backend))
    )
    transport = GitHubMCPTransport(engine.settings, permit, writer, reader)
    action = {
        "action_type": "issue_comment",
        "payload": {
            "repository": "example/mgar-test",
            "issue_number": 1,
            "body": "Controlled result <!-- mgar-action:fixture -->",
        },
    }
    assert transport.send(action) == "1"
    assert transport.find(action) == "1"
    assert len(json.loads(backend.read_text())) == 1


def test_unrecognized_enable_flag_is_disabled(settings):
    from mgar_v3.settings import Settings

    copy = Settings(
        database_url=settings.database_url,
        local_token=settings.local_token,
        allow_paid="2",
        allow_external_write="yes",
    )
    assert not copy.allow_paid and not copy.allow_external_write


def test_live_preflight_does_not_authorize_or_call_network(live_case, tmp_path):
    from mgar_v3.preflight import preflight

    engine, permit, _ = live_case
    path = tmp_path / "profile.json"
    path.write_text(permit.profile.model_dump_json())
    result = preflight(path, engine.settings)
    assert result["status"] == "NOT_RUN" and not result["ready_for_execution"]
    assert result["paid_calls"] == result["network_calls"] == 0
    assert "matching_user_authorization" in result["missing"]
    assert result["maximum_model_calls"] == permit.profile.max_model_calls
    assert result["maximum_cost_micros"] == permit.profile.max_cost_micros


@pytest.mark.parametrize("limit", [0, True, "3", 4])
def test_invalid_call_limits_are_rejected_before_execution(live_case, limit):
    from pydantic import ValidationError

    _, permit, _ = live_case
    payload = {**permit.profile.model_dump(), "max_model_calls": limit}
    with pytest.raises(ValidationError):
        LiveProfile.model_validate(payload)


@pytest.mark.parametrize("recovery_gap", [None, "report_commit", "model_commit"])
def test_live_policy_path_with_real_graph_mcp_and_fixture_model(
    live_case, trigger, tmp_path, monkeypatch, recovery_gap
):
    import json
    import sys
    from pathlib import Path

    from mgar_v3.effects.dispatcher import Dispatcher
    from mgar_v3.effects.github import GitHubMCPTransport
    from mgar_v3.graph.live_investigator import LiveInvestigator
    from mgar_v3.graph.runner import Runner
    from mgar_v3.mcp.client import ReadOnlyGitHubMCP

    engine, permit, initial = live_case
    engine.cancel(initial["run_id"])
    event = trigger.model_copy(
        update={
            "external_event_id": "integrated-live-policy",
            "execution_mode": "live",
            "body": COMMAND + permit.profile.question,
        }
    )
    receipt = engine.ingest(event, permit)
    source_server = Path(__file__).parent / "fixtures/mcp_server.py"
    reader = ReadOnlyGitHubMCP(
        "example/mgar-test", command=sys.executable, args=(str(source_server),)
    )
    requests = []

    def respond(request):
        requests.append(request)
        prompt = json.loads(json.loads(request.content)["messages"][1]["content"])
        quote = prompt["source"].strip()
        finding = {
            "claims": [{"statement": quote, "quote": quote, "evidence_id": prompt["evidence_id"]}],
            "open_questions": [],
        }
        return httpx.Response(
            200,
            json={
                "usage": {"prompt_tokens": 100, "completion_tokens": 20},
                "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(finding)}}],
            },
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as model:
        investigator = LiveInvestigator(engine, receipt.run_id, permit, reader, model)
        if recovery_gap is not None:
            original_report = engine.commit_report
            original_settle = engine.settle_call

            def crash_report(*args):
                raise RuntimeError("injected crash before report commit")

            def crash_model(*args):
                original_settle(*args)
                raise RuntimeError("injected crash after model result commit")

            monkeypatch.setattr(engine, "commit_report", crash_report)
            if recovery_gap == "model_commit":
                monkeypatch.setattr(engine, "settle_call", crash_model)
            with pytest.raises(RuntimeError):
                Runner(engine, investigator).execute(receipt.run_id)
            engine.recover()
            assert engine.resume(receipt.run_id) == receipt.run_id
            monkeypatch.setattr(engine, "commit_report", original_report)
            monkeypatch.setattr(engine, "settle_call", original_settle)
        action_id = Runner(engine, investigator).execute(receipt.run_id)
        assert len(requests) == 1
    action = engine.actions(receipt.run_id)[0]
    engine.approve(action_id, action["payload_hash"], "owner")
    remote = tmp_path / "remote.json"
    server = Path(__file__).parent / "fixtures/github_write_server.py"
    writer = ReadOnlyGitHubMCP(
        "example/mgar-test", command=sys.executable, args=(str(server), str(remote))
    )
    lookup = ReadOnlyGitHubMCP(
        "example/mgar-test", command=sys.executable, args=(str(server), str(remote))
    )
    dispatcher = Dispatcher(engine, GitHubMCPTransport(engine.settings, permit, writer, lookup))
    assert dispatcher.tick() == action_id and dispatcher.tick() is None
    assert engine.get_run(receipt.run_id)["status"] == "succeeded"
    assert len(json.loads(remote.read_text())) == 1


def test_stale_model_execution_cannot_borrow_new_epoch(live_case):
    engine, permit, run = live_case
    engine.recover()
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: pytest.fail("must not call model"))
    ) as client:
        with pytest.raises(DomainError) as error:
            DeepSeekInvestigator(engine, permit, client).investigate(
                run["run_id"],
                permit.profile.question,
                "Observation",
                uuid4(),
                execution_epoch=run["execution_epoch"],
            )
        assert error.value.code == "FENCED"
