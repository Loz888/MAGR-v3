"""Mechanical verification and Markdown rendering for repository reports."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from uuid import UUID

from pydantic import Field

from mgar_v3.domain import Model
from mgar_v3.repository_analysis.domain import (
    AnalysisSpec,
    PartialReport,
    RepoAnalysisReport,
    SourceClaim,
    ToolObservation,
    locate_quote,
)


class EvidenceRecord(Model):
    evidence_id: UUID
    source_uri: str
    source_version: str
    content_hash: str
    start_line: int
    end_line: int


class ClaimBinding(Model):
    section: str
    statement: str
    evidence_id: UUID
    quote: str
    source_uri: str
    start_line: int
    end_line: int


class ReportVerification(Model):
    passed: bool
    checks: list[str]
    failures: list[str] = Field(default_factory=list)
    bindings: list[ClaimBinding] = Field(default_factory=list)


def _path_from_uri(uri: str) -> str:
    if "://" not in uri:
        return uri
    body = uri.split("://", 1)[1]
    parts = body.split("/", 2)
    return parts[2] if len(parts) > 2 else ""


def verify_report(
    report: RepoAnalysisReport,
    evidence: Iterable[EvidenceRecord],
    read_artifact: Callable[[str], bytes],
) -> ReportVerification:
    records = {record.evidence_id: record for record in evidence}
    checks: list[str] = []
    failures: list[str] = []
    bindings: list[ClaimBinding] = []
    seen_claim_keys: set[tuple[str, str, str]] = set()
    for section, claims in (
        ("findings", report.findings),
        ("key_files", report.key_files),
        ("call_chains", report.call_chains),
    ):
        for index, claim in enumerate(claims):
            label = f"{section}[{index}]"
            record = records.get(claim.evidence_id)
            if record is None:
                failures.append(f"{label}: evidence was not read by this run")
                continue
            try:
                content = read_artifact(record.content_hash)
            except Exception:
                failures.append(f"{label}: evidence bytes are unavailable")
                continue
            located = locate_quote(content, claim.quote)
            if located is None:
                failures.append(f"{label}: quote is not a verbatim source span")
                continue
            key = (str(claim.evidence_id), claim.quote, section)
            if key in seen_claim_keys:
                failures.append(f"{label}: duplicate evidence claim")
                continue
            seen_claim_keys.add(key)
            start, end = located
            if not (record.start_line <= start <= end <= record.end_line):
                failures.append(f"{label}: located lines fall outside the recorded evidence block")
                continue
            bindings.append(
                ClaimBinding(
                    section=section,
                    statement=claim.statement,
                    evidence_id=claim.evidence_id,
                    quote=claim.quote,
                    source_uri=record.source_uri,
                    start_line=start,
                    end_line=end,
                )
            )
            checks.append(f"{label}: evidence_content_and_locator")
    if not failures:
        checks.append("all_source_claims_bound")
    return ReportVerification(
        passed=not failures, checks=checks, failures=failures, bindings=bindings
    )


def _claim_markdown(binding: ClaimBinding, repository: str, commit: str) -> str:
    path = _path_from_uri(binding.source_uri).split("?", 1)[0]
    link = f"https://github.com/{repository}/blob/{commit}/{path}#L{binding.start_line}-L{binding.end_line}"
    quote = binding.quote if len(binding.quote) <= 800 else binding.quote[:800] + " …"
    quoted = "\n".join("    " + line for line in quote.rstrip("\n").splitlines())
    return (
        f"- **{binding.statement}**\n"
        f"  - 源码位置：`{binding.source_uri}` · [L{binding.start_line}-L{binding.end_line}]({link})\n"
        f"  - 证据 ID：`{binding.evidence_id}`\n"
        f"  - 原文：\n\n    ```text\n{quoted}\n    ```\n"
    )


def render_report(
    spec: AnalysisSpec,
    report: RepoAnalysisReport,
    verification: ReportVerification,
) -> bytes:
    lines = [
        "# RepoSage 仓库分析报告",
        "",
        f"- Repository: `{spec.repository}`",
        f"- Commit: `{spec.commit}`",
        f"- 执行模式: `{spec.execution_mode}`",
        f"- 任务: {spec.task}",
        "",
        "## 1. 问题结论",
        "",
        report.conclusion,
        "",
        "## 2. 仓库总体结构",
        "",
        report.structure,
        "",
        "## 3. 关键代码文件与调用关系",
        "",
    ]
    key_files = [b for b in verification.bindings if b.section == "key_files"]
    chains = [b for b in verification.bindings if b.section == "call_chains"]
    if not key_files:
        lines.extend(["（未形成经源码证据支持的关键文件结论）", ""])
    for binding in key_files:
        lines.append(_claim_markdown(binding, spec.repository, spec.commit))
    if chains:
        lines.extend(["", "### 调用关系", ""])
        for binding in chains:
            lines.append(_claim_markdown(binding, spec.repository, spec.commit))
    lines.extend(["", "## 4. 实际代码证据", ""])
    findings = [b for b in verification.bindings if b.section == "findings"]
    if not findings:
        lines.extend(["（没有通过机械核验的源码引用）", ""])
    for binding in findings:
        lines.append(_claim_markdown(binding, spec.repository, spec.commit))
    lines.extend(["", "## 5. 不确定的结论", ""])
    lines.extend([f"- {item}" for item in report.uncertainties] or ["- 无"])
    lines.extend(["", "## 6. 建议继续调查的方向", ""])
    lines.extend([f"- {item}" for item in report.next_steps] or ["- 无"])
    lines.extend(
        [
            "",
            "---",
            "",
            "> 机械核验只验证源码引用存在、逐字匹配并定位行号；"
            "不证明业务语义或架构推断本身正确。",
            "",
        ]
    )
    return "\n".join(lines).encode("utf-8")


def insufficient_report(reason: str, structure: str, uncertainties: list[str]) -> RepoAnalysisReport:
    return RepoAnalysisReport(
        conclusion=reason,
        structure=structure,
        key_files=[],
        call_chains=[],
        findings=[],
        uncertainties=uncertainties[:10],
        next_steps=[
            "补充更有针对性的符号、文件名或错误信息后重新发起分析",
            "确认目标代码是否位于当前固定的 commit 中",
        ],
    )


def render_partial(spec: AnalysisSpec, partial: PartialReport) -> bytes:
    lines = [
        "# RepoSage 仓库分析报告（未完成）",
        "",
        f"- Repository: `{spec.repository}`",
        f"- Commit: `{spec.commit}`",
        f"- 停止原因: `{partial.reason_code}`",
        f"- 任务: {spec.task}",
        "",
        "## 1. 问题结论",
        "",
        partial.conclusion,
        "",
        "## 2. 仓库总体结构",
        "",
        partial.structure,
        "",
        "## 3. 关键代码文件与调用关系",
        "",
        "（任务未完成，不能形成经核验的调用关系结论）",
        "",
        "## 4. 实际代码证据",
        "",
    ]
    if not partial.evidence:
        lines.extend(["（本轮没有已落库的源码证据）", ""])
    for observation in partial.evidence:
        lines.append(
            f"- `{observation.reference}` · Evidence `{observation.evidence_id}` · "
            f"Version `{observation.source_version}`"
        )
    lines.extend(["", "## 5. 不确定的结论", ""])
    lines.extend([f"- {item}" for item in partial.uncertainties] or ["- 任务未完成"])
    lines.extend(["", "## 6. 建议继续调查的方向", ""])
    lines.extend([f"- {item}" for item in partial.next_steps] or ["- 在获得新预算后继续调查"])
    lines.extend(
        [
            "",
            "---",
            "",
            "> 本报告是预算、上下文或期限耗尽时的已知结果，不是成功结论。",
            "",
        ]
    )
    return "\n".join(lines).encode("utf-8")


def partial_from_evidence(
    reason_code: str,
    evidence: list[ToolObservation],
    *,
    detail: str,
) -> PartialReport:
    known = [item for item in evidence if item.evidence_id is not None]
    return PartialReport(
        reason_code=reason_code,
        conclusion=(
            f"任务未完成：{detail}。以下仅列出本轮已经实际读取并落库的源码证据，"
            "不构成完整结论。"
        ),
        structure="尚未完成跨文件的总体结构判断。",
        evidence=known[:10],
        uncertainties=[f"停止原因：{reason_code}", detail],
        next_steps=[
            "提高本任务的模型、工具或费用预算后继续",
            "缩小问题范围（指定文件、符号或错误码）后重新分析",
        ],
    )
