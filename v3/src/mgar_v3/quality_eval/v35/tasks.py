"""Pinned task definitions for the V3.5 end-to-end evaluation harness.

The dataset is deliberately honest about its shape:

* every task pins a real public repository commit and a byte-exact snapshot;
* public tests are visible to the provider, hidden verification tests never are;
* the single holdout task lives in a separate file that the development loader
  never reads;
* fixture execution is engineering evidence only and can never satisfy the
  live-agent gate.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

SCHEMA = "mgar-v35-eval-task-v1"
CONFIG_SCHEMA = "mgar-v35-eval-config-v1"
KINDS = ("known_bug_fix", "regression_investigation", "multi_file_feature")
DEV_FILE = "tasks.jsonl"
HOLDOUT_FILE = "holdout/tasks.jsonl"
_EXCLUDED_PARTS = frozenset({"__pycache__", ".git", ".mypy_cache", ".pytest_cache"})


class DatasetError(ValueError):
    """The evaluation dataset is internally inconsistent."""


@dataclass(frozen=True)
class Task:
    task_id: str
    kind: str
    language: str
    repository: str
    source_url: str
    base_commit: str
    gold_commit: str | None
    gold_note: str | None
    snapshot_dir: Path
    snapshot_digest: str
    prompt: str
    allowed_paths: tuple[str, ...]
    expected_changed_paths: tuple[str, ...]
    public_targets: tuple[str, ...]
    public_files: tuple[Path, ...]
    hidden_targets: tuple[str, ...]
    hidden_files: tuple[Path, ...]
    hidden_tests: tuple[str, ...]
    gold_files: Mapping[str, Path]
    reference_patch: Path | None
    first_patch: Mapping[str, Any] | None
    budget: Mapping[str, Any]
    success: Mapping[str, Any]
    adversarial: bool
    resume: bool
    holdout: bool

    @property
    def snapshot_name(self) -> str:
        return self.snapshot_dir.name

    def public_sources(self) -> dict[str, str]:
        return _read_pairs(self.public_targets, self.public_files)

    def hidden_sources(self) -> dict[str, str]:
        return _read_pairs(self.hidden_targets, self.hidden_files)

    def gold_sources(self) -> dict[str, str]:
        return {
            target: path.read_text(encoding="utf-8")
            for target, path in self.gold_files.items()
        }


def _read_pairs(targets: Iterable[str], files: Iterable[Path]) -> dict[str, str]:
    pairs = dict(zip(targets, files, strict=True))
    return {target: path.read_text(encoding="utf-8") for target, path in pairs.items()}


def snapshot_files(directory: Path) -> list[Path]:
    """Every source file of a pinned snapshot, excluding caches and Git data."""

    return sorted(
        path
        for path in directory.rglob("*")
        if path.is_file()
        and not (_EXCLUDED_PARTS & set(path.relative_to(directory).parts))
        and path.suffix not in {".pyc", ".pyo"}
    )


def snapshot_digest(directory: Path) -> str:
    """Digest every source file under a base snapshot (path + content hash)."""

    lines = []
    for path in snapshot_files(directory):
        relative = path.relative_to(directory).as_posix()
        lines.append(f"{relative}\0{hashlib.sha256(path.read_bytes()).hexdigest()}")
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()


def _resolve(experiment: Path, relative: str) -> Path:
    candidate = (experiment / relative).resolve()
    root = experiment.resolve().parent
    if not candidate.is_relative_to(root):
        raise DatasetError(f"dataset path escapes the experiments root: {relative}")
    return candidate


def _task_from_payload(experiment: Path, payload: Mapping[str, Any]) -> Task:
    if payload.get("schema") != SCHEMA:
        raise DatasetError(f"{payload.get('task_id')}: unknown task schema")
    return Task(
        task_id=payload["task_id"],
        kind=payload["kind"],
        language=payload["language"],
        repository=payload["repository"],
        source_url=payload["source_url"],
        base_commit=payload["base_commit"],
        gold_commit=payload.get("gold_commit"),
        gold_note=payload.get("gold_note"),
        snapshot_dir=_resolve(experiment, payload["snapshot_dir"]),
        snapshot_digest=payload["snapshot_digest"],
        prompt=payload["prompt"],
        allowed_paths=tuple(payload["allowed_paths"]),
        expected_changed_paths=tuple(payload["expected_changed_paths"]),
        public_targets=tuple(payload["public_targets"]),
        public_files=tuple(_resolve(experiment, item) for item in payload["public_files"]),
        hidden_targets=tuple(payload["hidden_targets"]),
        hidden_files=tuple(_resolve(experiment, item) for item in payload["hidden_files"]),
        hidden_tests=tuple(payload["hidden_tests"]),
        gold_files={
            target: _resolve(experiment, relative)
            for target, relative in payload["gold_files"].items()
        },
        reference_patch=(
            _resolve(experiment, payload["reference_patch"])
            if payload.get("reference_patch")
            else None
        ),
        first_patch=payload.get("first_patch"),
        budget=payload["budget"],
        success=payload["success"],
        adversarial=bool(payload.get("adversarial")),
        resume=bool(payload.get("resume")),
        holdout=bool(payload.get("holdout")),
    )


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise DatasetError(f"missing dataset file: {path}")
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def load_development(experiment: Path) -> list[Task]:
    """Development tasks only; the holdout file is never opened here."""

    return [
        _task_from_payload(experiment, payload)
        for payload in _load_jsonl(experiment / DEV_FILE)
    ]


def load_holdout(experiment: Path, *, required: bool = True) -> list[Task]:
    """Holdout tasks, loaded only by an explicit evaluation run."""

    path = experiment / HOLDOUT_FILE
    if not path.is_file():
        if required:
            raise DatasetError(f"missing holdout dataset: {path}")
        return []
    return [_task_from_payload(experiment, payload) for payload in _load_jsonl(path)]


def load_config(experiment: Path) -> dict[str, Any]:
    payload = json.loads((experiment / "config.json").read_text(encoding="utf-8"))
    if payload.get("schema") != CONFIG_SCHEMA:
        raise DatasetError("unknown evaluation config schema")
    return cast(dict[str, Any], payload)


def select_tasks(
    experiment: Path, *, suite: str = "dev"
) -> list[Task]:
    """``dev`` and ``holdout`` are separate suites, never mixed by accident."""

    if suite == "dev":
        return load_development(experiment)
    if suite == "holdout":
        return load_holdout(experiment)
    raise DatasetError(f"unknown suite: {suite}")


def validate_task(task: Task) -> list[str]:
    errors: list[str] = []
    if task.kind not in KINDS:
        errors.append(f"{task.task_id}: unknown kind {task.kind}")
    if len(task.base_commit) != 40 or any(c not in "0123456789abcdef" for c in task.base_commit):
        errors.append(f"{task.task_id}: base commit is not a full SHA-1")
    if not task.source_url.startswith("https://github.com/"):
        errors.append(f"{task.task_id}: source_url is not a public GitHub URL")
    if not task.snapshot_dir.is_dir():
        errors.append(f"{task.task_id}: missing snapshot {task.snapshot_dir}")
    elif snapshot_digest(task.snapshot_dir) != task.snapshot_digest:
        errors.append(f"{task.task_id}: snapshot digest mismatch")
    elif task.snapshot_dir.name != task.base_commit:
        errors.append(f"{task.task_id}: snapshot name does not pin the base commit")

    base_files = {
        path.relative_to(task.snapshot_dir).as_posix()
        for path in snapshot_files(task.snapshot_dir)
    } if task.snapshot_dir.is_dir() else set()
    for allowed in task.allowed_paths:
        if allowed not in base_files:
            errors.append(f"{task.task_id}: allowed path missing from snapshot: {allowed}")
    if not set(task.expected_changed_paths) <= set(task.allowed_paths):
        errors.append(f"{task.task_id}: expected changes exceed allowed paths")
    if len(set(task.public_targets)) != len(task.public_targets):
        errors.append(f"{task.task_id}: duplicate public target")
    if set(task.public_targets) & set(task.hidden_targets):
        errors.append(f"{task.task_id}: public and hidden targets overlap")
    for target in task.hidden_targets:
        if not target.startswith("verification/"):
            errors.append(f"{task.task_id}: hidden target outside verification/: {target}")
    for relative in (*task.public_files, *task.hidden_files, *task.gold_files.values()):
        if not relative.is_file():
            errors.append(f"{task.task_id}: missing dataset asset {relative}")
    for node in task.hidden_tests:
        file, _, name = node.partition("::")
        if not name or file not in task.hidden_targets:
            errors.append(f"{task.task_id}: malformed hidden test id {node}")
    for target, path in task.gold_files.items():
        if target not in task.expected_changed_paths:
            errors.append(f"{task.task_id}: gold change outside expected paths: {target}")
        if not path.is_file():
            continue
        if target not in base_files:
            errors.append(f"{task.task_id}: gold target missing from snapshot: {target}")
            continue
        base_text = (task.snapshot_dir / target).read_text(encoding="utf-8")
        if base_text == path.read_text(encoding="utf-8"):
            errors.append(f"{task.task_id}: gold file does not differ from the base")
    if task.kind == "multi_file_feature" and len(task.expected_changed_paths) < 2:
        errors.append(f"{task.task_id}: multi-file feature expects fewer than two files")
    if task.adversarial and not task.resume:
        errors.append(f"{task.task_id}: adversarial task must also be resumable")
    if task.adversarial and not task.first_patch:
        errors.append(f"{task.task_id}: adversarial task needs a first-patch scenario")
    if task.first_patch:
        replacements = task.first_patch.get("replacements")
        if not isinstance(replacements, list) or not replacements:
            errors.append(f"{task.task_id}: first_patch has no replacements")
        else:
            gold = task.gold_sources()
            for replacement in replacements:
                path = replacement.get("path")
                find = replacement.get("find")
                if path not in task.expected_changed_paths:
                    errors.append(f"{task.task_id}: first_patch path is out of scope: {path}")
                elif not isinstance(find, str) or find not in gold.get(path, ""):
                    errors.append(f"{task.task_id}: first_patch find text is absent: {path}")
    if not task.adversarial and task.first_patch:
        errors.append(f"{task.task_id}: first_patch is only meaningful for adversarial tasks")
    if task.success.get("forbid_publication") is not True:
        errors.append(f"{task.task_id}: publication must stay forbidden in evaluation")
    if task.budget.get("max_attempts", 0) < 2:
        errors.append(f"{task.task_id}: budget cannot prove retry behaviour")
    return errors


def validate_dataset(experiment: Path, *, include_holdout: bool = False) -> list[str]:
    errors: list[str] = []
    config = load_config(experiment)
    development = load_development(experiment)
    holdout = load_holdout(experiment, required=False)
    all_tasks = development + holdout
    ids = [task.task_id for task in all_tasks]
    if len(ids) != len(set(ids)):
        errors.append("duplicate task ids across development and holdout")
    if config.get("holdout", {}).get("task_count") != len(holdout):
        errors.append("config holdout count does not match the holdout dataset")
    if config.get("holdout", {}).get("executed_by_default") is not False:
        errors.append("holdout must not execute by default")
    if config.get("gates", {}).get("engineering_gate_implies_live_agent_gate") is not False:
        errors.append("engineering and live-agent gates must stay independent")
    for task in all_tasks:
        errors.extend(validate_task(task))
    if not any(task.kind == "known_bug_fix" for task in development):
        errors.append("development set must cover known bug fixes")
    if not any(task.kind == "regression_investigation" for task in development):
        errors.append("development set must cover regression investigations")
    if not any(task.kind == "multi_file_feature" for task in development):
        errors.append("development set must cover a multi-file feature")
    if not any(task.adversarial and task.resume for task in development):
        errors.append("development set must include an adversarial multi-round task")
    if not any(task.holdout for task in holdout):
        errors.append("holdout tasks must be marked as holdout")
    if any(task.holdout for task in development):
        errors.append("development tasks must not be marked as holdout")
    if include_holdout:
        dev_paths = {
            path.resolve()
            for task in development
            for path in (*task.public_files, *task.hidden_files, *task.gold_files.values())
        }
        for task in holdout:
            for path in (*task.public_files, *task.hidden_files, *task.gold_files.values()):
                if path.resolve() in dev_paths:
                    errors.append(f"holdout asset reused by development: {path}")
    return errors


def score_signature(results: Iterable[Mapping[str, Any]]) -> str:
    """Stable digest of the decision-relevant outcome, ignoring timings and ids."""

    signature = []
    for result in sorted(results, key=lambda item: str(item.get("task_id"))):
        fix = result.get("fix", {})
        signature.append(
            {
                "task_id": result.get("task_id"),
                "status": result.get("status"),
                "public": fix.get("public_passed"),
                "hidden": fix.get("hidden_passed"),
                "changed": sorted(fix.get("changed_files", [])),
                "unrelated": sorted(fix.get("unrelated_changes", [])),
                "out_of_scope": sorted(fix.get("out_of_scope", [])),
                "attempts": result.get("attempts"),
            }
        )
    payload = json.dumps(signature, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()
