"""V3.4 real repository evaluation helpers (E01).

This module is deliberately standard-library only. It validates the offline
E01 dataset (pinned public repositories, review/repair tasks, holdout split)
and computes the metrics that the experiment declares. Live model or GitHub
runs require explicit authorization and are therefore recorded as ``NOT_RUN``;
nothing in this module fabricates a capability result.

The harness self-test is a *conformance* check of the grading pipeline against
known answers. Its results are scope-tagged and never counted as model scores.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

TASK_SCHEMA = "mgar-v34-task-v1"
REPOSITORY_SCHEMA = "mgar-v34-repositories-v1"
DATASET_SCHEMA = "mgar-v34-dataset-v1"
SELFTEST_SCHEMA = "mgar-v34-harness-selftest-v1"
TASK_KINDS = ("review", "repair", "negative")
LANGUAGES = ("python", "go")
ARMS = ("A", "B", "C")
FINDING_CATEGORIES = (
    "singleton_identity_loss",
    "sentence_boundary_abbreviation",
    "parameter_name_collision",
    "sequence_envvar_formatting",
    "malformed_parameter_value",
    "empty_netrc_fallback",
    "unknown_flag_consumption",
    "state_mutation_on_error",
    "integer_width_mismatch",
    "missing_newline_output",
    "file_wrapper_detection",
    "ipv4_mask_validation",
    "shell_command_execution",
    "dynamic_code_execution",
    "unsafe_pointer_arithmetic",
)
_HEX40 = re.compile(r"^[0-9a-f]{40}$")
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_VERIFIER_CACHE: dict[str, Any] = {}


class EvaluationError(ValueError):
    """Raised when the E01 dataset or a grading input is inconsistent."""

    def __init__(self, problems: Sequence[str]):
        super().__init__("; ".join(problems))
        self.problems = list(problems)


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with Path(path).open("r", encoding="utf-8") as handle:
        for number, raw in enumerate(handle, start=1):
            if not raw.strip():
                continue
            try:
                value = json.loads(raw)
            except json.JSONDecodeError as error:
                raise EvaluationError([f"{path}:{number}: invalid JSON ({error})"]) from error
            if not isinstance(value, dict):
                raise EvaluationError([f"{path}:{number}: task must be a JSON object"])
            records.append(value)
    return records


def load_json(path: Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def load_tasks(
    experiment_dir: Path, manifest: Mapping[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    v34 = manifest.get("v34")
    if not isinstance(v34, Mapping):
        raise EvaluationError(["manifest has no v34 section"])
    tasks = load_jsonl(Path(experiment_dir) / str(v34["tasks_file"]))
    holdout = load_jsonl(Path(experiment_dir) / str(v34["holdout_file"]))
    return tasks, holdout


def task_by_id(tasks: Iterable[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    result: dict[str, Mapping[str, Any]] = {}
    for task in tasks:
        result[str(task["task_id"])] = task
    return result


def finding_key(finding: Mapping[str, Any]) -> tuple[str, str, str]:
    return (
        str(finding.get("file", "")),
        str(finding.get("symbol", "")),
        str(finding.get("category", "")),
    )


def _norm_chars(text: str) -> str:
    return "\n".join(line.rstrip() for line in text.replace("\r\n", "\n").split("\n")).strip("\n")


def _snapshot_file(experiment_dir: Path, task: Mapping[str, Any], file: str) -> Path:
    return Path(experiment_dir) / str(task["snapshot_dir"]) / file


def quote_at(
    experiment_dir: Path, task: Mapping[str, Any], finding: Mapping[str, Any]
) -> str:
    path = _snapshot_file(experiment_dir, task, str(finding["file"]))
    lines = path.read_text(encoding="utf-8").splitlines()
    start = int(finding["start_line"])
    end = int(finding["end_line"])
    if start < 1 or end < start or end > len(lines):
        raise EvaluationError(
            [f"{task['task_id']}: line range {start}-{end} outside {path} ({len(lines)} lines)"]
        )
    return "\n".join(lines[start - 1 : end])


def _check_quote(
    experiment_dir: Path, task: Mapping[str, Any], finding: Mapping[str, Any]
) -> list[str]:
    quote = quote_at(experiment_dir, task, finding)
    expected = str(finding.get("quote_sha256", ""))
    if sha256_text(quote) != expected:
        return [
            f"{task['task_id']}: quote hash mismatch at {finding['file']}:"
            f"{finding['start_line']}-{finding['end_line']}"
        ]
    return []


def _parse_patch_lines(patch: str) -> tuple[set[str], set[str]]:
    before: set[str] = set()
    added: set[str] = set()
    for line in patch.splitlines():
        if line.startswith(("+++", "---", "@@", "diff --git", "index ")):
            continue
        if line.startswith(("-", " ")):
            before.add(line[1:].strip())
        elif line.startswith("+"):
            added.add(line[1:].strip())
    return before, added


def _check_fix_evidence(
    experiment_dir: Path,
    task: Mapping[str, Any],
    finding: Mapping[str, Any],
    patch_text: str | None,
) -> list[str]:
    evidence = finding.get("fix_evidence")
    if not isinstance(evidence, Mapping):
        return [f"{task['task_id']}: gold finding has no fix_evidence"]
    if patch_text is None:
        return [f"{task['task_id']}: missing fix patch"]
    removed, added = _parse_patch_lines(patch_text)
    problems: list[str] = []
    for token in evidence.get("buggy_tokens", []):
        if str(token).strip() not in removed:
            problems.append(f"{task['task_id']}: buggy token absent from patch context: {token!r}")
    for token in evidence.get("fixed_tokens", []):
        if str(token).strip() not in added:
            problems.append(f"{task['task_id']}: fixed token absent from patch: {token!r}")
    return problems


def validate_repositories(
    experiment_dir: Path, repositories: Mapping[str, Any], *, check_files: bool
) -> list[str]:
    problems: list[str] = []
    if repositories.get("schema") != REPOSITORY_SCHEMA:
        problems.append(f"repositories schema must be {REPOSITORY_SCHEMA}")
    entries = repositories.get("repositories")
    if not isinstance(entries, list) or not entries:
        return problems + ["repositories must be a non-empty list"]
    seen_ids: set[str] = set()
    for entry in entries:
        if not isinstance(entry, Mapping):
            problems.append("repository entry must be an object")
            continue
        repo_id = str(entry.get("repo_id", ""))
        if not repo_id or repo_id in seen_ids:
            problems.append(f"duplicate or empty repository id: {repo_id!r}")
        seen_ids.add(repo_id)
        if not str(entry.get("url", "")).startswith("https://github.com/"):
            problems.append(f"{repo_id}: url must be a public GitHub URL")
        if entry.get("language") not in LANGUAGES:
            problems.append(f"{repo_id}: language must be one of {LANGUAGES}")
        if not entry.get("license"):
            problems.append(f"{repo_id}: missing license")
        snapshots = entry.get("snapshots")
        if not isinstance(snapshots, list) or not snapshots:
            problems.append(f"{repo_id}: snapshots must be a non-empty list")
            continue
        for snapshot in snapshots:
            if not isinstance(snapshot, Mapping):
                problems.append(f"{repo_id}: snapshot must be an object")
                continue
            commit = str(snapshot.get("commit", ""))
            if not _HEX40.match(commit):
                problems.append(f"{repo_id}: snapshot commit must be 40 hex chars")
            directory = Path(experiment_dir) / str(snapshot.get("dir", ""))
            files = snapshot.get("files")
            if not isinstance(files, Mapping) or not files:
                problems.append(f"{repo_id}/{commit}: snapshot files must be a non-empty object")
                continue
            if check_files:
                for relative, expected in files.items():
                    path = directory / str(relative)
                    if not path.is_file():
                        problems.append(f"{repo_id}/{commit}: missing snapshot file {path}")
                    elif sha256_file(path) != expected:
                        problems.append(f"{repo_id}/{commit}: snapshot hash mismatch for {path}")
    return problems


def _validate_finding(
    experiment_dir: Path,
    task: Mapping[str, Any],
    finding: Mapping[str, Any],
    *,
    check_files: bool,
) -> list[str]:
    problems: list[str] = []
    task_id = str(task.get("task_id", "?"))
    for name in ("finding_id", "file", "symbol", "category", "call_chain", "fix_evidence"):
        if name not in finding:
            problems.append(f"{task_id}: gold finding missing {name}")
    if problems:
        return problems
    if finding["category"] not in FINDING_CATEGORIES:
        problems.append(f"{task_id}: unknown finding category {finding['category']!r}")
    if not str(finding["file"]).startswith(tuple(str(p) for p in task.get("scope_paths", []))):
        problems.append(f"{task_id}: gold finding file is outside scope_paths")
    if not isinstance(finding["call_chain"], list) or len(finding["call_chain"]) < 2:
        problems.append(f"{task_id}: call_chain needs at least two symbols")
    if not _HEX40.match(str(task.get("commit", ""))):
        problems.append(f"{task_id}: task commit must be 40 hex chars")
    if check_files:
        try:
            problems.extend(_check_quote(experiment_dir, task, finding))
            patch_file = str(task.get("provenance", {}).get("fix_patch", ""))
            patch_text = (
                (Path(experiment_dir) / patch_file).read_text(encoding="utf-8")
                if patch_file
                else None
            )
            problems.extend(_check_fix_evidence(experiment_dir, task, finding, patch_text))
        except (OSError, EvaluationError) as error:
            problems.append(f"{task_id}: {error}")
    return problems


def validate_tasks(
    experiment_dir: Path,
    tasks: Sequence[Mapping[str, Any]],
    repositories: Mapping[str, Any],
    *,
    holdout: bool,
    check_files: bool,
) -> list[str]:
    problems: list[str] = []
    if not tasks:
        return ["task list is empty"]
    repo_languages = {
        str(entry.get("repo_id")): str(entry.get("language"))
        for entry in repositories.get("repositories", [])
        if isinstance(entry, Mapping)
    }
    seen: set[str] = set()
    for task in tasks:
        task_id = str(task.get("task_id", ""))
        if task.get("schema") != TASK_SCHEMA:
            problems.append(f"{task_id}: schema must be {TASK_SCHEMA}")
        if not task_id or task_id in seen:
            problems.append(f"duplicate or empty task_id: {task_id!r}")
            continue
        seen.add(task_id)
        if task.get("kind") not in TASK_KINDS:
            problems.append(f"{task_id}: kind must be one of {TASK_KINDS}")
        if task.get("holdout") is not holdout:
            problems.append(f"{task_id}: holdout flag must be {holdout}")
        if task.get("repo_id") not in repo_languages:
            problems.append(f"{task_id}: unknown repo_id {task.get('repo_id')!r}")
        elif task.get("language") != repo_languages[str(task["repo_id"])]:
            problems.append(f"{task_id}: language does not match repository")
        if not isinstance(task.get("prompt"), str) or not task["prompt"].strip():
            problems.append(f"{task_id}: prompt must be non-empty")
        budget = task.get("budget")
        grading = task.get("grading")
        if not isinstance(budget, Mapping) or int(budget.get("max_findings", 0)) < 1:
            problems.append(f"{task_id}: budget.max_findings must be >= 1")
        if not isinstance(grading, Mapping) or int(grading.get("k", 0)) < 1:
            problems.append(f"{task_id}: grading.k must be >= 1")
        scope = task.get("scope_paths")
        if not isinstance(scope, list) or not scope:
            problems.append(f"{task_id}: scope_paths must be non-empty")
        snapshot_dir = Path(experiment_dir) / str(task.get("snapshot_dir", ""))
        if check_files and not snapshot_dir.is_dir():
            problems.append(f"{task_id}: snapshot directory missing: {snapshot_dir}")
        gold = task.get("gold_findings")
        if not isinstance(gold, list):
            problems.append(f"{task_id}: gold_findings must be a list")
            continue
        if task.get("kind") == "negative":
            if gold:
                problems.append(f"{task_id}: negative task must have no gold findings")
            negative = task.get("negative")
            if not isinstance(negative, Mapping) or not negative.get("forbidden_patterns"):
                problems.append(f"{task_id}: negative task needs forbidden_patterns")
        elif not gold:
            problems.append(f"{task_id}: {task.get('kind')} task needs gold findings")
        for finding in gold:
            if isinstance(finding, Mapping):
                problems.extend(
                    _validate_finding(
                        experiment_dir, task, finding, check_files=check_files
                    )
                )
        repair = task.get("repair")
        if task.get("kind") == "repair":
            if not isinstance(repair, Mapping):
                problems.append(f"{task_id}: repair task needs repair block")
            else:
                for name in (
                    "buggy_snapshot_dir",
                    "fixed_snapshot_dir",
                    "patch_file",
                    "test_file",
                ):
                    if not repair.get(name):
                        problems.append(f"{task_id}: repair missing {name}")
                if check_files:
                    for name in ("buggy_snapshot_dir", "fixed_snapshot_dir"):
                        if not (Path(experiment_dir) / str(repair.get(name, ""))).is_dir():
                            problems.append(f"{task_id}: missing {name}")
                    for name in ("patch_file", "test_file"):
                        if not (Path(experiment_dir) / str(repair.get(name, ""))).is_file():
                            problems.append(f"{task_id}: missing {name}")
                    expected = repair.get("reference_patch_sha256")
                    path = Path(experiment_dir) / str(repair.get("patch_file", ""))
                    if expected and path.is_file() and sha256_file(path) != expected:
                        problems.append(f"{task_id}: reference patch hash mismatch")
    return problems


def validate_bundle(
    experiment_dir: Path, manifest: Mapping[str, Any], *, check_files: bool
) -> list[str]:
    v34 = manifest.get("v34")
    if not isinstance(v34, Mapping):
        return ["manifest has no v34 section"]
    experiment_dir = Path(experiment_dir)
    problems: list[str] = []
    tasks, holdout = load_tasks(experiment_dir, manifest)
    repositories_file = experiment_dir / str(v34.get("repositories_file", ""))
    if not repositories_file.is_file():
        return [f"repositories file missing: {repositories_file}"]
    repositories = load_json(repositories_file)
    problems.extend(
        validate_repositories(experiment_dir, repositories, check_files=check_files)
    )
    problems.extend(
        validate_tasks(
            experiment_dir, tasks, repositories, holdout=False, check_files=check_files
        )
    )
    problems.extend(
        validate_tasks(
            experiment_dir, holdout, repositories, holdout=True, check_files=check_files
        )
    )
    dev_ids = {str(task["task_id"]) for task in tasks}
    holdout_ids = {str(task["task_id"]) for task in holdout}
    if dev_ids & holdout_ids:
        problems.append(f"dev and holdout task ids overlap: {sorted(dev_ids & holdout_ids)}")
    declared = {str(item) for item in v34.get("holdout_ids", [])}
    if declared != holdout_ids:
        problems.append(
            f"v34.holdout_ids {sorted(declared)} do not match holdout file {sorted(holdout_ids)}"
        )
    if check_files:
        for key in ("tasks_file", "holdout_file", "repositories_file"):
            path = experiment_dir / str(v34.get(key, ""))
            expected = v34.get(f"{key}_sha256")
            if not path.is_file():
                problems.append(f"v34 {key} missing: {path}")
            elif expected and sha256_file(path) != expected:
                problems.append(f"v34 {key} hash mismatch: {path}")
    return problems


def _repo_lookup(repositories: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {
        str(entry["repo_id"]): entry
        for entry in repositories.get("repositories", [])
        if isinstance(entry, Mapping) and "repo_id" in entry
    }


def summarize_bundle(
    experiment_dir: Path,
    manifest: Mapping[str, Any],
    records: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    experiment_dir = Path(experiment_dir)
    tasks, holdout = load_tasks(experiment_dir, manifest)
    repositories = load_json(experiment_dir / str(manifest["v34"]["repositories_file"]))
    repos = _repo_lookup(repositories)
    selftest_path = experiment_dir / str(manifest["v34"].get("selftest_file", ""))
    selftest = load_json(selftest_path) if selftest_path.is_file() else None
    all_tasks = list(tasks) + list(holdout)
    kinds = {kind: sum(1 for task in all_tasks if task["kind"] == kind) for kind in TASK_KINDS}
    languages = {
        language: sum(1 for task in all_tasks if task["language"] == language)
        for language in LANGUAGES
    }
    arm_counts: dict[str, dict[str, int]] = {}
    for arm in ARMS:
        rows = [record for record in records if str(record["arm"]) == arm]
        arm_counts[arm] = {
            "records": len(rows),
            "not_run": sum(1 for row in rows if row["status"] == "NOT_RUN"),
            "pass": sum(1 for row in rows if row["status"] == "PASS"),
            "fail": sum(1 for row in rows if row["status"] == "FAIL"),
        }
    sources = [
        {
            "repo_id": repo_id,
            "url": entry.get("url"),
            "language": entry.get("language"),
            "license": entry.get("license"),
            "commits": sorted(
                {
                    str(snapshot.get("commit"))
                    for snapshot in entry.get("snapshots", [])
                    if isinstance(snapshot, Mapping)
                }
            ),
        }
        for repo_id, entry in sorted(repos.items())
    ]
    selftest_summary = None
    if isinstance(selftest, Mapping):
        counts = selftest.get("status_counts", {})
        selftest_summary = {
            "scope": selftest.get("scope"),
            "executed": sum(int(counts.get(key, 0)) for key in ("PASS", "FAIL")),
            "pass": int(counts.get("PASS", 0)),
            "fail": int(counts.get("FAIL", 0)),
        }
    return {
        "schema": "mgar-v34-summary-v1",
        "dataset": {
            "repositories": len(repos),
            "tasks": len(all_tasks),
            "dev_tasks": len(tasks),
            "holdout_tasks": len(holdout),
            "kinds": kinds,
            "languages": languages,
        },
        "repository_sources": sources,
        "holdout": {
            "ids": sorted(str(task["task_id"]) for task in holdout),
            "used_in_development": False,
        },
        "authorization": dict(manifest.get("authorization", {})),
        "live_arms": {
            "status": "NOT_RUN",
            "reason": str(manifest["v34"].get("not_run_reason", "")),
            "records": arm_counts,
        },
        "harness_selftest": selftest_summary,
        "metrics_not_run": [
            "finding_precision_at_k",
            "recall_at_budget",
            "unverified_false_positives",
            "source_citation_validity",
            "call_chain_correctness",
            "bug_reproduction_success",
            "patch_validation_success",
            "independent_test_pass",
            "regression_failures",
            "draft_pr_correctness",
            "memory_recall_at_k",
            "stale_rejection",
            "contamination_rate",
            "token_reduction",
            "repeated_tool_call_reduction",
            "correctness_with_memory",
            "correctness_without_memory",
            "retrieval_cost",
        ],
    }


def _valid_citation(
    experiment_dir: Path, task: Mapping[str, Any], finding: Mapping[str, Any]
) -> bool:
    try:
        expected = finding.get("quote_sha256")
        if expected:
            return sha256_text(quote_at(experiment_dir, task, finding)) == str(expected)
        path = _snapshot_file(experiment_dir, task, str(finding["file"]))
        lines = path.read_text(encoding="utf-8").splitlines()
        quote = str(finding.get("quote", ""))
        return bool(quote) and quote in "\n".join(lines)
    except (OSError, EvaluationError, KeyError):
        return False


def _chain_matches(candidate: Mapping[str, Any], gold: Mapping[str, Any]) -> bool:
    return [str(item) for item in candidate.get("call_chain", [])] == [
        str(item) for item in gold.get("call_chain", [])
    ]


def grade_review(
    task: Mapping[str, Any],
    answer: Mapping[str, Any],
    *,
    experiment_dir: Path,
) -> dict[str, Any]:
    """Grade one review/negative answer against gold findings."""
    findings = [item for item in answer.get("findings", []) if isinstance(item, Mapping)]
    gold = [item for item in task.get("gold_findings", []) if isinstance(item, Mapping)]
    gold_keys = {finding_key(item) for item in gold}
    k = int(task["grading"]["k"])
    budget = int(task["grading"]["recall_budget"])
    top_k = findings[:k]
    evaluated = findings[:budget]
    relevant_top = [item for item in top_k if finding_key(item) in gold_keys]
    matched_keys = {finding_key(item) for item in evaluated if finding_key(item) in gold_keys}
    citation_valid = [
        item for item in findings if _valid_citation(experiment_dir, task, item)
    ]
    unverified_fp = sum(
        1
        for item in findings
        if finding_key(item) not in gold_keys and not _valid_citation(experiment_dir, task, item)
    )
    matched = [item for item in evaluated if finding_key(item) in gold_keys]
    chain_ok = [
        item
        for item in matched
        if any(
            _chain_matches(item, gold_item)
            for gold_item in gold
            if finding_key(gold_item) == finding_key(item)
        )
    ]
    scanned = answer.get("coverage", {}).get("scanned_files", [])
    scannable = answer.get("coverage", {}).get("scannable_files", [])
    coverage = len(set(scanned) & set(scannable)) / len(scannable) if scannable else 0.0
    if not findings:
        precision = 1.0 if not gold else 0.0
    else:
        precision = len(relevant_top) / len(top_k)
    recall = len(matched_keys) / len(gold_keys) if gold_keys else 1.0
    total = len(findings)
    metrics = {
        "task_id": task["task_id"],
        "kind": task["kind"],
        "findings": total,
        "gold_findings": len(gold_keys),
        "precision_at_k": precision,
        "recall_at_budget": recall,
        "unverified_false_positives": unverified_fp,
        "false_positives": sum(1 for item in findings if finding_key(item) not in gold_keys),
        "source_citation_validity": len(citation_valid) / total if total else 1.0,
        "call_chain_correctness": len(chain_ok) / len(matched) if matched else 1.0,
        "scanned_files": len(set(scanned) & set(scannable)),
        "scannable_files": len(scannable),
        "scan_coverage": coverage,
        "within_budget": total <= budget,
    }
    metrics["passed"] = bool(
        recall == 1.0
        and unverified_fp == 0
        and metrics["source_citation_validity"] == 1.0
        and metrics["call_chain_correctness"] == 1.0
        and coverage == 1.0
        and metrics["within_budget"]
        and (precision == 1.0)
    )
    return metrics


def grade_repair(task: Mapping[str, Any], run: Mapping[str, Any]) -> dict[str, Any]:
    """Grade one repair run using the declared binary checks."""
    regression_failures = int(run.get("regression_failures", 0))
    flags = {
        "bug_reproduction_success": bool(run.get("bug_reproduction_success")),
        "patch_validation_success": bool(run.get("patch_validation_success")),
        "independent_test_pass": bool(run.get("independent_test_pass")),
        "draft_pr_correctness": bool(run.get("draft_pr_correctness")),
    }
    metrics = {
        "task_id": task["task_id"],
        "kind": "repair",
        **flags,
        "regression_failures": regression_failures,
        "cost_micros": int(run.get("cost_micros", 0)),
        "prompt_tokens": int(run.get("prompt_tokens", 0)),
        "completion_tokens": int(run.get("completion_tokens", 0)),
        "latency_ms": float(run.get("latency_ms", 0.0)),
        "model_calls": int(run.get("model_calls", 0)),
        "tool_calls": int(run.get("tool_calls", 0)),
    }
    metrics["passed"] = all(flags.values()) and regression_failures == 0
    return metrics


def grade_memory(case: Mapping[str, Any], answer: Mapping[str, Any]) -> dict[str, Any]:
    """Grade one memory retrieval case (synthetic selftest input only)."""
    retrieved = [item for item in answer.get("retrieved", []) if isinstance(item, Mapping)]
    k = int(case.get("k", 5))
    relevant = {str(item) for item in case.get("relevant_memory_ids", [])}
    stale = {str(item) for item in case.get("stale_memory_ids", [])}
    contaminated = {str(item) for item in case.get("contaminated_memory_ids", [])}
    top = retrieved[:k]
    top_ids = {str(item.get("memory_id")) for item in top}
    recall = len(top_ids & relevant) / len(relevant) if relevant else 1.0
    stale_hits = sum(1 for item in retrieved if str(item.get("memory_id")) in stale)
    stale_rejection = 1.0 - (stale_hits / len(stale) if stale else 0.0)
    contamination = (
        sum(1 for item in retrieved if str(item.get("memory_id")) in contaminated) / len(retrieved)
        if retrieved
        else 0.0
    )
    baseline_tokens = float(case.get("baseline_context_tokens", 0))
    compressed_tokens = float(answer.get("compressed_context_tokens", baseline_tokens))
    token_reduction = (
        1.0 - compressed_tokens / baseline_tokens if baseline_tokens > 0 else 0.0
    )
    baseline_calls = float(case.get("baseline_repeated_tool_calls", 0))
    actual_calls = float(answer.get("repeated_tool_calls", baseline_calls))
    repeated_reduction = 1.0 - actual_calls / baseline_calls if baseline_calls > 0 else 0.0
    return {
        "case_id": case.get("case_id"),
        "recall_at_k": recall,
        "stale_rejection": stale_rejection,
        "contamination_rate": contamination,
        "token_reduction": token_reduction,
        "repeated_tool_call_reduction": repeated_reduction,
        "retrieval_cost": {
            "tokens": int(answer.get("retrieval_tokens", 0)),
            "calls": int(answer.get("retrieval_calls", 0)),
            "cost_micros": int(answer.get("retrieval_cost_micros", 0)),
        },
    }


def correctness_delta(
    records: Sequence[Mapping[str, Any]], without_arm: str = "A", with_arm: str = "C"
) -> dict[str, Any]:
    """Correctness with/without cross-task memory over paired records."""
    def passed(arm: str) -> dict[str, bool]:
        return {
            str(record["case_id"]): record["status"] == "PASS"
            for record in records
            if str(record["arm"]) == arm
        }

    left, right = passed(without_arm), passed(with_arm)
    common = sorted(set(left) & set(right))
    if not common:
        return {
            "without_memory": None,
            "with_memory": None,
            "difference_pp": None,
            "pairs": 0,
        }
    without = sum(left[case] for case in common) / len(common)
    with_memory = sum(right[case] for case in common) / len(common)
    return {
        "without_memory": without,
        "with_memory": with_memory,
        "difference_pp": (with_memory - without) * 100,
        "pairs": len(common),
    }


def pair_coverage(
    records: Sequence[Mapping[str, Any]], arms: Sequence[str] = ARMS
) -> dict[str, Any]:
    keys = {
        arm: {
            (str(record["case_id"]), int(record.get("repeat_index", 0)))
            for record in records
            if str(record["arm"]) == arm
        }
        for arm in arms
    }
    reference = keys[arms[0]]
    return {
        "arms": list(arms),
        "pairs": len(reference),
        "missing": {
            arm: sorted(reference - keys[arm]) for arm in arms if keys[arm] != reference
        },
        "complete": all(keys[arm] == reference for arm in arms),
    }


def apply_unified_patch(patch: str, root: Path) -> list[str]:
    """Apply a git-style unified diff to files below ``root``."""
    root = Path(root).resolve()
    changed: list[str] = []
    lines = patch.splitlines()
    index = 0
    while index < len(lines):
        if not lines[index].startswith("diff --git "):
            index += 1
            continue
        index += 1
        target: str | None = None
        while index < len(lines) and not lines[index].startswith("@@"):
            if lines[index].startswith("+++ "):
                raw = lines[index][4:].strip()
                if raw != "/dev/null":
                    target = raw[2:] if raw.startswith(("a/", "b/")) else raw
            index += 1
        if target is None:
            raise EvaluationError(["patch has no target file"])
        path = (root / target).resolve()
        if root not in path.parents and path != root:
            raise EvaluationError([f"patch path escapes root: {target}"])
        original = path.read_text(encoding="utf-8").splitlines()
        output: list[str] = []
        position = 0
        while index < len(lines) and not lines[index].startswith("diff --git "):
            match = _HUNK.match(lines[index])
            if not match:
                index += 1
                continue
            old_start = int(match.group(1))
            index += 1
            hunk: list[str] = []
            while index < len(lines) and not lines[index].startswith(("@@", "diff --git ")):
                hunk.append(lines[index])
                index += 1
            cursor = old_start - 1
            output.extend(original[position:cursor])
            for raw in hunk:
                if raw.startswith("\\"):
                    continue
                tag, text = raw[:1], raw[1:]
                if tag == " ":
                    if cursor >= len(original) or original[cursor] != text:
                        raise EvaluationError([f"patch context mismatch in {target}"])
                    output.append(text)
                    cursor += 1
                elif tag == "-":
                    if cursor >= len(original) or original[cursor] != text:
                        raise EvaluationError([f"patch removal mismatch in {target}"])
                    cursor += 1
                elif tag == "+":
                    output.append(text)
            position = cursor
        output.extend(original[position:])
        path.write_text("\n".join(output) + ("\n" if output else ""), encoding="utf-8")
        changed.append(target)
    if not changed:
        raise EvaluationError(["patch changed no files"])
    return changed


def run_python_snippet(code: str, snapshot_src: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(snapshot_src)
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )


def _run_pytest(test_file: Path, snapshot_src: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(snapshot_src)
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(test_file),
            "-q",
            "-p",
            "no:cacheprovider",
        ],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
        cwd=str(test_file.parent),
    )


def _load_verifier(script: Path) -> Any:
    key = str(script)
    if key not in _VERIFIER_CACHE:
        spec = importlib.util.spec_from_file_location(script.stem, script)
        if spec is None or spec.loader is None:
            raise EvaluationError([f"cannot load verifier {script}"])
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _VERIFIER_CACHE[key] = module
    return _VERIFIER_CACHE[key]


def _gold_checks(experiment_dir: Path, task: Mapping[str, Any]) -> list[str]:
    problems: list[str] = []
    patch_file = str(task.get("provenance", {}).get("fix_patch", ""))
    patch_text = (experiment_dir / patch_file).read_text(encoding="utf-8")
    for finding in task.get("gold_findings", []):
        problems.extend(_check_quote(experiment_dir, task, finding))
        problems.extend(_check_fix_evidence(experiment_dir, task, finding, patch_text))
    if problems:
        raise EvaluationError(problems)
    return [f"quote and upstream fix evidence verified for {len(task['gold_findings'])} finding(s)"]


def _verify_review(
    experiment_dir: Path, task: Mapping[str, Any]
) -> list[str]:
    checks = _gold_checks(experiment_dir, task)
    verifier = task.get("verifier")
    if isinstance(verifier, Mapping):
        function = _load_verifier(experiment_dir / str(verifier["script"]))
        checks.extend(getattr(function, str(verifier["function"]))(experiment_dir, task))
    return checks


def _verify_negative(experiment_dir: Path, task: Mapping[str, Any]) -> list[str]:
    negative = task["negative"]
    patterns = [str(item) for item in negative["forbidden_patterns"]]
    checks: list[str] = []
    for relative in negative["scope_files"]:
        text = (experiment_dir / str(task["snapshot_dir"]) / str(relative)).read_text(
            encoding="utf-8"
        )
        for pattern in patterns:
            if re.search(pattern, text):
                raise EvaluationError(
                    [f"{task['task_id']}: forbidden pattern {pattern!r} present in {relative}"]
                )
        checks.append(f"no forbidden pattern in {relative}")
    verifier = negative.get("verifier")
    if isinstance(verifier, Mapping):
        function = _load_verifier(experiment_dir / str(verifier["script"]))
        checks.extend(getattr(function, str(verifier["function"]))(experiment_dir, task))
    return checks


def _verify_repair(experiment_dir: Path, task: Mapping[str, Any]) -> list[str]:
    repair = task["repair"]
    checks = _gold_checks(experiment_dir, task)
    buggy = experiment_dir / str(repair["buggy_snapshot_dir"])
    fixed = experiment_dir / str(repair["fixed_snapshot_dir"])
    test_file = experiment_dir / str(repair["test_file"])
    buggy_run = _run_pytest(test_file, buggy / "src")
    if buggy_run.returncode == 0:
        raise EvaluationError(
            [f"{task['task_id']}: independent test unexpectedly passes on buggy tree"]
        )
    checks.append("independent test fails on buggy snapshot")
    fixed_run = _run_pytest(test_file, fixed / "src")
    if fixed_run.returncode != 0:
        raise EvaluationError(
            [f"{task['task_id']}: independent test fails on fixed snapshot\n{fixed_run.stdout}"]
        )
    checks.append("independent test passes on fixed snapshot")
    with tempfile.TemporaryDirectory(prefix="e01-patch-") as temporary:
        copy = Path(temporary) / "tree"
        shutil.copytree(buggy, copy)
        patch_text = (experiment_dir / str(repair["patch_file"])).read_text(encoding="utf-8")
        changed = apply_unified_patch(patch_text, copy)
        if not changed:
            raise EvaluationError([f"{task['task_id']}: patch changed no files"])
        for relative in changed:
            if (copy / relative).read_bytes() != (fixed / relative).read_bytes():
                raise EvaluationError(
                    [f"{task['task_id']}: patched {relative} differs from fixed snapshot"]
                )
        checks.append(f"reference patch reproduces fixed tree for {len(changed)} file(s)")
        patched_run = _run_pytest(test_file, copy / "src")
        if patched_run.returncode != 0:
            raise EvaluationError(
                [f"{task['task_id']}: independent test fails on patched tree\n{patched_run.stdout}"]
            )
        checks.append("independent test passes on patched tree")
    return checks


def verify_task(experiment_dir: Path, task: Mapping[str, Any]) -> dict[str, Any]:
    try:
        if task["kind"] == "repair":
            checks = _verify_repair(Path(experiment_dir), task)
        elif task["kind"] == "negative":
            checks = _verify_negative(Path(experiment_dir), task)
        else:
            checks = _verify_review(Path(experiment_dir), task)
    except (EvaluationError, OSError, subprocess.SubprocessError, AssertionError) as error:
        return {
            "task_id": task["task_id"],
            "kind": task["kind"],
            "status": "FAIL",
            "checks": [],
            "error": str(error),
        }
    return {
        "task_id": task["task_id"],
        "kind": task["kind"],
        "status": "PASS",
        "checks": checks,
    }


def run_selftest(experiment_dir: Path, manifest: Mapping[str, Any]) -> dict[str, Any]:
    tasks, holdout = load_tasks(Path(experiment_dir), manifest)
    results = [verify_task(Path(experiment_dir), task) for task in tasks]
    counts = {
        "PASS": sum(1 for item in results if item["status"] == "PASS"),
        "FAIL": sum(1 for item in results if item["status"] == "FAIL"),
        "SKIPPED": 0,
    }
    return {
        "schema": SELFTEST_SCHEMA,
        "scope": "harness_conformance_not_model_capability",
        "experiment_id": manifest["experiment_id"],
        "task_count": len(tasks),
        "status_counts": counts,
        "tasks": {item["task_id"]: item for item in results},
        "holdout_executed": False,
        "holdout_task_count": len(holdout),
        "note": (
            "Executes offline verifiers and the reference patch only. "
            "It is not a model capability score and is never counted in A/B/C success."
        ),
    }
