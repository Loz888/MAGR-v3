"""Offline CLI for quality evaluation experiments (QEV-02).

Subcommands:

* ``validate``  - schema, duplicate keys, manifest self-hash and cross-checks
* ``summarize`` - per-arm statistics written as summary.json + summary.md
* ``compare``   - paired baseline/candidate comparison written as comparison.*.md

No model client, no network, no paid calls: every command is pure file I/O.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from mgar_v3.quality_eval import stats, v34, validate
from mgar_v3.quality_eval.manifest import verify_manifest
from mgar_v3.quality_eval.record import canonical_digest


def _read_json(path: Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def _fmt(value: Any, digits: int = 4) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def load_experiment(
    directory: Path, *, root: Path | None = None, check_files: bool = False
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    manifest = _read_json(directory / "manifest.json")
    records = validate.load_jsonl(directory / "cases.jsonl")
    problems = validate.validate_records(records)
    problems.extend(validate.check_manifest_consistency(manifest, records))
    if isinstance(manifest.get("v34"), Mapping):
        problems.extend(v34.validate_bundle(directory, manifest, check_files=check_files))
    if check_files:
        file_result = verify_manifest(manifest, root=root, check_files=True)
        problems.extend(file_result["errors"])
    if problems:
        raise validate.ValidationError(problems)
    return manifest, records


def _manifest_view(manifest: Mapping[str, Any], file_checks: Mapping[str, str]) -> dict[str, Any]:
    dataset = manifest["dataset"]
    view = {
        "schema": manifest["schema"],
        "manifest_sha256": manifest["manifest_sha256"],
        "experiment_id": manifest["experiment_id"],
        "baseline_commit": manifest["baseline_commit"],
        "candidate_commit": manifest["candidate_commit"],
        "dataset_hash": dataset["hash"],
        "dataset_file": dataset["file"],
        "dataset_case_count": dataset["count"],
        "config_hash": manifest["config"]["hash"],
        "arms": manifest["arms"],
        "pairing_key": manifest["pairing_key"],
        "file_checks": dict(file_checks),
    }
    if isinstance(manifest.get("v34"), Mapping):
        source = manifest["v34"]
        view["v34"] = {
            "tasks_file": source.get("tasks_file"),
            "holdout_file": source.get("holdout_file"),
            "repositories_file": source.get("repositories_file"),
            "holdout_ids": list(source.get("holdout_ids", [])),
            "not_run_reason": source.get("not_run_reason"),
        }
    return view


def _status_counts(records: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, int]]:
    result: dict[str, dict[str, int]] = {}
    for arm in sorted({str(record["arm"]) for record in records}):
        rows = [record for record in records if str(record["arm"]) == arm]
        result[arm] = {
            "records": len(rows),
            "pass": sum(record["status"] == "PASS" for record in rows),
            "fail": sum(record["status"] == "FAIL" for record in rows),
            "not_run": sum(record["status"] == "NOT_RUN" for record in rows),
            "unknown": sum(record["status"] == "UNKNOWN" for record in rows),
        }
    return result


def _summary_markdown(summary: Mapping[str, Any], manifest: Mapping[str, Any]) -> str:
    lines = [
        f"# 质量评测摘要 {summary['experiment_id']}",
        "",
        "> 本文件由 `scripts/quality_eval.py summarize` 生成；数字可由 `cases.jsonl` 复算。",
        "",
        f"- 记录数：{summary['record_count']}，唯一 Case：{summary['case_count']}",
        f"- 数据集 Hash：`{summary['dataset_hash']}`",
        f"- 配置 Hash：`{summary['config_hash']}`",
        f"- 汇总摘要 Hash：`{summary['summary_sha256']}`",
        f"- 实验 Manifest：`{manifest['manifest_sha256']}`",
        "",
        "成功率分母仅包含 `PASS` + `FAIL`；`NOT_RUN` 与 `UNKNOWN` 单独计数，不计入分子或分母。",
        "",
        "| 臂 | PASS | FAIL | NOT_RUN | UNKNOWN | 成功率（分子/分母） | 95% CI |",
        "|---|---:|---:|---:|---:|---|---|",
    ]
    for arm, item in summary["arms"].items():
        counts = item["status_counts"]
        rate = item["success_rate"]
        interval = rate["interval95"]
        lines.append(
            "| {arm} | {pass_} | {fail} | {nr} | {un} | {num}/{den} = {value} | {ci} |".format(
                arm=arm,
                pass_=counts["pass"],
                fail=counts["fail"],
                nr=counts["not_run"],
                un=counts["unknown"],
                num=rate["numerator"],
                den=rate["denominator"],
                value=_fmt(rate["value"]),
                ci="n/a" if interval is None else f"[{interval[0]:.4f}, {interval[1]:.4f}]",
            )
        )
    lines.extend(["", "## 失败分类", "", "| 臂 | failure_kind | 数量 |", "|---|---|---:|"])
    failure_rows = 0
    for arm, item in summary["arms"].items():
        for kind, count in sorted(item["failure_kinds"].items()):
            lines.append(f"| {arm} | {kind} | {count} |")
            failure_rows += 1
    if not failure_rows:
        lines.append("| — | 无 | 0 |")
    lines.extend(["", "## NOT_RUN 原因", "", "| 臂 | 原因 | 数量 |", "|---|---|---:|"])
    not_run_rows = 0
    for arm, item in summary["arms"].items():
        for reason, count in sorted(item["not_run_reasons"].items()):
            lines.append(f"| {arm} | {reason} | {count} |")
            not_run_rows += 1
    if not not_run_rows:
        lines.append("| — | 无 | 0 |")
    lines.extend(
        [
            "",
            "## 延迟（P50/P95，按 cache_state 分列，不混合）",
            "",
            "| 臂 | 缓存状态 | 样本数 | P50 (ms) | P95 (ms) |",
            "|---|---|---:|---:|---:|",
        ]
    )
    for arm, item in summary["arms"].items():
        for state, value in sorted(item["latency_by_cache"].items()):
            lines.append(
                f"| {arm} | {state} | {value['n']} | {_fmt(value['p50_ms'], 1)} "
                f"| {_fmt(value['p95_ms'], 1)} |"
            )
    lines.extend(
        [
            "",
            "## 成本与调用",
            "",
            "| 臂 | model_calls | prompt_tokens | completion_tokens | cost_micros | "
            "每完成用例成本（分子/分母） |",
            "|---|---:|---:|---:|---:|---:|",
        ]
    )
    for arm, item in summary["arms"].items():
        totals = item["totals"]
        cost = item["cost_per_completed_case_micros"]
        lines.append(
            f"| {arm} | {totals['model_calls']} | {totals['prompt_tokens']} "
            f"| {totals['completion_tokens']} | {totals['cost_micros']} "
            f"| {cost['numerator']}/{cost['denominator']} = {_fmt(cost['value'], 1)} |"
        )
    if isinstance(summary.get("v34"), Mapping):
        lines.extend(_v34_markdown(summary["v34"]))
    lines.append("")
    return "\n".join(lines)


def _v34_markdown(section: Mapping[str, Any]) -> list[str]:
    dataset = section["dataset"]
    kinds = dataset["kinds"]
    languages = dataset["languages"]
    selftest = section.get("harness_selftest") or {}
    live = section["live_arms"]
    totals = live["totals"]
    lines = [
        "",
        "## V3.4 真实公开仓库数据集（E01）",
        "",
        f"- 仓库数：{dataset['repositories']}；任务数：{dataset['tasks']}"
        f"（dev {dataset['dev_tasks']} / holdout {dataset['holdout_tasks']}）",
        f"- 任务类型：review {kinds['review']}，repair {kinds['repair']}，"
        f"negative {kinds['negative']}",
        f"- 语言覆盖：python {languages['python']}，go {languages['go']}",
        f"- Holdout 未参与开发：{', '.join(section['holdout']['ids'])}"
        f"（used_in_development={section['holdout']['used_in_development']}）",
        f"- 实时臂状态：`{live['status']}`"
        + (f"；原因：{live['reason']}" if live.get("reason") else ""),
        f"- 真实调用：{totals['model_calls']} 次，记录 {totals['records']} 条"
        f"（执行 {totals['executed']}，NOT_RUN {totals['not_run']}），"
        f"保守计费 {totals['cost_micros']} micros（USD {totals['cost_micros'] / 1_000_000:.6f}）",
        f"- Tokens：输入 {totals['prompt_tokens']}，输出 {totals['completion_tokens']}",
        "",
        "| 臂 | 记录数 | 执行 | NOT_RUN | PASS | FAIL |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for arm, counts in live["records"].items():
        lines.append(
            f"| {arm} | {counts['records']} | {counts['executed']} | {counts['not_run']} "
            f"| {counts['pass']} | {counts['fail']} |"
        )
    lines.extend(
        [
            "",
            "| 臂 | Precision@K | Recall@Budget | 未验证误报 | Citation | Chain | Coverage |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for arm, item in live["by_arm"].items():
        review = item["review"]
        lines.append(
            f"| {arm} | {_fmt(review['precision_at_k_mean'])} "
            f"| {_fmt(review['recall_at_budget_mean'])} "
            f"| {review['unverified_false_positives']} "
            f"| {_fmt(review['source_citation_validity_mean'])} "
            f"| {_fmt(review['call_chain_correctness_mean'])} "
            f"| {_fmt(review['scan_coverage_mean'])} |"
        )
    lines.extend(
        [
            "",
            "| 臂 | 修复用例 | 复现成功 | 补丁可应用 | 独立测试通过 | Draft PR 正确 | 回归失败 |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for arm, item in live["by_arm"].items():
        repair = item["repair"]
        lines.append(
            f"| {arm} | {repair['cases']} | {repair['bug_reproduction_success']} "
            f"| {repair['patch_validation_success']} | {repair['independent_test_pass']} "
            f"| {repair['draft_pr_correctness']} | {repair['regression_failures']} |"
        )
    paired = live["paired"]
    lines.extend(
        [
            "",
            f"- 配对完整用例：{paired['cases_with_all_arms_executed']}/{paired['cases']}",
            f"- 正确率（无 Memory / 仅检索 / 完整 Memory+压缩）："
            f"{_fmt(paired['correctness_without_memory'])} / "
            f"{_fmt(paired['correctness_with_retrieval_only'])} / "
            f"{_fmt(paired['correctness_with_memory_and_compression'])}",
            f"- 输入 Token 相对 A 的增量（B / C）："
            f"{_fmt(paired['prompt_token_overhead'].get('B'))} / "
            f"{_fmt(paired['prompt_token_overhead'].get('C'))}",
            f"- 成本相对 A 的增量 micros（B / C）："
            f"{_fmt(paired['cost_overhead_micros'].get('B'))} / "
            f"{_fmt(paired['cost_overhead_micros'].get('C'))}",
            f"- C 相对 B 的上下文 Token 缩减：{_fmt(paired['context_token_reduction_c_vs_b'])}",
        ]
    )
    if selftest:
        lines.extend(
            [
                "",
                f"- 离线 Harness 自检（scope={selftest['scope']}）："
                f"执行 {selftest['executed']}，PASS {selftest['pass']}，FAIL {selftest['fail']}",
                "- 该自检只证明判定管线与固定答案一致，不是模型能力成绩，也不计入 A/B/C 成功率。",
            ]
        )
    lines.extend(["", "未测量的指标：", ""])
    for name, reason in live.get("metrics_unmeasured", {}).items():
        lines.append(f"- `{name}`：{reason}")
    return lines


def _comparison_markdown(comparison: Mapping[str, Any]) -> str:
    coverage = comparison["coverage"]
    success = comparison["success"]
    baseline = comparison["baseline"]
    candidate = comparison["candidate"]
    lines = [
        f"# 配对比较 {baseline} vs {candidate}",
        "",
        "> 本文件由 `scripts/quality_eval.py compare` 生成；仅作配对描述统计。",
        "",
        f"- 实验：`{comparison['experiment_id']}`",
        f"- 数据集 Hash：`{comparison['dataset_hash']}`",
        f"- 配置 Hash：`{comparison['config_hash']}`",
        f"- 配对键：all `(case_id, repeat_index)`；总对数 {coverage['pairs_total']}，"
        f"可计入 {coverage['pairs_eligible']}，排除 {coverage['pairs_excluded']}",
        f"- 排除原因：{coverage['exclusion_reasons'] or '无'}",
        "",
        "| 臂 | 成功（分子/分母） | 成功率 | 95% CI |",
        "|---|---|---|---|",
    ]
    for label, key in ((baseline, "baseline"), (candidate, "candidate")):
        item = success[key]
        lines.append(
            f"| {label} | {item['numerator']}/{item['denominator']} "
            f"| {_fmt(item['value'])} | {item['interval95']} |"
        )
    lines.extend(
        [
            "",
            f"- 成功率差：{_fmt(success['difference_pp'], 2)} 个百分点"
            f"（{candidate} 减 {baseline}）",
            f"- 仅 {baseline} 成功：{success[f'only_{baseline}']}",
            f"- 仅 {candidate} 成功：{success[f'only_{candidate}']}",
            f"- 两臂均成功：{success['both_pass']}",
            f"- 两臂均失败：{success['both_fail']}",
            f"- 精确 McNemar 双侧 p：{_fmt(success['mcnemar_exact_two_sided_p'], 6)}",
            "",
            "## 延迟差（配对 Bootstrap，按 cache_state 分列）",
            "",
            "| 缓存状态 | 对数 | 平均差 (ms) | 95% CI (ms) |",
            "|---|---:|---:|---|",
        ]
    )
    for state, item in sorted(comparison["latency_difference_by_cache"].items()):
        ci = item["ci95"]
        lines.append(
            f"| {state} | {item['n_pairs']} | {_fmt(item['mean_difference'], 1)} "
            f"| {'n/a' if ci is None else f'[{ci[0]:.1f}, {ci[1]:.1f}]'} |"
        )
    cost = comparison["cost_difference"]
    cost_ci = cost["ci95"]
    lines.extend(
        [
            "",
            f"- 缓存状态不一致的配对数（不参与延迟比较）："
            f"{comparison['cache_state_mismatch_pairs']}",
            f"- 每用例成本差（{candidate} 减 {baseline}）：{_fmt(cost['mean_difference'], 1)} "
            f"micros，95% CI "
            f"{'n/a' if cost_ci is None else f'[{cost_ci[0]:.1f}, {cost_ci[1]:.1f}]'}",
            "",
            f"- 结论字段：`{comparison['decision']}` — {comparison['decision_note']}",
            "",
        ]
    )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline quality evaluation CLI (QEV-02)")
    sub = parser.add_subparsers(dest="command", required=True)

    validate_parser = sub.add_parser("validate", help="校验记录与 manifest")
    validate_parser.add_argument("--experiment", type=Path, required=True)
    validate_parser.add_argument("--root", type=Path)
    validate_parser.add_argument("--check-files", action="store_true")

    summarize_parser = sub.add_parser("summarize", help="生成分臂统计")
    summarize_parser.add_argument("--experiment", type=Path, required=True)
    summarize_parser.add_argument("--out", type=Path)
    summarize_parser.add_argument(
        "--bootstrap-iterations", type=int, default=stats.BOOTSTRAP_ITERATIONS
    )
    summarize_parser.add_argument("--seed", type=int, default=stats.BOOTSTRAP_SEED)

    compare_parser = sub.add_parser("compare", help="生成配对比较")
    compare_parser.add_argument("--experiment", type=Path, required=True)
    compare_parser.add_argument("--baseline", required=True)
    compare_parser.add_argument("--candidate", required=True)
    compare_parser.add_argument("--out", type=Path)
    compare_parser.add_argument(
        "--bootstrap-iterations", type=int, default=stats.BOOTSTRAP_ITERATIONS
    )
    compare_parser.add_argument("--seed", type=int, default=stats.BOOTSTRAP_SEED)

    args = parser.parse_args(argv)

    try:
        manifest, records = load_experiment(
            args.experiment,
            root=getattr(args, "root", None),
            check_files=getattr(args, "check_files", False),
        )
    except validate.ValidationError as error:
        print(
            json.dumps({"status": "FAIL", "errors": error.problems}, ensure_ascii=False, indent=2)
        )
        return 2

    if args.command == "validate":
        file_checks: Mapping[str, str] = {}
        if args.check_files:
            file_checks = verify_manifest(manifest, root=args.root, check_files=True)[
                "file_checks"
            ]
        result = {
            "status": "PASS",
            "errors": [],
            "manifest": _manifest_view(manifest, file_checks),
            "records": len(records),
            "arms": _status_counts(records),
        }
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    if args.command == "summarize":
        try:
            summary = stats.summarize(
                records, iterations=args.bootstrap_iterations, seed=args.seed
            )
        except validate.ValidationError as error:
            print(
                json.dumps(
                    {"status": "FAIL", "errors": error.problems}, ensure_ascii=False, indent=2
                )
            )
            return 2
        if isinstance(manifest.get("v34"), Mapping):
            summary["v34"] = v34.summarize_bundle(args.experiment, manifest, records)
            body = {key: value for key, value in summary.items() if key != "summary_sha256"}
            summary["summary_sha256"] = canonical_digest(body)
        out = args.out or args.experiment
        _write_json(out / "summary.json", summary)
        _write_text(out / "summary.md", _summary_markdown(summary, manifest))
        print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    try:
        comparison = stats.compare(
            records,
            args.baseline,
            args.candidate,
            iterations=args.bootstrap_iterations,
            seed=args.seed,
        )
    except validate.ValidationError as error:
        print(
            json.dumps({"status": "FAIL", "errors": error.problems}, ensure_ascii=False, indent=2)
        )
        return 2
    out = args.out or args.experiment
    _write_json(out / "comparison.json", comparison)
    _write_text(out / "comparison.md", _comparison_markdown(comparison))
    print(json.dumps(comparison, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
