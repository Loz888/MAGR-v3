"""V3.5 E02: Memory / Compaction / Expert ablation tests.

Offline tests cover arm gating, memory scoping, compaction accounting, expert
triggering, bootstrap determinism, live authorization and budget caps, and
report immutability.  One integration test runs all four arms on a real task
through the E01 sandbox so the fixture grid is not a paper exercise.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from mgar_v3.quality_eval.v35 import ablations, grader, live_provider, runner, tasks
from mgar_v3.repair.repair_agent import RepairFeedback, RepairPrompt

EXPERIMENT = runner.EXPERIMENT


def _dev() -> list[tasks.Task]:
    return tasks.load_development(EXPERIMENT)


def _task(kind: str) -> tasks.Task:
    return next(item for item in _dev() if item.kind == kind)


def _prompt(
    task: tasks.Task,
    *,
    attempt: int = 1,
    feedback: list[RepairFeedback] | None = None,
) -> RepairPrompt:
    snapshot = runner.snapshot_for(task)
    return RepairPrompt(
        repository=task.repository,
        objective=task.prompt,
        language=task.language,
        attempt=attempt,
        max_attempts=int(task.budget["max_attempts"]),
        files={path: snapshot.files[path] for path in task.allowed_paths},
        allowed_paths=list(task.allowed_paths),
        limits=runner.PATCH_LIMITS,
        feedback=feedback or [],
    )


def _auth_file(
    tmp_path: Path,
    *,
    max_calls: int = 4,
    max_cost_micros: int = 1_000_000,
    allow_paid: bool = True,
    actor: str = "loz888",
    approved_by: str = "loz888",
    expires_in: timedelta = timedelta(hours=1),
) -> Path:
    payload = {
        "schema": live_provider.AUTHORIZATION_SCHEMA,
        "approved_by": approved_by,
        "actor": actor,
        "approved_at": (datetime.now(UTC) - timedelta(minutes=5)).isoformat(),
        "expires_at": (datetime.now(UTC) + expires_in).isoformat(),
        "allow_paid": allow_paid,
        "model_id": live_provider.DEFAULT_MODEL,
        "max_cost_micros": max_cost_micros,
        "max_calls": max_calls,
        "endpoint": live_provider.DEFAULT_ENDPOINT,
    }
    path = tmp_path / "authorization.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _completion_payload(
    *,
    prompt_tokens: int = 10,
    completion_tokens: int = 5,
    content: str = "{}",
    finish: str = "stop",
) -> dict[str, Any]:
    return {
        "model": "deepseek-chat",
        "choices": [
            {
                "message": {"content": content},
                "finish_reason": finish,
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
        },
    }


def _completion(
    tmp_path: Path,
    *,
    max_calls: int = 4,
    max_cost_micros: int = 1_000_000,
    handler: Any,
) -> live_provider.DeepSeekCompletion:
    authorization = live_provider.load_authorization(
        _auth_file(tmp_path, max_calls=max_calls, max_cost_micros=max_cost_micros),
        actor="loz888",
    )
    return live_provider.DeepSeekCompletion(
        api_key="test-key-not-a-secret",
        authorization=authorization,
        budget=live_provider.LiveBudget(),
        transport=httpx.MockTransport(handler),
        clock=lambda: 0.0,
    )


def test_four_arms_are_paired_and_distinct() -> None:
    arms = ablations.ARMS
    assert [item.id for item in arms] == ["A", "B", "C", "D"]
    assert [item.memory for item in arms] == [False, True, True, True]
    assert [item.compaction for item in arms] == [False, False, True, True]
    assert [item.experts for item in arms] == [False, False, False, True]
    assert len({(item.memory, item.compaction, item.experts) for item in arms}) == 4


def test_memory_corpus_filters_stale_unverified_and_superseded() -> None:
    task = _task("known_bug_fix")
    corpus = ablations.memory_corpus()
    eligible = ablations.eligible_memories(corpus, task)
    assert eligible
    assert all(item.commit == task.base_commit for item in eligible)
    assert all(item.verified and item.status == "active" for item in eligible)
    own = [item for item in corpus if item.task_id == task.task_id]
    assert len(own) == 5  # two verified facts plus three decoys
    assert len(eligible) == 2
    subjects = {item.subject_key for item in eligible}
    assert "click.decoy.stale-commit" not in subjects
    assert "click.decoy.superseded" not in subjects
    assert not any(not item.verified for item in eligible)


def test_arm_context_gates_memory_and_compaction_per_arm() -> None:
    task = _task("known_bug_fix")
    prompt = _prompt(task)
    contexts = {
        arm.id: ablations.ArmContextBuilder(task, arm).build(prompt) for arm in ablations.ARMS
    }
    assert contexts["A"].payload["memory_hints"] == []
    assert contexts["A"].payload["history"]["strategy"] == "raw_all"
    assert contexts["B"].payload["memory_hints"]
    assert contexts["B"].payload["history"]["strategy"] == "raw_all"
    assert contexts["C"].payload["memory_hints"]
    assert contexts["C"].payload["history"]["strategy"] == "compacted"
    assert contexts["C"].payload["history"]["summary"] is not None
    assert contexts["D"].payload["history"]["strategy"] == "compacted"
    assert contexts["A"].memory_filtered == contexts["B"].memory_filtered
    assert contexts["A"].memory_used == 0
    assert contexts["B"].memory_used >= 1


def test_compaction_accounts_for_every_observation_and_reports_loss() -> None:
    task = _task("regression_investigation")
    feedback = [
        RepairFeedback(
            attempt=1,
            stage="public_tests",
            code="TEST_FAILED",
            message="short-help truncation still splits on an abbreviation",
            failed_tests=list(task.public_targets),
        )
    ]
    prompt = _prompt(task, attempt=2, feedback=feedback)
    builder = ablations.ArmContextBuilder(task, ablations.ARM_BY_ID["C"])
    context = builder.build(prompt)
    assert context.strategy == "compacted"
    summary = context.payload["history"]["summary"]
    assert summary is not None
    kept = context.payload["history"]["observations"]
    assert context.observations_kept == len(kept)
    assert context.observations_total >= context.observations_kept
    assert (
        context.observations_kept + context.observations_omitted + context.observations_deduplicated
        <= context.observations_total
    )
    evidence = {item["evidence_id"] for item in kept if item.get("evidence_id")} | set(
        summary["evidence_ids"]
    )
    observed = {
        item.evidence_id for item in ablations.build_observations(task, prompt) if item.evidence_id
    }
    assert observed <= evidence
    assert 0.0 <= context.loss_rate <= 1.0
    assert 0.0 <= context.duplicate_work_rate <= 1.0


def test_experts_only_consult_on_multifile_revision() -> None:
    single = _task("known_bug_fix")
    single_context = ablations.ArmContextBuilder(single, ablations.ARM_BY_ID["D"]).build(
        _prompt(single)
    )
    assert single_context.expert_mode == "single_agent"
    assert single_context.expert_calls == 0

    multifile = _task("multi_file_feature")
    multi_context = ablations.ArmContextBuilder(multifile, ablations.ARM_BY_ID["D"]).build(
        _prompt(multifile)
    )
    assert multi_context.expert_mode == "experts"
    assert "repo" in multi_context.expert_roles
    assert multi_context.payload["expert_advisory"] is not None
    advisory = multi_context.payload["expert_advisory"]
    assert advisory["advisories"][0]["read_only"] is True
    assert advisory["advisories"][0]["may_write"] is False
    assert advisory["write_access"] is False
    assert advisory["main_agent_decides"] is True
    assert multi_context.expert_calls >= 1

    for arm in ("A", "B", "C"):
        context = ablations.ArmContextBuilder(multifile, ablations.ARM_BY_ID[arm]).build(
            _prompt(multifile)
        )
        assert context.expert_mode == "disabled"
        assert context.expert_calls == 0
        assert context.payload["expert_advisory"] is None


def test_bootstrap_is_deterministic_and_bounded() -> None:
    values = [0.1, -0.2, 0.4, 0.0]
    first = ablations.paired_bootstrap(values)
    second = ablations.paired_bootstrap(values)
    assert first == second
    assert first["n"] == 4
    assert first["exploratory"] is True
    assert first["low"] <= first["mean"] <= first["high"]
    assert ablations.paired_bootstrap([])["note"] == "no paired observation"


def test_summary_reports_paired_intervals_and_gates() -> None:
    records: list[dict[str, Any]] = []
    for task in _dev():
        for index, arm in enumerate(ablations.ARMS):
            records.append(
                {
                    "schema": ablations.SCHEMA,
                    "mode": "fixture",
                    "arm": arm.id,
                    "arm_name": arm.name,
                    "task_id": task.task_id,
                    "kind": task.kind,
                    "status": grader.PASS,
                    "success": True,
                    "public_passed": True,
                    "hidden_passed": True,
                    "regressions": 0,
                    "context_tokens": 100 + index,
                    "model_calls": 1,
                    "cost_micros": 0,
                    "wall_clock_ms": 10.0,
                    "context": {
                        "context_loss_rate": 0.1 * index,
                        "duplicate_work_rate": 0.05 * index,
                        "expert_calls": index,
                    },
                    "failure_type": None,
                }
            )
    summary = ablations.summarize_arms(records, mode="fixture")
    assert summary["engineering_gate"] == "PASS"
    assert summary["live_agent_gate"] == "NOT_RUN"
    assert summary["executed"] == 16
    assert summary["paired_vs_A"]["B-A"]["success"]["n"] == 4
    assert summary["paired_vs_A"]["D-A"]["context_tokens"]["mean"] == pytest.approx(3.0)
    assert summary["paired_vs_A"]["C-A"]["context_loss_rate"]["mean"] == pytest.approx(0.2)
    assert all(item["exploratory"] for item in summary["paired_vs_A"]["B-A"].values())


def test_all_failed_live_arms_do_not_crown_a_winner() -> None:
    records = []
    for arm in ablations.ARMS:
        records.append(
            {
                "schema": ablations.SCHEMA,
                "mode": "live",
                "arm": arm.id,
                "arm_name": arm.name,
                "task_id": "v35-known-bug-help-name-collision",
                "kind": "known_bug_fix",
                "status": "fail",
                "success": False,
                "public_passed": False,
                "hidden_passed": False,
                "regressions": 0,
                "context_tokens": 100,
                "model_calls": 3,
                "cost_micros": 100 + len(arm.id),
                "wall_clock_ms": 10.0,
                "context": {
                    "context_loss_rate": 0.0,
                    "duplicate_work_rate": 0.0,
                    "expert_calls": 0,
                },
                "failure_type": "fail:PATCH_TRUNCATED",
            }
        )
    summary = ablations.summarize_arms(records, mode="live")
    decision = ablations.decide(summary, evidence_dir=Path("/tmp/e02"), mode="live")
    assert decision["status"] == "INCONCLUSIVE"
    assert decision["winner"] is None
    assert decision["basis"] == "live_all_arms_failed"
    assert decision["arms"][0]["failure_types"] == {"fail:PATCH_TRUNCATED": 1}


def test_not_run_cells_never_count_as_executed() -> None:
    records = ablations.not_run_records(_dev(), "authorization absent", "live")
    summary = ablations.summarize_arms(records, mode="live")
    assert summary["executed"] == 0
    assert summary["live_agent_gate"] == "NOT_RUN"
    assert all(item["tasks"] == 0 for item in summary["arms"])
    decision = ablations.decide(summary, evidence_dir=Path("/tmp/e02"), mode="live")
    assert decision["status"] == "NOT_RUN"
    assert decision["winner"] is None


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"allow_paid": False}, "paid"),
        ({"actor": "someone-else"}, "actor"),
        ({"approved_by": "someone-else"}, "actor"),
        ({"expires_in": timedelta(seconds=-1)}, "expired"),
    ],
)
def test_authorization_rejects_unscoped_or_expired_grants(
    tmp_path: Path, override: dict[str, Any], message: str
) -> None:
    path = _auth_file(tmp_path, **override)
    with pytest.raises(runner.LiveNotAuthorized, match=message):
        live_provider.load_authorization(path, actor="loz888")


def test_authorization_requires_a_deepseek_endpoint(tmp_path: Path) -> None:
    path = _auth_file(tmp_path)
    payload = json.loads(path.read_text())
    payload["endpoint"] = "https://attacker.example/v1/chat/completions"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(runner.LiveNotAuthorized, match="host"):
        live_provider.load_authorization(path, actor="loz888")


def test_live_completion_enforces_call_cap_without_leaking_the_key(
    tmp_path: Path,
) -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, json=_completion_payload())

    completion = _completion(tmp_path, max_calls=1, handler=handler)
    reply = completion.complete(system="s", payload={"a": 1})
    assert reply["usage"]["prompt_tokens"] == 10
    assert completion.calls == 1
    with pytest.raises(Exception) as error:
        completion.complete(system="s", payload={"a": 2})
    assert getattr(error.value, "code", None) == "LIVE_CALL_BUDGET"
    assert len(calls) == 1
    assert "test-key-not-a-secret" not in json.dumps(
        {"usage": reply["usage"], "model": reply["model"]}
    )


def test_live_completion_stops_when_the_cost_cap_is_exceeded(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=_completion_payload(prompt_tokens=1000, completion_tokens=100)
        )

    completion = _completion(tmp_path, max_cost_micros=1, handler=handler)
    with pytest.raises(Exception) as error:
        completion.complete(system="s", payload={})
    assert getattr(error.value, "code", None) == "LIVE_COST_BUDGET"
    assert completion.cost_micros > 0


def test_unknown_paid_outcome_is_never_retried(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    completion = _completion(tmp_path, handler=handler)
    with pytest.raises(Exception) as error:
        completion.complete(system="s", payload={})
    assert getattr(error.value, "code", None) == "USAGE_UNKNOWN"
    assert completion.calls == 1


def test_report_is_immutable_and_fixture_never_claims_live_gate(
    tmp_path: Path,
) -> None:
    records = ablations.not_run_records(_dev(), "fixture placeholder", "fixture")
    out = tmp_path / "report"
    summary = ablations.write_report(records, out_dir=out, mode="fixture")
    assert summary["live_agent_gate"] == "NOT_RUN"
    assert (out / "results.json").is_file()
    assert (out / "summary.json").is_file()
    assert (out / "decision.json").is_file()
    with pytest.raises(FileExistsError):
        ablations.write_report(records, out_dir=out, mode="fixture")


def test_fixture_ablation_runs_all_four_arms_on_the_real_sandbox(
    tmp_path: Path,
) -> None:
    task = _task("known_bug_fix")
    records = [
        ablations.run_arm(
            task,
            arm,
            experiment=EXPERIMENT,
            run_dir=tmp_path / task.task_id / arm.id,
            mode="fixture",
        )
        for arm in ablations.ARMS
    ]
    assert [item["arm"] for item in records] == ["A", "B", "C", "D"]
    for record in records:
        assert record["status"] == grader.PASS
        assert record["success"] is True
        assert record["public_passed"] is True
        assert record["hidden_passed"] is True
        assert record["model_calls"] == 1
        assert record["context_tokens_source"] == "estimated_context"
    by_arm = {item["arm"]: item for item in records}
    assert by_arm["A"]["context"]["memory_used"] == 0
    assert by_arm["B"]["context"]["memory_used"] >= 1
    assert by_arm["A"]["context"]["strategy"] == "raw_all"
    assert by_arm["C"]["context"]["strategy"] == "compacted"
    assert by_arm["D"]["context"]["strategy"] == "compacted"
    assert by_arm["D"]["context"]["expert_mode"] == "single_agent"
    summary = ablations.summarize_arms(records, mode="fixture")
    assert summary["engineering_gate"] == "PASS"
    assert summary["live_agent_gate"] == "NOT_RUN"
