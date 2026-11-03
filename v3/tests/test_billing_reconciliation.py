from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import SecretStr

from mgar_v3.benchmark.domain import EvaluationAuthorization
from mgar_v3.benchmark.reconcile import HourlyReceipt, reconcile
from mgar_v3.domain import DomainError, digest
from mgar_v3.live import Authorization, LivePermit, LiveProfile
from mgar_v3.proactivity.domain import Activation, Policy, ProactiveEvent


@pytest.fixture
def pending(engine):
    engine.settings.allow_paid = True
    engine.settings.deepseek_api_key = SecretStr("controlled-billing-key")
    expiry = datetime.now(UTC) + timedelta(hours=1)
    profiles = []
    for arm in "AB":
        p = LiveProfile(
            case_id="r6-eval:billing:" + arm,
            project_id=engine.settings.project_id,
            repository=engine.settings.repository,
            issue_number=1,
            actor="owner",
            question="Read contract",
            source_ref="a" * 40,
            model_id="controlled",
            price_version="controlled",
            input_micros_per_million=300000,
            output_micros_per_million=1200000,
            context_tokens=12288,
            max_output_tokens=2048,
            max_model_calls=3,
            max_cost_micros=18432,
        )
        profiles.append(
            {
                "case_id": "billing",
                "arm": arm,
                "profile": p.model_dump(mode="json"),
                "profile_hash": p.profile_hash,
            }
        )
    manifest = {
        "repository": engine.settings.repository,
        "profiles": profiles,
        "max_new_calls": 3,
        "max_new_cost_micros": 18432,
        "prior_cost_micros": 0,
    }
    engine.benchmarks.authorize(
        manifest,
        EvaluationAuthorization(
            manifest_hash=digest(manifest),
            approved_by="owner",
            expires_at=expiry,
            max_new_calls=3,
            max_new_cost_micros=18432,
            allow_paid=True,
        ),
    )
    calls = []
    for item in profiles:
        p = LiveProfile.model_validate(item["profile"])
        permit = LivePermit(
            p,
            Authorization(
                profile_hash=p.profile_hash,
                approved_by="owner",
                expires_at=expiry,
                allow_paid=True,
                allow_external_write=False,
            ),
            engine.settings,
        )
        policy = Policy(
            repository=p.repository,
            allowed_actors=["owner"],
            kinds=["pr_comment"],
            source_ref="a" * 40,
            question=p.question,
            expires_at=expiry,
            execution_mode="live",
            model_profile_hash=p.profile_hash,
            max_model_calls=3,
            max_cost_micros=18432,
        )
        engine.proactivity.create(policy, "owner", permit)
        engine.proactivity.activate(
            Activation(policy_hash=policy.policy_hash, mode="auto"), "owner", permit
        )
        run = engine.proactivity.observe(
            ProactiveEvent(
                external_event_id=str(uuid4()),
                repository=p.repository,
                actor="owner",
                kind="pr_comment",
                issue_number=1,
                source_ref="a" * 40,
            ),
            permit,
        )[0]["run_id"]
        engine.benchmarks.bind(run, digest(manifest), "billing", item["arm"])
        epoch = engine.begin_run(run)["execution_epoch"]
        if item["arm"] == "A":
            call = engine.reserve_call(run, epoch, "known", permit)
            engine.settle_call(
                call,
                42,
                {
                    "prompt_tokens": 100,
                    "completion_tokens": 10,
                    "prompt_cache_hit_tokens": 20,
                    "prompt_cache_miss_tokens": 80,
                },
            )
        else:
            calls = [engine.reserve_call(run, epoch, "unknown-" + str(i), permit) for i in range(2)]
            for call in calls:
                engine.settle_call(call, None)
            engine.fail_run(run, "USAGE_UNKNOWN")
    start = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    bill = HourlyReceipt(
        bill_id="provider-hourly-example",
        start=start,
        end=start + timedelta(hours=1),
        model="controlled",
        credential_alias="fixture",
        currency="CNY",
        request_count=3,
        input_cache_hit_tokens=30,
        input_cache_miss_tokens=280,
        output_tokens=50,
        cache_hit_unit_price=Decimal(".00000004"),
        cache_miss_unit_price=Decimal(".000002"),
        output_unit_price=Decimal(".000008"),
        paid_amount=Decimal(".0009612"),
        complete_scope=True,
        approved_by="owner",
        source="user_supplied_provider_bill",
    )
    return calls, bill


def test_complete_provider_aggregate_settles_only_shared_scope_and_keeps_usage_honest(
    engine, pending
):
    calls, bill = pending
    result = reconcile(engine, bill, calls)
    assert result["group_prompt_tokens"] == 210 and result["group_completion_tokens"] == 40
    assert result["group_upper_micros"] == 112
    assert reconcile(engine, bill, calls) == result
    with engine.transaction() as conn:
        rows = conn.execute(
            "SELECT * FROM app.usage_calls WHERE call_id=ANY(%s)", (calls,)
        ).fetchall()
        assert all(r["status"] == "known" and r["settled_micros"] == 56 for r in rows)
        assert all(
            "prompt_tokens" not in r["usage"] and r["usage"]["per_call_tokens_unknown"]
            for r in rows
        )
        budget = conn.execute(
            "SELECT used_calls,reserved_micros,settled_micros FROM app.benchmark_budgets"
        ).fetchone()
        assert budget == {"used_calls": 3, "reserved_micros": 0, "settled_micros": 154}
        assert not conn.execute("SELECT 1 FROM app.outbox").fetchone()


@pytest.mark.parametrize(
    "change",
    [
        {"request_count": 4},
        {"output_tokens": 9},
        {"model": "another-model"},
        {"approved_by": "outsider"},
    ],
)
def test_incomplete_or_unattributable_bill_never_releases_hold(engine, pending, change):
    calls, bill = pending
    altered = bill.model_copy(update=change)
    with pytest.raises(DomainError):
        reconcile(engine, altered, calls)
    with engine.transaction() as conn:
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM app.usage_calls WHERE status='unknown'"
            ).fetchone()["n"]
            == 2
        )
        assert (
            conn.execute("SELECT reserved_micros FROM app.benchmark_budgets").fetchone()[
                "reserved_micros"
            ]
            == 12288
        )
