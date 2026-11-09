"""Live A/B/C runner for the E01 real-repository evaluation.

Paid execution is only allowed by an explicit user authorization with a hard
USD cap. Every call is settled from provider usage; records never carry
estimated numbers. No GitHub write is performed.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

V3 = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(V3 / "src"))

from mgar_v3.quality_eval import v34  # noqa: E402
from mgar_v3.quality_eval.record import canonical_json  # noqa: E402
from mgar_v3.quality_eval.v34 import sha256_text  # noqa: E402

EXP = V3 / "experiments" / "v34-audit-repair"
MODEL_ID = "deepseek-flash"
ENDPOINT = "https://api.deepseek.com/v1/chat/completions"
PRICE_INPUT_MICROS_PER_MILLION = 300000
PRICE_OUTPUT_MICROS_PER_MILLION = 1200000
MAX_OUTPUT_TOKENS = 16384
BUDGET_MICROS = 2000000
ARMS = ("A", "B", "C")
SYSTEM = (
    "You are a senior software engineer performing a bounded code review or repair on "
    "pinned source files. Answer with a single compact JSON object and no prose outside it."
)
OUTPUT_SCHEMA = (
    'JSON shape: {"findings": [{"file": str, "symbol": str, "category": str, '
    '"call_chain": [str, ...], "start_line": int, "end_line": int, "quote": str}], '
    '"coverage": {"scanned_files": [str], "scannable_files": [str]}}. '
    "quote must be copied verbatim from the provided file lines. Return an empty findings "
    "list when nothing is provably wrong. Never invent a finding you cannot quote."
)
REPAIR_SCHEMA = (
    'JSON shape: {"reproduction": str, "patch": str, "files_touched": [str], "notes": str}. '
    "patch must be a git-style unified diff with a/ and b/ prefixes that applies to the "
    "provided snapshot without touching files outside the provided scope."
)


class BudgetExhausted(RuntimeError):
    pass


def load_env() -> dict[str, str]:
    values: dict[str, str] = {}
    for line in (V3 / ".env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


class Budget:
    def __init__(self, limit_micros: int) -> None:
        self.limit = limit_micros
        self.spent = 0
        self.calls = 0

    def reserve(self, prompt_chars: int) -> int:
        estimate = (prompt_chars // 3 + 512) * PRICE_INPUT_MICROS_PER_MILLION // 1_000_000
        estimate += MAX_OUTPUT_TOKENS * PRICE_OUTPUT_MICROS_PER_MILLION // 1_000_000
        if self.spent + estimate > self.limit:
            raise BudgetExhausted(
                f"budget guard: spent {self.spent} + estimate {estimate} > {self.limit} micros"
            )
        return estimate

    def settle(self, prompt_tokens: int, completion_tokens: int) -> int:
        cost = prompt_tokens * PRICE_INPUT_MICROS_PER_MILLION // 1_000_000
        cost += completion_tokens * PRICE_OUTPUT_MICROS_PER_MILLION // 1_000_000
        self.spent += cost
        self.calls += 1
        return cost


def post_chat(
    api_key: str, messages: list[dict[str, str]]
) -> tuple[str, dict[str, Any], float, str]:
    body = json.dumps(
        {
            "model": MODEL_ID,
            "messages": messages,
            "max_tokens": MAX_OUTPUT_TOKENS,
            "temperature": 0.0,
            "response_format": {"type": "json_object"},
        }
    ).encode()
    request = urllib.request.Request(
        ENDPOINT,
        data=body,
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + api_key},
    )
    started = time.monotonic()
    with urllib.request.urlopen(request, timeout=600) as response:
        payload = json.loads(response.read())
    latency_ms = round((time.monotonic() - started) * 1000, 3)
    message = payload["choices"][0]["message"]
    content = message.get("content") or ""
    usage = payload.get("usage") or {}
    return content, usage, latency_ms, payload.get("model") or MODEL_ID


def parse_json_object(content: str) -> Any:
    text = content.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text
        text = text.rsplit("```", 1)[0]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, depth = None, 0
        for index, char in enumerate(text):
            if char == "{":
                if start is None:
                    start = index
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0 and start is not None:
                    return json.loads(text[start : index + 1])
    raise ValueError("response did not contain a JSON object")


def render_files(task: dict[str, Any]) -> tuple[str, list[str]]:
    root = EXP / task["snapshot_dir"]
    blocks: list[str] = []
    scanned: list[str] = []
    for relative in task["scope_paths"]:
        path = root / relative
        blocks.append(
            f"--- FILE {relative} ---\n{path.read_text(encoding='utf-8')}\n--- END {relative} ---"
        )
        scanned.append(relative)
    return "\n\n".join(blocks), scanned


class MemoryStore:
    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []

    def retrieve(self, task: dict[str, Any], k: int = 2) -> list[dict[str, Any]]:
        same_repo = [entry for entry in self.entries if entry["repo_id"] == task["repo_id"]]
        return same_repo[-k:]

    def write(self, task: dict[str, Any], findings: list[dict[str, Any]]) -> list[str]:
        if not findings:
            return []
        entry = {
            "memory_id": f"mem-{len(self.entries) + 1:03d}",
            "task_id": task["task_id"],
            "repo_id": task["repo_id"],
            "language": task["language"],
            "findings": [
                {
                    "file": item.get("file", ""),
                    "symbol": item.get("symbol", ""),
                    "category": item.get("category", ""),
                    "quote": str(item.get("quote", ""))[:300],
                }
                for item in findings[:3]
            ],
        }
        self.entries.append(entry)
        return [entry["memory_id"]]


def memory_block(entries: list[dict[str, Any]], compressed: bool) -> str:
    if not entries:
        return ""
    if compressed:
        lines = ["Compressed cross-task memory:"]
        for entry in entries:
            labels = ", ".join(
                f"{item['category']}@{item['file']}:{item['symbol']}" for item in entry["findings"]
            )
            lines.append(f"- {entry['task_id']} ({entry['repo_id']}): {labels}")
        return "\n".join(lines)
    lines = ["Cross-task memory (raw excerpts from earlier tasks in this run):"]
    for entry in entries:
        for item in entry["findings"]:
            lines.append(
                f"- task {entry['task_id']} [{entry['repo_id']}] {item['file']} "
                f"{item['symbol']} {item['category']} quote={item['quote']!r}"
            )
    return "\n".join(lines)


def build_messages(
    task: dict[str, Any], arm: str, memory: list[dict[str, Any]]
) -> list[dict[str, str]]:
    files, scanned = render_files(task)
    parts = [task["prompt"], "", "Reviewed files (verbatim):", "", files, "", OUTPUT_SCHEMA]
    parts.append("coverage.scannable_files must equal: " + json.dumps(scanned))
    block = memory_block(memory, compressed=(arm == "C"))
    if block:
        parts.extend(["", block])
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": "\n".join(parts)}]


def build_repair_messages(
    task: dict[str, Any], arm: str, memory: list[dict[str, Any]]
) -> list[dict[str, str]]:
    files, _ = render_files(task)
    parts = [task["prompt"], "", "Buggy snapshot files (verbatim):", "", files, "", REPAIR_SCHEMA]
    block = memory_block(memory, compressed=(arm == "C"))
    if block:
        parts.extend(["", block])
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": "\n".join(parts)}]


def run_pytest(test_file: Path, source: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "pytest", str(test_file), "-q", "-p", "no:cacheprovider"],
        cwd=str(test_file.parent),
        env={
            "PYTHONPATH": str(source),
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "PATH": "/usr/local/bin:/usr/bin:/bin",
        },
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )


def grade_repair_live(task: dict[str, Any], answer: dict[str, Any]) -> dict[str, Any]:
    patch = str(answer.get("patch", ""))
    test_file = EXP / task["repair"]["test_file"]
    buggy_root = EXP / task["repair"]["buggy_snapshot_dir"]
    result: dict[str, Any] = {
        "bug_reproduction_success": False,
        "patch_validation_success": False,
        "independent_test_pass": False,
        "draft_pr_correctness": False,
        "regression_failures": 1,
        "files_touched": [],
        "error": None,
    }
    buggy_run = run_pytest(test_file, buggy_root / "src")
    result["bug_reproduction_success"] = buggy_run.returncode != 0
    if not patch.strip():
        result["error"] = "empty patch"
        return v34.grade_repair(task, result)
    workdir = Path(tempfile.mkdtemp(prefix="v34-live-"))
    try:
        tree = workdir / "tree"
        shutil.copytree(buggy_root, tree)
        try:
            touched = v34.apply_unified_patch(patch, tree)
        except v34.EvaluationError as error:
            result["error"] = f"patch rejected: {error}"
            return v34.grade_repair(task, result)
        result["patch_validation_success"] = bool(touched)
        result["files_touched"] = list(touched)
        allowed = [
            path
            for path in touched
            if any(
                path == scope or path.startswith(scope.rstrip("/") + "/")
                for scope in task["scope_paths"]
            )
        ]
        patched_run = run_pytest(test_file, tree / "src")
        result["independent_test_pass"] = patched_run.returncode == 0
        result["regression_failures"] = 0 if patched_run.returncode == 0 else 1
        result["draft_pr_correctness"] = bool(touched) and len(allowed) == len(touched)
        return v34.grade_repair(task, result)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def record_for(
    manifest: dict[str, Any],
    task: dict[str, Any],
    arm: str,
    *,
    status: str,
    failure_kind: str | None,
    latency_ms: float | None,
    model_calls: int,
    usage_source: str,
    tokens: tuple[int, int, int],
    refs: list[str],
    input_ref: str | None,
    output_ref: str | None,
    input_sha: str | None,
    output_sha: str | None,
    not_run_reason: str | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    return {
        "experiment_id": "v34-audit-repair",
        "baseline_commit": manifest["baseline_commit"],
        "candidate_commit": manifest["candidate_commit"],
        "dataset_hash": manifest["dataset"]["hash"],
        "config_hash": manifest["config"]["hash"],
        "case_id": task["task_id"],
        "arm": arm,
        "mode": "real_model_live",
        "status": status,
        "failure_kind": failure_kind,
        "latency_ms": latency_ms,
        "model_calls": model_calls,
        "usage_source": usage_source,
        "evidence_refs": refs,
        "repeat_index": 0,
        "order_index": task["order_index"],
        "seed": manifest["seed"],
        "cache_state": "none",
        "card": "E01",
        "not_run_reason": not_run_reason,
        "prompt_tokens": tokens[0],
        "completion_tokens": tokens[1],
        "cost_micros": tokens[2],
        "input_sha256": input_sha,
        "output_sha256": output_sha,
        "input_ref": input_ref,
        "output_ref": output_ref,
        "note": note,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tasks", default="")
    parser.add_argument("--arms", default="A,B,C")
    parser.add_argument("--run-id", default="")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    manifest = json.loads((EXP / "manifest.json").read_text(encoding="utf-8"))
    tasks, holdout = v34.load_tasks(EXP, manifest)
    everything = tasks + holdout
    if args.tasks:
        wanted = {item.strip() for item in args.tasks.split(",") if item.strip()}
        everything = [task for task in everything if task["task_id"] in wanted]
    if args.limit:
        everything = everything[: args.limit]
    arms = tuple(item.strip() for item in args.arms.split(",") if item.strip())
    run_id = args.run_id or time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    run_dir = EXP / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    api_key = load_env()["MGAR_V3_DEEPSEEK_API_KEY"]
    budget = Budget(BUDGET_MICROS)
    memory = MemoryStore()
    records: list[dict[str, Any]] = []
    order = 0

    for task in everything:
        for arm in arms:
            order += 1
            entry = dict(task, order_index=order)
            prefix = f"{task['task_id']}.{arm}"
            retrieved = memory.retrieve(task) if arm in {"B", "C"} else []
            messages = (
                build_repair_messages(entry, arm, retrieved)
                if task["kind"] == "repair"
                else build_messages(entry, arm, retrieved)
            )
            prompt_chars = sum(len(item["content"]) for item in messages)
            try:
                budget.reserve(prompt_chars)
            except BudgetExhausted as error:
                records.append(
                    record_for(
                        manifest,
                        entry,
                        arm,
                        status="NOT_RUN",
                        failure_kind=None,
                        latency_ms=None,
                        model_calls=0,
                        usage_source="not_applicable",
                        tokens=(0, 0, 0),
                        refs=["config/evaluation.json"],
                        input_ref=None,
                        output_ref=None,
                        input_sha=None,
                        output_sha=None,
                        not_run_reason=str(error),
                    )
                )
                continue

            input_path = run_dir / f"{prefix}.input.json"
            output_path = run_dir / f"{prefix}.output.json"
            result_path = run_dir / f"{prefix}.result.json"
            input_bytes = canonical_json(messages).encode()
            input_path.write_bytes(input_bytes)
            input_sha = sha256_text(canonical_json(messages))
            try:
                content, usage, latency_ms, served_model = post_chat(api_key, messages)
            except (urllib.error.URLError, TimeoutError, OSError) as error:
                records.append(
                    record_for(
                        manifest,
                        entry,
                        arm,
                        status="FAIL",
                        failure_kind="infrastructure",
                        latency_ms=None,
                        model_calls=0,
                        usage_source="not_applicable",
                        tokens=(0, 0, 0),
                        refs=[f"runs/{run_id}/{prefix}.input.json"],
                        input_ref=f"runs/{run_id}/{prefix}.input.json",
                        output_ref=None,
                        input_sha=input_sha,
                        output_sha=None,
                        note=f"transport error: {type(error).__name__}",
                    )
                )
                continue

            prompt_tokens = int(usage.get("prompt_tokens", 0))
            completion_tokens = int(usage.get("completion_tokens", 0))
            cost = budget.settle(prompt_tokens, completion_tokens)
            output_sha = sha256_text(content)
            output_path.write_text(
                json.dumps(
                    {
                        "content": content,
                        "usage": usage,
                        "model": served_model,
                        "latency_ms": latency_ms,
                        "cost_micros": cost,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            try:
                answer = parse_json_object(content)
                if not isinstance(answer, dict):
                    raise ValueError("answer is not an object")
                parse_error = None
            except ValueError as error:
                answer = {"findings": [], "coverage": {"scanned_files": [], "scannable_files": []}}
                parse_error = str(error)

            if task["kind"] == "repair":
                grading = grade_repair_live(task, answer)
                findings: list[dict[str, Any]] = []
            else:
                if not isinstance(answer.get("findings"), list):
                    answer["findings"] = []
                if not isinstance(answer.get("coverage"), dict):
                    answer["coverage"] = {}
                findings = answer["findings"]
                grading = v34.grade_review(task, answer, experiment_dir=EXP)
            status = "PASS" if grading.get("passed") else "FAIL"
            memory_ids = memory.write(task, findings) if arm == "C" else []
            result_path.write_text(
                json.dumps(
                    {
                        "task_id": task["task_id"],
                        "arm": arm,
                        "answer": answer,
                        "grading": grading,
                        "memory_read": [item["memory_id"] for item in retrieved],
                        "memory_written": memory_ids,
                        "parse_error": parse_error,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            records.append(
                record_for(
                    manifest,
                    entry,
                    arm,
                    status=status,
                    failure_kind=None if status == "PASS" else "business",
                    latency_ms=latency_ms,
                    model_calls=1,
                    usage_source="actual_provider_usage",
                    tokens=(prompt_tokens, completion_tokens, cost),
                    refs=[
                        f"runs/{run_id}/{prefix}.result.json",
                        f"runs/{run_id}/{prefix}.input.json",
                        f"runs/{run_id}/{prefix}.output.json",
                    ],
                    input_ref=f"runs/{run_id}/{prefix}.input.json",
                    output_ref=f"runs/{run_id}/{prefix}.output.json",
                    input_sha=input_sha,
                    output_sha=output_sha,
                    note=(
                        "memory_read="
                        + (",".join(item["memory_id"] for item in retrieved) or "none")
                        + ";memory_written="
                        + (",".join(memory_ids) or "none")
                        + (f"; parse_error={parse_error}" if parse_error else "")
                    ),
                )
            )
            print(
                json.dumps(
                    {
                        "task": task["task_id"],
                        "arm": arm,
                        "status": status,
                        "prompt_tokens": prompt_tokens,
                        "completion_tokens": completion_tokens,
                        "cost_micros": cost,
                        "spent_micros": budget.spent,
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )

    (run_dir / "records.jsonl").write_text(
        "".join(canonical_json(record) + "\n" for record in records), encoding="utf-8"
    )
    (run_dir / "memory.json").write_text(
        json.dumps(memory.entries, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    report = {
        "run_id": run_id,
        "model": MODEL_ID,
        "calls": budget.calls,
        "spent_micros": budget.spent,
        "budget_micros": budget.limit,
        "records": len(records),
        "status_counts": {
            status: sum(1 for record in records if record["status"] == status)
            for status in ("PASS", "FAIL", "NOT_RUN")
        },
    }
    (run_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
