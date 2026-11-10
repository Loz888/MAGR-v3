"""V3.5 E02: Memory / Compaction / Expert ablation on the E01 harness.

Four paired arms share one task, commit, model configuration, budget and
grader:

* **A** — no memory, raw history;
* **B** — verified memory only, raw history;
* **C** — memory plus the shared M02 compactor;
* **D** — memory, compaction and on-demand X01 experts.

The module owns the arm definition, the commit-scoped memory corpus, the
context each arm hands to the planner, per-arm metrics, paired bootstrap
intervals and the immutable report.  It never invents a live number: fixture
arms are engineering evidence, live arms need an explicit authorization and a
real provider, and a cell that cannot run is recorded as ``NOT_RUN``.

The ablation never rewrites E01/E02 historical results; a decision is a new
artifact that points at one evidence directory.
"""

from __future__ import annotations

import hashlib
import json
import random
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from mgar_v3.context.compactor import (
    CompactionSummary,
    Observation,
    PermissionSnapshot,
    compact_observations,
    estimate_tokens,
)
from mgar_v3.domain import Model, digest
from mgar_v3.quality_eval.v35 import runner
from mgar_v3.quality_eval.v35 import tasks as tasks_mod
from mgar_v3.quality_eval.v35.grader import PASS
from mgar_v3.quality_eval.v35.tasks import Task
from mgar_v3.repair.repair_agent import PatchPlan, RepairPrompt
from mgar_v3.routing.experts import (
    ExpertAdvisory,
    ExpertBudget,
    ExpertContext,
    ExpertCoordinator,
    ExpertOutcome,
    ExpertRole,
)

ArmId = Literal["A", "B", "C", "D"]
SCHEMA = "mgar-v35-e02-arm-result-v1"
REPORT_SCHEMA = "mgar-v35-e02-report-v1"
DECISION_SCHEMA = "mgar-v35-ablation-decision-v1"
BOOTSTRAP_SEED = 20261110
BOOTSTRAP_ITERATIONS = 2000
EVIDENCE_ROOT = Path(__file__).resolve().parents[4] / "evidence" / "v35" / "E02"


class ArmSpec(Model):
    """One ablation arm: the only difference between arms is this flag set."""

    id: ArmId
    name: str
    memory: bool
    compaction: bool
    experts: bool
    description: str


ARMS: tuple[ArmSpec, ...] = (
    ArmSpec(
        id="A",
        name="no_memory",
        memory=False,
        compaction=False,
        experts=False,
        description="No memory and no compaction; the planner sees raw recent history.",
    ),
    ArmSpec(
        id="B",
        name="memory",
        memory=True,
        compaction=False,
        experts=False,
        description="Verified commit-scoped memory added to the raw history.",
    ),
    ArmSpec(
        id="C",
        name="memory_compaction",
        memory=True,
        compaction=True,
        experts=False,
        description="Memory plus the shared M02 dedupe/compress/summary policy.",
    ),
    ArmSpec(
        id="D",
        name="memory_compaction_experts",
        memory=True,
        compaction=True,
        experts=True,
        description="Memory and compaction plus on-demand read-only X01 experts.",
    ),
)

ARM_BY_ID: dict[str, ArmSpec] = {arm.id: arm for arm in ARMS}


class MemoryEntry(Model):
    """One fixture memory fact bound to an exact task commit and evidence."""

    memory_id: str
    task_id: str
    commit: str
    subject_key: str
    summary: str
    evidence_id: str
    status: Literal["active", "superseded", "revoked"]
    verified: bool
    confidence: float = Field(ge=0.0, le=1.0)


# Verified, commit-scoped facts.  Each task also gets three decoys (stale
# commit, unverified and superseded) so filtering is observable in the arm
# metrics instead of being taken on trust.
_TASK_MEMORY: dict[str, tuple[tuple[str, str, float], ...]] = {
    "v35-known-bug-help-name-collision": (
        (
            "click.automatic-help.storage-name",
            "A command argument declared as 'help' collides with the automatic "
            "help option; give the automatic option a reserved storage name so "
            "the argument keeps its value.",
            0.93,
        ),
        (
            "click.help-option.keeps-working",
            "The reserved storage name must still register --help and render the "
            "help page for every command.",
            0.9,
        ),
    ),
    "v35-regression-short-help-abbreviation": (
        (
            "click.short-help.abbreviation-boundary",
            "Sentence detection in _make_default_short_help must not split after "
            "an abbreviation such as 'vs.' or 'e.g.'; only treat a dot as a "
            "boundary when the next word starts a sentence or the width is hit.",
            0.91,
        ),
    ),
    "v35-multifile-short-help-placeholder": (
        (
            "click.short-help.placeholder-source",
            "Command.get_short_help_str reads short_help_placeholder from "
            "context_settings and passes it to _make_default_short_help.",
            0.92,
        ),
        (
            "click.short-help.default-placeholder",
            "The default short-help placeholder is still '...' and the "
            "abbreviation-aware truncation behaviour is preserved.",
            0.89,
        ),
    ),
    "v35-adversarial-help-name-retry": (
        (
            "click.automatic-help.storage-name",
            "A command argument declared as 'help' collides with the automatic "
            "help option; give the automatic option a reserved storage name so "
            "the argument keeps its value.",
            0.93,
        ),
        (
            "click.warning-only-is-not-a-fix",
            "Adding a duplicate-name warning without changing the storage name "
            "leaves the argument value lost; the public test keeps failing.",
            0.85,
        ),
    ),
}

_DECOY_SUBJECT = "click.decoy.unverified-hint"


def _entry(
    task: Task,
    subject_key: str,
    summary: str,
    confidence: float,
    *,
    commit: str | None = None,
    status: Literal["active", "superseded", "revoked"] = "active",
    verified: bool = True,
) -> MemoryEntry:
    commit_value = commit or task.base_commit
    return MemoryEntry(
        memory_id=digest({"task": task.task_id, "subject": subject_key}),
        task_id=task.task_id,
        commit=commit_value,
        subject_key=subject_key,
        summary=summary,
        evidence_id=digest(
            {
                "task": task.task_id,
                "subject": subject_key,
                "commit": commit_value,
                "summary": summary,
            }
        ),
        status=status,
        verified=verified,
        confidence=confidence,
    )


def memory_corpus(selected: Sequence[Task] | None = None) -> tuple[MemoryEntry, ...]:
    """Build the fixture corpus, including three decoys per task."""

    dataset = list(selected) if selected is not None else dev_tasks()
    entries: list[MemoryEntry] = []
    for task in dataset:
        for subject, summary, confidence in _TASK_MEMORY.get(task.task_id, ()):
            entries.append(_entry(task, subject, summary, confidence))
        entries.append(
            _entry(
                task,
                _DECOY_SUBJECT,
                "Unverified hint from an earlier run; it must not enter a prompt.",
                0.4,
                verified=False,
            )
        )
        entries.append(
            _entry(
                task,
                "click.decoy.stale-commit",
                "Fact recorded at another commit; it must be downweighted.",
                0.7,
                commit="f" * 40,
            )
        )
        entries.append(
            _entry(
                task,
                "click.decoy.superseded",
                "Superseded fact; only the active version may be used.",
                0.6,
                status="superseded",
            )
        )
    return tuple(entries)


def eligible_memories(corpus: Sequence[MemoryEntry], task: Task) -> list[MemoryEntry]:
    """Only active, verified facts recorded at the exact task commit survive."""

    return [
        item
        for item in corpus
        if item.task_id == task.task_id
        and item.commit == task.base_commit
        and item.status == "active"
        and item.verified
    ]


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def build_observations(task: Task, prompt: RepairPrompt) -> list[Observation]:
    """Turn the visible attempt state into the history an agent would hold."""

    observations: list[Observation] = [
        Observation(
            tool="test_run",
            reference="baseline",
            content="baseline public tests failed: " + ", ".join(task.public_targets),
            error_code="TEST_FAILED",
            evidence_id=digest({"task": task.task_id, "baseline": list(task.public_targets)}),
            source_version=task.base_commit,
        )
    ]
    for path in prompt.allowed_paths:
        content = str(prompt.files.get(path, ""))
        observations.append(
            Observation(
                tool="file_read",
                reference=path,
                content=content[:600],
                evidence_id=digest({"task": task.task_id, "path": path, "sha": _sha(content)}),
                source_version=task.base_commit,
            )
        )
    for item in prompt.feedback:
        observations.append(
            Observation(
                tool="patch_apply" if item.stage == "patch" else "test_run",
                reference=item.stage,
                content=f"{item.code}: {item.message}",
                error_code=item.code,
                evidence_id=digest(
                    {
                        "task": task.task_id,
                        "stage": item.stage,
                        "code": item.code,
                        "message": item.message,
                    }
                ),
                source_version=task.base_commit,
            )
        )
        for name in item.failed_tests[:8]:
            observations.append(
                Observation(
                    tool="test_run",
                    reference=name,
                    content=f"failed test: {name}",
                    error_code="TEST_FAILED",
                    evidence_id=digest({"task": task.task_id, "failed": name}),
                    source_version=task.base_commit,
                )
            )
    return observations


@dataclass(frozen=True)
class ArmContext:
    """The exact arm-specific section attached to one planner call."""

    payload: dict[str, Any]
    strategy: str
    memory_considered: int
    memory_used: int
    memory_filtered: int
    observations_total: int
    observations_kept: int
    observations_deduplicated: int
    observations_compressed: int
    observations_omitted: int
    estimated_tokens: int
    expert_mode: str
    expert_roles: tuple[str, ...]
    expert_calls: int
    expert_duplicates: int
    expert_degraded: bool

    @property
    def loss_rate(self) -> float:
        if not self.observations_total:
            return 0.0
        return self.observations_omitted / self.observations_total

    @property
    def duplicate_work_rate(self) -> float:
        denominator = self.observations_total + len(self.expert_roles)
        if not denominator:
            return 0.0
        return (self.observations_deduplicated + self.expert_duplicates) / denominator

    def metrics(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "memory_considered": self.memory_considered,
            "memory_used": self.memory_used,
            "memory_filtered": self.memory_filtered,
            "observations_total": self.observations_total,
            "observations_kept": self.observations_kept,
            "observations_deduplicated": self.observations_deduplicated,
            "observations_compressed": self.observations_compressed,
            "observations_omitted": self.observations_omitted,
            "context_tokens_estimated": self.estimated_tokens,
            "context_loss_rate": round(self.loss_rate, 6),
            "duplicate_work_rate": round(self.duplicate_work_rate, 6),
            "expert_mode": self.expert_mode,
            "expert_roles": list(self.expert_roles),
            "expert_calls": self.expert_calls,
            "expert_duplicates": self.expert_duplicates,
            "expert_degraded": self.expert_degraded,
        }


class CorpusExpertProvider:
    """Fixture expert that answers from the verified corpus, read-only."""

    def __init__(self, role: ExpertRole, entries: Sequence[MemoryEntry]) -> None:
        self.role = role
        self.entries = list(entries)
        self.calls = 0

    def investigate(
        self, role: ExpertRole, context: ExpertContext, budget_seconds: float
    ) -> ExpertAdvisory:
        self.calls += 1
        summary = " ".join(item.summary for item in self.entries)[:400]
        evidence = [
            item.evidence_id for item in self.entries if item.evidence_id in context.evidence_ids
        ][:8]
        return ExpertAdvisory(
            role=self.role,
            status="succeeded" if summary else "empty",
            summary=summary,
            facts=[item.subject_key for item in self.entries][:8],
            verification_suggestions=["run the public tests before proposing a patch"],
            evidence_ids=evidence,
            input_revision=context.input_revision,
            cost_micros=0,
        )


class ArmContextBuilder:
    """Assemble one arm's memory, history and optional expert advice."""

    def __init__(
        self,
        task: Task,
        arm: ArmSpec,
        *,
        corpus: Sequence[MemoryEntry] | None = None,
        token_budget: int = 160,
        recent_window: int = 2,
        observation_chars: int = 600,
    ) -> None:
        self.task = task
        self.arm = arm
        self.corpus = tuple(corpus) if corpus is not None else memory_corpus()
        self.token_budget = token_budget
        self.recent_window = recent_window
        self.observation_chars = observation_chars
        self.permissions = PermissionSnapshot(
            project_id="v35-e02",
            repository=task.repository,
            commit=task.base_commit,
            actor="v35-e02-ablations",
            read_only=True,
            allowed_tools=["file_read"],
            budgets={"model_calls": int(task.budget["max_model_calls"])},
        )
        self._expert_provider: CorpusExpertProvider | None = None
        self._coordinator: ExpertCoordinator | None = None
        if arm.experts:
            eligible = eligible_memories(self.corpus, task)
            self._expert_provider = CorpusExpertProvider("repo", eligible)
            self._coordinator = ExpertCoordinator(
                {"repo": self._expert_provider}, budget=ExpertBudget()
            )

    def _memory(self) -> tuple[list[MemoryEntry], int]:
        entries = [item for item in self.corpus if item.task_id == self.task.task_id]
        eligible = eligible_memories(self.corpus, self.task)
        return eligible, len(entries)

    def _history(
        self, observations: Sequence[Observation], prompt: RepairPrompt
    ) -> tuple[list[Observation], CompactionSummary | None, str, int, int, int]:
        if not self.arm.compaction:
            return list(observations), None, "raw_all", 0, 0, 0
        result = compact_observations(
            observations,
            token_budget=self.token_budget,
            recent_window=self.recent_window,
            counter=estimate_tokens,
            failed_attempts=[f"{item.stage}:{item.code}" for item in prompt.feedback],
            permissions=self.permissions,
            context_revision=digest(
                {
                    "task": self.task.task_id,
                    "attempt": prompt.attempt,
                    "repo": self.task.repository,
                    "commit": self.task.base_commit,
                }
            ),
        )
        return (
            result.observations,
            result.summary,
            "compacted",
            result.summary.observations_deduplicated,
            result.summary.observations_compressed,
            result.summary.observations_omitted,
        )

    def _consult(
        self, prompt: RepairPrompt, entries: Sequence[MemoryEntry], evidence_ids: Sequence[str]
    ) -> ExpertOutcome | None:
        if self._coordinator is None:
            return None
        changed = sorted(set(self.task.expected_changed_paths))
        failed_tests = [name for item in prompt.feedback for name in item.failed_tests]
        context = ExpertContext(
            task=self.task.prompt,
            question=prompt.objective,
            changed_files=changed,
            failed_tests=failed_tests,
            failed_attempts=len(prompt.feedback),
            evidence_ids=sorted({*evidence_ids, *(item.evidence_id for item in entries)}),
            input_revision=prompt.attempt,
        )
        return self._coordinator.consult(context)

    def build(self, prompt: RepairPrompt) -> ArmContext:
        entries, considered = self._memory()
        notes = entries if self.arm.memory else []
        observations = build_observations(self.task, prompt)
        observations = [
            item.model_copy(update={"content": item.content[: self.observation_chars]})
            for item in observations
        ]
        (
            history,
            summary,
            strategy,
            deduplicated,
            compressed,
            omitted,
        ) = self._history(observations, prompt)
        evidence_ids = sorted({item.evidence_id for item in observations if item.evidence_id})
        outcome = self._consult(prompt, notes, evidence_ids)
        expert_mode = outcome.decision.mode if outcome is not None else "disabled"
        roles = tuple(outcome.decision.roles) if outcome is not None else ()
        expert_calls = outcome.expert_calls if outcome is not None else 0
        expert_duplicates = len(outcome.duplicates_skipped) if outcome is not None else 0
        expert_degraded = outcome.degraded if outcome is not None else False
        payload: dict[str, Any] = {
            "arm": {
                "id": self.arm.id,
                "name": self.arm.name,
                "memory": self.arm.memory,
                "compaction": self.arm.compaction,
                "experts": self.arm.experts,
            },
            "memory_hints": [
                {
                    "subject_key": item.subject_key,
                    "summary": item.summary,
                    "evidence_id": item.evidence_id,
                    "confidence": item.confidence,
                }
                for item in notes
            ],
            "history": {
                "strategy": strategy,
                "observations": [
                    {
                        "tool": item.tool,
                        "reference": item.reference,
                        "content": item.content,
                        "evidence_id": item.evidence_id,
                        "error_code": item.error_code,
                    }
                    for item in history
                ],
                "summary": (summary.model_dump(mode="json") if summary is not None else None),
            },
            "expert_advisory": (
                outcome.to_payload()
                if outcome is not None and outcome.decision.mode == "experts"
                else None
            ),
        }
        estimated = estimate_tokens(payload)
        return ArmContext(
            payload=payload,
            strategy=strategy,
            memory_considered=considered,
            memory_used=len(notes),
            memory_filtered=max(0, considered - len(entries)),
            observations_total=len(observations),
            observations_kept=len(history),
            observations_deduplicated=deduplicated,
            observations_compressed=compressed,
            observations_omitted=omitted,
            estimated_tokens=estimated,
            expert_mode=expert_mode,
            expert_roles=roles,
            expert_calls=expert_calls,
            expert_duplicates=expert_duplicates,
            expert_degraded=expert_degraded,
        )


class AblationFixtureProvider:
    """Engineering-only provider: real context policy, scripted patch plan."""

    def __init__(
        self, plans: Sequence[PatchPlan], *, builder: ArmContextBuilder, label: str
    ) -> None:
        if not plans:
            raise ValueError("a fixture ablation provider needs at least one plan")
        self.plans = list(plans)
        self.builder = builder
        self.label = label
        self._contexts: list[ArmContext] = []

    @property
    def contexts(self) -> tuple[ArmContext, ...]:
        return tuple(self._contexts)

    def propose(self, prompt: RepairPrompt) -> PatchPlan:
        self._contexts.append(self.builder.build(prompt))
        plan = self.plans[min(len(self._contexts) - 1, len(self.plans) - 1)]
        return plan.model_copy(deep=True)

    def usage(self) -> dict[str, Any]:
        return {
            "model_calls": len(self._contexts),
            "prompt_tokens": sum(item.estimated_tokens for item in self._contexts),
            "completion_tokens": 0,
            "cost_micros": 0,
            "usage_source": "fixture_estimated_context",
        }


def dev_tasks(experiment: Path = runner.EXPERIMENT) -> list[Task]:
    return tasks_mod.load_development(experiment)


def _aggregate_contexts(contexts: Sequence[ArmContext]) -> dict[str, Any]:
    total = sum(item.observations_total for item in contexts)
    omitted = sum(item.observations_omitted for item in contexts)
    deduplicated = sum(item.observations_deduplicated for item in contexts)
    roles = sum(len(item.expert_roles) for item in contexts)
    duplicates = sum(item.expert_duplicates for item in contexts)
    return {
        "calls": len(contexts),
        "strategy": contexts[-1].strategy if contexts else "none",
        "context_tokens_estimated": sum(item.estimated_tokens for item in contexts),
        "context_loss_rate": round(omitted / total, 6) if total else 0.0,
        "duplicate_work_rate": (
            round((deduplicated + duplicates) / (total + roles), 6) if total + roles else 0.0
        ),
        "observations_total": total,
        "observations_deduplicated": deduplicated,
        "observations_omitted": omitted,
        "expert_calls": sum(item.expert_calls for item in contexts),
        "expert_roles": sorted({role for item in contexts for role in item.expert_roles}),
        "expert_mode": (contexts[-1].expert_mode if contexts else "disabled"),
        "memory_used": sum(item.memory_used for item in contexts),
        "memory_filtered": (contexts[-1].memory_filtered if contexts else 0),
    }


def run_arm(
    task: Task,
    arm: ArmSpec,
    *,
    experiment: Path,
    run_dir: Path,
    mode: str,
    live_factory: Callable[[Task, ArmSpec], Any] | None = None,
) -> dict[str, Any]:
    """Run one task under one arm through the real E01 orchestration chain."""

    if mode not in {"fixture", "live"}:
        raise ValueError(f"unknown ablation mode: {mode}")
    if mode == "fixture":
        snapshot = runner.snapshot_for(task)
        plans = runner.scripted_provider(task, snapshot).plans
        provider: Any = AblationFixtureProvider(
            plans,
            builder=ArmContextBuilder(task, arm),
            label="ablation_fixture",
        )
    else:
        if live_factory is None:
            raise runner.LiveNotAuthorized(
                "live ablation requires an authorization file and a real provider"
            )
        provider = live_factory(task, arm)
    started = time.monotonic()
    result = runner.run_task(
        task,
        experiment=experiment,
        run_dir=run_dir,
        mode=mode,
        provider=provider,
    )
    wall_ms = (time.monotonic() - started) * 1000
    contexts = list(provider.contexts)
    context = _aggregate_contexts(contexts)
    usage = result.get("usage") or {}
    live_tokens = int(usage.get("prompt_tokens", 0))
    tokens_channel = "provider_usage" if mode == "live" and live_tokens > 0 else "estimated_context"
    fix = result.get("fix") or {}
    failure = result.get("failure") or {}
    regressions = len(fix.get("unrelated_changes") or []) + len(fix.get("out_of_scope") or [])
    return {
        "schema": SCHEMA,
        "mode": mode,
        "arm": arm.id,
        "arm_name": arm.name,
        "task_id": task.task_id,
        "kind": task.kind,
        "status": result.get("status"),
        "success": result.get("status") == PASS,
        "public_passed": bool(fix.get("public_passed")),
        "hidden_passed": bool(fix.get("hidden_passed")),
        "regressions": regressions,
        "attempts": int(result.get("attempts", 0)),
        "context_tokens": (
            live_tokens
            if tokens_channel == "provider_usage"
            else context["context_tokens_estimated"]
        ),
        "context_tokens_source": tokens_channel,
        "model_calls": int(usage.get("model_calls", context["calls"])),
        "cost_micros": int(usage.get("cost_micros", 0)),
        "wall_clock_ms": round(wall_ms, 3),
        "context": context,
        "failure_type": (
            None
            if result.get("status") == PASS
            else f"{failure.get('classification')}:{failure.get('code') or failure.get('reason')}"
        ),
        "run_dir": str(run_dir),
    }


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def paired_bootstrap(
    values: Sequence[float],
    *,
    iterations: int = BOOTSTRAP_ITERATIONS,
    seed: int = BOOTSTRAP_SEED,
) -> dict[str, Any]:
    """Exploratory paired bootstrap CI over per-task differences."""

    if not values:
        return {
            "n": 0,
            "mean": 0.0,
            "low": 0.0,
            "high": 0.0,
            "iterations": iterations,
            "seed": seed,
            "exploratory": True,
            "note": "no paired observation",
        }
    rng = random.Random(seed)
    count = len(values)
    means = [
        sum(values[rng.randrange(count)] for _ in range(count)) / count for _ in range(iterations)
    ]
    means.sort()
    low = means[int(0.025 * iterations)]
    high = means[min(iterations - 1, int(0.975 * iterations))]
    return {
        "n": count,
        "mean": round(_mean(values), 6),
        "low": round(low, 6),
        "high": round(high, 6),
        "iterations": iterations,
        "seed": seed,
        "exploratory": count < 10,
        "note": "paired bootstrap over the development tasks; n is small, treat as exploratory",
    }


def _arm_summary(records: Sequence[Mapping[str, Any]], arm: ArmSpec) -> dict[str, Any]:
    rows = [item for item in records if item["arm"] == arm.id]
    executed = [item for item in rows if item.get("status") not in {None, "NOT_RUN"}]
    passed = [item for item in executed if item.get("success")]
    failures = Counter(
        str(item.get("failure_type"))
        for item in rows
        if item.get("status") not in {None, "NOT_RUN"} and not item.get("success")
    )
    return {
        "arm": arm.id,
        "name": arm.name,
        "memory": arm.memory,
        "compaction": arm.compaction,
        "experts": arm.experts,
        "tasks": len(executed),
        "passed": len(passed),
        "success_rate": round(len(passed) / len(executed), 6) if executed else 0.0,
        "public_pass_rate": (
            round(sum(bool(item.get("public_passed")) for item in executed) / len(executed), 6)
            if executed
            else 0.0
        ),
        "hidden_pass_rate": (
            round(sum(bool(item.get("hidden_passed")) for item in executed) / len(executed), 6)
            if executed
            else 0.0
        ),
        "regressions": sum(int(item.get("regressions", 0)) for item in executed),
        "avg_context_tokens": round(_mean([float(item["context_tokens"]) for item in executed]), 3),
        "total_context_tokens": sum(int(item["context_tokens"]) for item in executed),
        "avg_model_calls": round(_mean([float(item["model_calls"]) for item in executed]), 3),
        "total_model_calls": sum(int(item["model_calls"]) for item in executed),
        "avg_cost_micros": round(_mean([float(item["cost_micros"]) for item in executed]), 3),
        "total_cost_micros": sum(int(item["cost_micros"]) for item in executed),
        "avg_wall_clock_ms": round(_mean([float(item["wall_clock_ms"]) for item in executed]), 3),
        "avg_context_loss_rate": round(
            _mean(
                [
                    float((item.get("context") or {}).get("context_loss_rate", 0.0))
                    for item in executed
                ]
            ),
            6,
        ),
        "avg_duplicate_work_rate": round(
            _mean(
                [
                    float((item.get("context") or {}).get("duplicate_work_rate", 0.0))
                    for item in executed
                ]
            ),
            6,
        ),
        "expert_calls": sum(
            int((item.get("context") or {}).get("expert_calls", 0)) for item in executed
        ),
        "failure_types": dict(sorted(failures.items())),
        "status": "PASS"
        if executed and len(passed) == len(executed)
        else ("FAIL" if executed else "NOT_RUN"),
    }


_PAIR_METRICS = (
    "success",
    "context_tokens",
    "model_calls",
    "context_loss_rate",
    "duplicate_work_rate",
)


def summarize_arms(records: Sequence[Mapping[str, Any]], *, mode: str) -> dict[str, Any]:
    """Per-arm metrics plus paired differences against arm A."""

    arms = [_arm_summary(records, arm) for arm in ARMS]
    by_arm_task = {
        (item["arm"], item["task_id"]): item for item in records if item.get("status") != "NOT_RUN"
    }
    baseline = "A"
    paired: dict[str, dict[str, Any]] = {}
    for arm in ARMS[1:]:
        entry: dict[str, Any] = {}
        for key in _PAIR_METRICS:
            values: list[float] = []
            for task_id in sorted({str(item["task_id"]) for item in records}):
                left = by_arm_task.get((arm.id, task_id))
                right = by_arm_task.get((baseline, task_id))
                if left is None or right is None:
                    continue
                left_value = (
                    float(bool(left.get("success")))
                    if key == "success"
                    else float((left.get("context") or {}).get("context_loss_rate", 0.0))
                    if key == "context_loss_rate"
                    else float((left.get("context") or {}).get("duplicate_work_rate", 0.0))
                    if key == "duplicate_work_rate"
                    else float(left.get(key, 0))
                )
                right_value = (
                    float(bool(right.get("success")))
                    if key == "success"
                    else float((right.get("context") or {}).get("context_loss_rate", 0.0))
                    if key == "context_loss_rate"
                    else float((right.get("context") or {}).get("duplicate_work_rate", 0.0))
                    if key == "duplicate_work_rate"
                    else float(right.get(key, 0))
                )
                values.append(left_value - right_value)
            entry[key] = paired_bootstrap(values)
        paired[f"{arm.id}-{baseline}"] = entry
    executed = sum(1 for item in records if item.get("status") != "NOT_RUN")
    return {
        "schema": REPORT_SCHEMA,
        "mode": mode,
        "arms": arms,
        "paired_vs_A": paired,
        "cells": len(records),
        "executed": executed,
        "not_run": len(records) - executed,
        "engineering_gate": (
            "PASS"
            if mode == "fixture"
            and executed == len(records)
            and all(item.get("success") for item in records)
            else "FAIL"
            if mode == "fixture" and executed == len(records)
            else "NOT_RUN"
            if mode == "fixture"
            else "NOT_APPLICABLE"
        ),
        "live_agent_gate": (
            "NOT_RUN"
            if mode == "fixture"
            else "PASS"
            if executed and all(item.get("success") for item in records)
            else "FAIL"
            if executed
            else "NOT_RUN"
        ),
        "statistics_note": (
            "paired bootstrap 95% CI with seed "
            f"{BOOTSTRAP_SEED}; the development set has four tasks, so every "
            "interval is exploratory and never a significance claim"
        ),
    }


def decide(summary: Mapping[str, Any], *, evidence_dir: Path, mode: str) -> dict[str, Any]:
    """Write a decision artifact; it never rewrites historical results."""

    arms = {str(item["arm"]): item for item in summary.get("arms", [])}
    executed = int(summary.get("executed", 0))
    generated = datetime.now(UTC).isoformat()
    if mode != "live" or executed == 0:
        return {
            "schema": DECISION_SCHEMA,
            "status": "NOT_RUN",
            "mode": mode,
            "winner": None,
            "basis": "live_ablation_not_executed",
            "conclusion": (
                "Fixture arms exercise the real context policy but use a scripted "
                "patch provider, so they cannot prove a success benefit. No live "
                "arm ran; the V3.5 configuration keeps its current defaults and "
                "this decision makes no capability claim."
            ),
            "arms": [
                {
                    "arm": key,
                    "success_rate": value["success_rate"],
                    "avg_context_tokens": value["avg_context_tokens"],
                    "avg_model_calls": value["avg_model_calls"],
                }
                for key, value in sorted(arms.items())
            ],
            "evidence": str(evidence_dir),
            "generated_at": generated,
            "history_preserved": True,
        }
    best_success = max(float(item["success_rate"]) for item in arms.values())
    if best_success == 0.0:
        return {
            "schema": DECISION_SCHEMA,
            "status": "INCONCLUSIVE",
            "mode": mode,
            "winner": None,
            "basis": "live_all_arms_failed",
            "conclusion": (
                "Every executed live arm failed, so no arm demonstrates a "
                "capability benefit and no configuration change is justified. "
                "The dominant failure is recorded per arm; a failed arm that "
                "spent marginally fewer tokens is not a winner."
            ),
            "arms": [
                {
                    "arm": key,
                    "success_rate": value["success_rate"],
                    "avg_context_tokens": value["avg_context_tokens"],
                    "total_cost_micros": value["total_cost_micros"],
                    "failure_types": value["failure_types"],
                }
                for key, value in sorted(arms.items())
            ],
            "evidence": str(evidence_dir),
            "generated_at": generated,
            "history_preserved": True,
        }
    ranked = sorted(
        arms.items(),
        key=lambda pair: (
            -float(pair[1]["success_rate"]),
            float(pair[1]["total_cost_micros"]),
            float(pair[1]["avg_context_tokens"]),
        ),
    )
    winner, best = ranked[0]
    tied = [
        key
        for key, value in arms.items()
        if float(value["success_rate"]) == float(best["success_rate"])
        and int(value["total_cost_micros"]) == int(best["total_cost_micros"])
    ]
    if len(tied) > 1:
        return {
            "schema": DECISION_SCHEMA,
            "status": "INCONCLUSIVE",
            "mode": mode,
            "winner": None,
            "basis": "live_success_then_cost_tie",
            "tied_arms": sorted(tied),
            "conclusion": (
                "Executed live arms are indistinguishable on success and cost; "
                "no configuration change is justified by this ablation."
            ),
            "arms": [
                {
                    "arm": key,
                    "success_rate": value["success_rate"],
                    "avg_context_tokens": value["avg_context_tokens"],
                    "total_cost_micros": value["total_cost_micros"],
                }
                for key, value in sorted(arms.items())
            ],
            "evidence": str(evidence_dir),
            "generated_at": generated,
            "history_preserved": True,
        }
    return {
        "schema": DECISION_SCHEMA,
        "status": "PASS",
        "mode": mode,
        "winner": winner,
        "basis": "live_success_then_cost_then_context",
        "conclusion": (
            f"Arm {winner} leads executed live arms on success, then cost and "
            "context size. The decision only records a measured preference and "
            "does not retcon any historical E01 or E02 result."
        ),
        "arms": [
            {
                "arm": key,
                "success_rate": value["success_rate"],
                "avg_context_tokens": value["avg_context_tokens"],
                "total_cost_micros": value["total_cost_micros"],
            }
            for key, value in sorted(arms.items())
        ],
        "evidence": str(evidence_dir),
        "generated_at": generated,
        "history_preserved": True,
    }


def not_run_records(selected: Sequence[Task], reason: str, mode: str) -> list[dict[str, Any]]:
    return [
        {
            "schema": SCHEMA,
            "mode": mode,
            "arm": arm.id,
            "arm_name": arm.name,
            "task_id": task.task_id,
            "kind": task.kind,
            "status": "NOT_RUN",
            "success": False,
            "reason": reason,
            "run_dir": None,
        }
        for task in selected
        for arm in ARMS
    ]


def write_report(
    records: Sequence[Mapping[str, Any]],
    *,
    out_dir: Path,
    mode: str,
) -> dict[str, Any]:
    """Write one immutable report directory and its decision artifact."""

    results_path = out_dir / "results.json"
    summary_path = out_dir / "summary.json"
    decision_path = out_dir / "decision.json"
    if results_path.exists() or summary_path.exists() or decision_path.exists():
        raise FileExistsError(f"report already exists: {out_dir}")
    out_dir.mkdir(parents=True, exist_ok=True)
    summary = summarize_arms(records, mode=mode)
    summary["suite"] = "dev"
    summary["generated_at"] = datetime.now(UTC).isoformat()
    decision = decide(summary, evidence_dir=out_dir, mode=mode)
    (out_dir / "results.json").write_text(
        json.dumps(list(records), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (out_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (decision_path).write_text(
        json.dumps(decision, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return summary


def _select(experiment: Path, suite: str, selected_ids: Sequence[str]) -> list[Task]:
    tasks_ = tasks_mod.select_tasks(experiment, suite=suite)
    if selected_ids:
        wanted = set(selected_ids)
        tasks_ = [item for item in tasks_ if item.task_id in wanted]
        missing = wanted - {item.task_id for item in tasks_}
        if missing:
            raise SystemExit(f"unknown task ids: {sorted(missing)}")
    return tasks_


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point; ``scripts/run_eval.py --ablation`` dispatches here."""

    import argparse
    import sys

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", choices=("dev",), default="dev")
    parser.add_argument("--mode", choices=("fixture", "live"), default="fixture")
    parser.add_argument("--task", action="append", default=[])
    parser.add_argument("--experiment", type=Path, default=runner.EXPERIMENT)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument(
        "--arms",
        default="ABCD",
        help="arm subset, e.g. ABCD or D for a live probe",
    )
    parser.add_argument(
        "--authorization",
        type=Path,
        default=None,
        help="live only: authorization JSON; never a plan value",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    experiment = args.experiment.resolve()
    errors = tasks_mod.validate_dataset(experiment)
    if errors:
        for error in errors:
            print(f"dataset error: {error}", file=sys.stderr)
        return 2
    selected = _select(experiment, args.suite, args.task)
    if not selected:
        print("no tasks selected", file=sys.stderr)
        return 2
    unknown = sorted(set(args.arms) - set(ARM_BY_ID))
    if unknown:
        print(f"unknown arms: {unknown}", file=sys.stderr)
        return 2
    arms = [ARM_BY_ID[value] for value in args.arms]
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = (
        Path(args.out).expanduser().resolve()
        if args.out
        else EVIDENCE_ROOT / f"{stamp}-{args.suite}-{args.mode}"
    )
    if out.exists():
        print(f"report already exists: {out}", file=sys.stderr)
        return 2

    live_factory: Callable[[Task, ArmSpec], Any] | None = None
    if args.mode == "live":
        if args.authorization is None or not args.authorization.is_file():
            records_not_run = not_run_records(
                selected,
                "live authorization file is absent; no paid model call was made",
                "live",
            )
            summary = write_report(records_not_run, out_dir=out, mode="live")
            print(json.dumps({"live_agent_gate": summary["live_agent_gate"]}))
            return 0
        from mgar_v3.quality_eval.v35 import live_provider

        authorization = live_provider.load_authorization(
            args.authorization,
            actor=live_provider.actor_from_environment(),
        )
        live_factory = live_provider.make_provider_factory(
            authorization=authorization,
        )

    records: list[dict[str, Any]] = []
    for task in selected:
        for arm in arms:
            run_dir = out / "tasks" / task.task_id / arm.id
            records.append(
                run_arm(
                    task,
                    arm,
                    experiment=experiment,
                    run_dir=run_dir,
                    mode=args.mode,
                    live_factory=live_factory,
                )
            )
    summary = write_report(records, out_dir=out, mode=args.mode)
    print(
        json.dumps(
            {
                "mode": args.mode,
                "engineering_gate": summary["engineering_gate"],
                "live_agent_gate": summary["live_agent_gate"],
                "cells": summary["cells"],
                "executed": summary["executed"],
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
