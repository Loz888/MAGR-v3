import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr

from mgar_v3.domain import DomainError, TriggerInput, digest
from mgar_v3.evaluation.domain import Assessment, TrialAuthorization
from mgar_v3.evaluation.oracle import expected, grade
from mgar_v3.live import Authorization, LivePermit, LiveProfile
from mgar_v3.models.structured import StructuredModel


def setup_trial(engine, call_limit=2):
    engine.settings.allow_paid = True
    engine.settings.deepseek_api_key = SecretStr("fixture-not-a-real-key")
    root = "f" * 64
    with engine.transaction() as conn:
        conn.execute(
            (
                "INSERT INTO "
                "app.authorization_budgets(profile_hash,limit_micros,call_limit,us"
                "ed_calls,settled_micros) VALUES (%s,2000000,3,3,100)"
            ),
            (root,),
        )
    profiles = []
    for i in range(3):
        profiles.append(
            LiveProfile(
                investigation_version="r4-experts-v1",
                knowledge_snapshot_ids=[str(uuid4())],
                rag_config_hash="a" * 64,
                data_fixture_hash="b" * 64,
                case_id=str(i),
                project_id=engine.settings.project_id,
                repository=engine.settings.repository,
                issue_number=i + 1,
                actor="owner",
                question="Audit paid order statistics",
                source_kind="file",
                source_ref="a" * 40,
                model_id="controlled-model",
                price_version="controlled-price",
                input_micros_per_million=300000,
                output_micros_per_million=1200000,
                context_tokens=16384,
                max_output_tokens=2048,
                max_cost_micros=100000,
                max_model_calls=3,
            )
        )
    manifest = {
        "additional_call_limit": call_limit,
        "predecessor_budget_hash": root,
        "prior_cost_micros": 100,
        "profiles": [
            {"case_id": str(i), "arm": "multi", "profile": p.model_dump(mode="json")}
            for i, p in enumerate(profiles)
        ],
    }
    auth = TrialAuthorization(
        manifest_hash=digest(manifest),
        predecessor_budget_hash=root,
        approved_by="owner",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        additional_call_limit=call_limit,
        cumulative_cost_limit_micros=2000000,
        allow_paid=True,
        basis="Controlled test, no external requests",
    )
    trial = engine.trials.authorize(manifest, auth)
    runs = []
    for i, p in enumerate(profiles):
        engine.trials.bind(trial["trial_id"], p, str(i), "multi")
        permit = LivePermit(
            p,
            Authorization(
                profile_hash=p.profile_hash,
                approved_by="owner",
                expires_at=auth.expires_at,
                allow_paid=True,
            ),
            engine.settings,
        )
        event = TriggerInput(
            source="cli",
            external_event_id=str(i),
            project_id=engine.settings.project_id,
            repository=engine.settings.repository,
            issue_number=i + 1,
            actor="owner",
            body="/mgar investigate " + p.question,
            execution_mode="live",
        )
        receipt = engine.ingest(event, permit)
        run = engine.begin_run(receipt.run_id)
        runs.append((run, permit))
    return trial, manifest, auth, runs


def test_shared_trial_budget_is_atomic_across_profiles_and_does_not_reset_prior(engine):
    trial, manifest, auth, runs = setup_trial(engine)

    def reserve(pair):
        run, permit = pair
        try:
            return engine.reserve_call(run["run_id"], run["execution_epoch"], "step", permit)
        except DomainError:
            return None

    with ThreadPoolExecutor(max_workers=3) as pool:
        calls = list(pool.map(reserve, runs))
    calls = [c for c in calls if c is not None]
    assert len(calls) == 2
    for i, call in enumerate(calls, 1):
        engine.settle_call(call, i)
    assert engine.trials.authorize(manifest, auth)["trial_id"] == trial["trial_id"]
    with engine.transaction() as conn:
        old = conn.execute(
            (
                "SELECT used_calls,settled_micros,reserved_micros FROM "
                "app.authorization_budgets WHERE profile_hash=%s"
            ),
            ("f" * 64,),
        ).fetchone()
        new = conn.execute(
            "SELECT * FROM app.authorization_budgets WHERE profile_hash=%s", (trial["budget_hash"],)
        ).fetchone()
    assert old == {"used_calls": 3, "settled_micros": 100, "reserved_micros": 0}
    assert new["used_calls"] == 2 and new["settled_micros"] == 3 and new["reserved_micros"] == 0
    assert new["limit_micros"] == 1999900


def test_unknown_trial_request_stops_further_calls_and_holds_reservation(engine):
    trial, _, _, runs = setup_trial(engine)
    run, p = runs[0]
    call = engine.reserve_call(run["run_id"], run["execution_epoch"], "unknown", p)
    engine.settle_call(call, None)
    run, p = runs[1]
    with pytest.raises(DomainError) as exc:
        engine.reserve_call(run["run_id"], run["execution_epoch"], "next", p)
    assert exc.value.code == "USAGE_UNKNOWN"
    with engine.transaction() as conn:
        budget = conn.execute(
            "SELECT * FROM app.authorization_budgets WHERE profile_hash=%s", (trial["budget_hash"],)
        ).fetchone()
    assert budget["used_calls"] == 1 and budget["reserved_micros"] > 0


def test_trial_grant_and_frozen_profile_cannot_be_changed(engine):
    trial, manifest, auth, runs = setup_trial(engine)
    with pytest.raises(DomainError):
        engine.trials.authorize(manifest | {"changed": True}, auth)
    with pytest.raises(DomainError):
        engine.trials.bind(
            trial["trial_id"],
            runs[0][1].profile.model_copy(update={"question": "changed"}),
            "0",
            "multi",
        )
    with pytest.raises(DomainError):
        engine.trials.authorize(manifest, auth.model_copy(update={"allow_paid": False}))


def test_structured_model_is_cached_settled_and_never_resends_unknown(engine):
    trial, _, _, runs = setup_trial(engine)
    run, permit = runs[0]
    calls = []
    answer = {
        "status": "insufficient",
        "metrics": {},
        "conclusions": {},
        "interpretations": [],
        "missing_sources": ["data"],
        "caveats": ["Controlled model response"],
    }

    def respond(request):
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(answer)}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 80},
            },
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        model = StructuredModel(engine, permit, client)
        result = model.complete(
            run["run_id"],
            run["execution_epoch"],
            "analysis",
            "Return JSON",
            {"input": "test"},
            Assessment,
        )
        assert (
            model.complete(
                run["run_id"],
                run["execution_epoch"],
                "analysis",
                "Return JSON",
                {"input": "test"},
                Assessment,
            )
            == result
        )
    assert len(calls) == 1
    with engine.transaction() as conn:
        row = conn.execute(
            "SELECT * FROM app.usage_calls WHERE run_id=%s", (run["run_id"],)
        ).fetchone()
    assert (
        row["status"] == "known"
        and row["settled_micros"] == 126
        and row["trial_budget_hash"] == trial["budget_hash"]
    )


def test_independent_oracle_detects_wrong_math_unread_references_and_missing_data():
    rows = [
        {"order_id": "one", "status": "paid", "amount": "10.00"},
        {"order_id": "one", "status": "paid", "amount": "10.00"},
        {"order_id": "two", "status": "paid", "amount": "5.00"},
    ]
    oracle = expected(rows, "normal")
    answer = {
        "status": "supported",
        "metrics": oracle["metrics"],
        "conclusions": oracle["conclusions"],
        "interpretations": [{"text": "Cross source observation", "evidence_ids": ["k", "r", "d"]}],
        "missing_sources": [],
    }
    assert grade(answer, oracle, {"k": "knowledge", "r": "repo", "d": "data"}, mechanical=True)[
        "passed"
    ]
    bad = answer | {"metrics": answer["metrics"] | {"unique_paid_amount": "25"}}
    assert not grade(bad, oracle, {"k": "knowledge", "r": "repo", "d": "data"}, mechanical=True)[
        "passed"
    ]
    assert not grade(answer, oracle, {"k": "knowledge", "r": "repo"}, mechanical=True)["passed"]
    missing = {
        "status": "insufficient",
        "metrics": {"event_count": "3"},
        "missing_sources": ["data"],
    }
    assert not grade(missing, expected(None, "missing"), {}, mechanical=True)["passed"]
    denied = {"status": "denied", "metrics": {}, "missing_sources": ["knowledge"]}
    assert not grade(denied, expected(None, "revoked"), {}, mechanical=True, forbidden_seen=True)[
        "passed"
    ]


def test_data_roles_remain_valid_and_isolated_across_trial_projects(engine, tmp_path):
    import psycopg
    from psycopg import sql

    from mgar_v3.agents.data import DataReader, provision

    first = tmp_path / "first.json"
    provision(engine, first)
    reader1 = DataReader(json.loads(first.read_text()))
    original = engine.settings.project_id
    engine.settings.project_id = "another-trial-project"
    second = tmp_path / "second.json"
    provision(
        engine, second, [{"event_id": 1, "order_id": "other", "status": "paid", "amount": "99.00"}]
    )
    reader2 = DataReader(json.loads(second.read_text()))
    engine.settings.project_id = original
    assert reader1.profile["reader_role"] != reader2.profile["reader_role"]
    assert reader1.query(f"SELECT count(*) AS n FROM {reader1.profile['schema']}.orders", [])[
        "rows"
    ] == [[4]]
    assert reader2.query(f"SELECT count(*) AS n FROM {reader2.profile['schema']}.orders", [])[
        "rows"
    ] == [[1]]
    with psycopg.connect(reader1.dsn.get_secret_value(), autocommit=True) as conn:
        with pytest.raises(psycopg.Error):
            conn.execute(
                sql.SQL("SELECT * FROM {}.orders").format(sql.Identifier(reader2.profile["schema"]))
            )


def test_multi_and_single_real_mcp_graph_with_controlled_model_and_negative_cases(
    engine, trigger, tmp_path, monkeypatch
):
    from test_r4_knowledge import upload

    from mgar_v3.agents.data import provision
    from mgar_v3.evaluation.provider import TrialExperts
    from mgar_v3.evaluation.runtime import mcp_authority
    from mgar_v3.graph.experts import execute_experts
    from mgar_v3.graph.live_investigator import LiveInvestigator
    from mgar_v3.graph.runner import Runner
    from mgar_v3.models.deepseek import DeepSeekInvestigator
    from mgar_v3.rag.domain import ACLUpdate
    from mgar_v3.settings import ROOT

    cfg = engine.settings
    cfg.graph_version = "r4-experts-v1"
    cfg.allow_paid = True
    cfg.deepseek_api_key = SecretStr("controlled-no-external-model-key")
    cfg.data_profile_path = tmp_path / "reader.json"
    data = provision(engine, cfg.data_profile_path)
    snapshot = upload(engine, (ROOT / "eval/r4-live/policy.md").read_text())
    _, chunks, _ = engine.knowledge.eligible(snapshot, "owner")
    doc = chunks[0]["document_id"]
    source = (ROOT / "src/mgar_v3/agents/experts.py").read_bytes()

    def github(self):
        return source, "github://example/mgar-test/code.py?ref=" + "a" * 40, "b" * 40

    monkeypatch.setattr(LiveInvestigator, "read", github)
    root = "e" * 64
    with engine.transaction() as conn:
        conn.execute(
            (
                "INSERT INTO "
                "app.authorization_budgets(profile_hash,limit_micros,call_limit,us"
                "ed_calls,settled_micros) VALUES (%s,2000000,3,3,100)"
            ),
            (root,),
        )
    profiles = []
    for kind in ("normal", "revoked", "missing"):
        for arm in ("multi", "single"):
            p = LiveProfile(
                investigation_version="r4-experts-v1",
                knowledge_snapshot_ids=[str(snapshot)],
                rag_config_hash=digest(engine.knowledge.config()),
                data_fixture_hash=data["fixture_hash"],
                case_id=kind + arm,
                project_id=cfg.project_id,
                repository=cfg.repository,
                issue_number=1,
                actor="owner",
                question="Audit order statistics across knowledge, SQL and code.",
                source_kind="file",
                source_path="code.py",
                source_ref="a" * 40,
                model_id="controlled-model",
                price_version="controlled-price",
                input_micros_per_million=300000,
                output_micros_per_million=1200000,
                context_tokens=16384,
                max_output_tokens=2048,
                max_cost_micros=100000,
                max_model_calls=3,
            )
            profiles.append({"case_id": kind, "arm": arm, "profile": p.model_dump(mode="json")})
    manifest = {
        "additional_call_limit": 24,
        "predecessor_budget_hash": root,
        "prior_cost_micros": 100,
        "profiles": profiles,
    }
    auth = TrialAuthorization(
        manifest_hash=digest(manifest),
        predecessor_budget_hash=root,
        approved_by="owner",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        additional_call_limit=24,
        cumulative_cost_limit_micros=2000000,
        allow_paid=True,
        basis="Controlled transport test; never use the external provider",
    )
    trial = engine.trials.authorize(manifest, auth)
    requests = []

    def respond(request):
        body = json.loads(request.content)
        payload = json.loads(body["messages"][1]["content"])
        requests.append(body)
        if "source" in payload:
            text = payload["source"]
            offset = text.find("count(DISTINCT order_id)")
            quote = text[max(0, offset - 30) : offset + 130] if offset >= 0 else text[:1000]
            answer = {
                "claims": [
                    {"statement": quote, "quote": quote, "evidence_id": payload["evidence_id"]}
                ],
                "open_questions": [],
            }
        else:
            roles = {item["role"] for item in payload["sources"]}
            missing = list({"knowledge", "repo", "data"} - roles)
            answer = {
                "status": "supported"
                if not missing
                else "denied"
                if "knowledge" in missing
                else "insufficient",
                "metrics": {
                    "event_count": "3",
                    "distinct_orders": "2",
                    "duplicate_events": "1",
                    "event_amount": "32.00",
                    "unique_paid_amount": "19.50",
                    "overcount_amount": "12.50",
                    "cancelled_events": "1",
                }
                if not missing
                else {},
                "conclusions": {
                    "duplicates_present": True,
                    "paid_filter_excludes_cancelled": True,
                    "amount_is_deduplicated": False,
                }
                if not missing
                else {},
                "interpretations": [
                    {
                        "text": "Controlled source interpretation",
                        "evidence_ids": [s["evidence_id"] for s in payload["sources"]],
                    }
                ],
                "missing_sources": missing,
                "caveats": ["Controlled fixture HTTP model, not live inference"],
            }
        return httpx.Response(
            200,
            json={
                "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(answer)}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 100},
            },
        )

    deep_init = DeepSeekInvestigator.__init__
    structured_init = StructuredModel.__init__
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:

        def deep(self, authority, permit, client_unused=None):
            deep_init(self, authority, permit, client)

        def structured(self, authority, permit, client_unused=None):
            structured_init(self, authority, permit, client)

        monkeypatch.setattr(DeepSeekInvestigator, "__init__", deep)
        monkeypatch.setattr(StructuredModel, "__init__", structured)
        for entry in profiles:
            kind, arm = entry["case_id"], entry["arm"]
            engine.knowledge.acl(doc, ACLUpdate(revoked=kind == "revoked"))
            cfg.data_profile_path = tmp_path / (
                "missing.json" if kind == "missing" else "reader.json"
            )
            profile = LiveProfile.model_validate(entry["profile"])
            engine.trials.bind(trial["trial_id"], profile, kind, arm)
            authorization = Authorization(
                profile_hash=profile.profile_hash,
                approved_by="owner",
                expires_at=auth.expires_at,
                allow_paid=True,
            )
            permit = LivePermit(profile, authorization, cfg)
            cfg.live_profile_path = tmp_path / "profile.json"
            cfg.live_profile_path.write_text(profile.model_dump_json())
            cfg.live_authorization_path = tmp_path / "auth.json"
            cfg.live_authorization_path.write_text(authorization.model_dump_json())
            event = trigger.model_copy(
                update={
                    "source": "cli",
                    "external_event_id": kind + arm,
                    "execution_mode": "live",
                    "body": "/mgar investigate " + profile.question,
                }
            )
            receipt = engine.ingest(event, permit)
            engine.tools.requested_snapshots(receipt.run_id, [snapshot])
            case = {"kind": kind, "denied_chunk_id": str(chunks[0]["chunk_id"])}
            with mcp_authority(engine):
                execute_experts(
                    Runner(engine), receipt.run_id, TrialExperts(engine, permit, case, arm)
                )
            resolution = engine.tools.resolution(receipt.run_id)
            with engine.transaction() as conn:
                analysis = conn.execute(
                    "SELECT result FROM app.r4_analyses WHERE run_id=%s", (receipt.run_id,)
                ).fetchone()["result"]
                reads = conn.execute(
                    (
                        "SELECT e.evidence_id,r.role FROM app.evidence e JOIN "
                        "app.r4_reads r USING(evidence_id) WHERE run_id=%s"
                    ),
                    (receipt.run_id,),
                ).fetchall()
            rows = json.loads((ROOT / "eval/r4-live/cases.json").read_text())[0]["rows"]
            oracle = expected(rows, kind)
            assert grade(
                analysis,
                oracle,
                {str(r["evidence_id"]): r["role"] for r in reads},
                mechanical=resolution["verdict"]["passed"],
            )["passed"]
            if kind != "normal":
                reasons = [
                    e["safe_payload"]["reason_code"]
                    for e in engine.events(receipt.run_id)
                    if e["kind"] == "readonly_tool_denied"
                ]
                assert (
                    "NOT_FOUND" if kind == "revoked" else "DATA_SOURCE_NOT_CONFIGURED"
                ) in reasons
            engine.cancel(receipt.run_id)
    assert len(requests) == 11
    with engine.transaction() as conn:
        assert (
            conn.execute(
                ("SELECT used_calls FROM app.authorization_budgets WHERE profile_hash=%s"),
                (trial["budget_hash"],),
            ).fetchone()["used_calls"]
            == 11
        )


def test_bounded_recovery_preserves_root_and_original_profile_counters(engine):
    trial, _, _, runs = setup_trial(engine, call_limit=24)
    parent, permit = runs[0]
    for i in range(3):
        call = engine.reserve_call(parent["run_id"], parent["execution_epoch"], str(i), permit)
        engine.settle_call(call, 1)
    engine.fail_run(parent["run_id"], "BUDGET_EXHAUSTED", parent["execution_epoch"])
    recovery = permit.profile.model_copy(update={"case_id": permit.profile.case_id + ":recovery-1"})
    with pytest.raises(DomainError):
        engine.trials.bind_recovery(
            trial["trial_id"],
            parent["run_id"],
            recovery.model_copy(update={"question": "changed"}),
            "hash",
            "test",
        )
    engine.trials.bind_recovery(
        trial["trial_id"], parent["run_id"], recovery, "hash", "test bounded recovery"
    )
    engine.trials.bind_recovery(
        trial["trial_id"], parent["run_id"], recovery, "hash", "test idempotence"
    )
    with engine.transaction() as conn:
        old = conn.execute(
            ("SELECT used_calls FROM app.authorization_budgets WHERE profile_hash=%s"),
            (permit.profile.profile_hash,),
        ).fetchone()
        shared = conn.execute(
            ("SELECT used_calls,call_limit FROM app.authorization_budgets WHERE profile_hash=%s"),
            (trial["budget_hash"],),
        ).fetchone()
        new = conn.execute(
            ("SELECT used_calls FROM app.authorization_budgets WHERE profile_hash=%s"),
            (recovery.profile_hash,),
        ).fetchone()
    assert old["used_calls"] == 3 and new["used_calls"] == 0
    assert shared == {"used_calls": 3, "call_limit": 24}
