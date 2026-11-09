"""Optional Semgrep adapter: no rules or no binary means an explicit skip (A02)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from mgar_v3.domain import DomainError
from mgar_v3.scanners.domain import (
    CAPABILITY_MISSING,
    OPTIONAL_NOT_CONFIGURED,
    OPTIONAL_TOOL_MISSING,
    SCAN_TIMEOUT,
    ResourceLimits,
    ScanContext,
    ScanFinding,
    ScanLocation,
    ScannerCoverage,
    ScannerSpec,
    ScanResult,
    Severity,
    ToolCapabilities,
    cap_findings,
    make_result,
    short,
)
from mgar_v3.scanners.sandbox import ScanRuntime, short_line


class SemgrepScanner:
    @property
    def spec(self) -> ScannerSpec:
        return self._spec

    def __init__(self, capabilities: ToolCapabilities, rules: Path | None = None):
        self.capabilities = capabilities
        self.rules = Path(rules) if rules is not None else None
        self._spec = ScannerSpec(
            id="semgrep",
            version="1",
            language="repository",
            required_capabilities=["semgrep", "rules"],
            timeout=240,
            resource_limits=ResourceLimits(cpu_seconds=240, memory_bytes=2_147_483_648),
            description="optional Semgrep rule adapter; disabled without local rules",
            optional=True,
        )

    def required(self, context: ScanContext) -> bool:
        return False

    def scan(self, context: ScanContext, runtime: ScanRuntime | None) -> ScanResult:
        considered = len(context.files)
        coverage = ScannerCoverage(files_considered=considered)
        if self.rules is None or not self.rules.exists():
            return make_result(
                self.spec,
                status="skipped",
                reason=OPTIONAL_NOT_CONFIGURED,
                required=False,
                coverage=coverage,
                diagnostics=["No Semgrep rule directory or file was configured"],
            )
        if self.capabilities.semgrep is None:
            return make_result(
                self.spec,
                status="skipped",
                reason=OPTIONAL_TOOL_MISSING,
                required=False,
                coverage=coverage,
            )
        if runtime is None:
            return make_result(
                self.spec, status="skipped", reason=CAPABILITY_MISSING, required=False,
                coverage=coverage,
            )
        try:
            result, mode = runtime.run(
                [
                    self.capabilities.semgrep,
                    "scan",
                    "--json",
                    "--config",
                    str(self.rules),
                    "--quiet",
                    "--no-git-ignore",
                    "--metrics=off",
                    "--disable-version-check",
                    ".",
                ],
                self.spec.timeout,
            )
        except DomainError as exc:
            return make_result(
                self.spec,
                status="failed",
                reason="SANDBOX_ERROR",
                required=False,
                coverage=coverage,
                diagnostics=[f"{exc.code}: {exc}"],
            )
        exit_codes = {"semgrep": result.exit_code}
        diagnostics = [
            f"semgrep mode={mode} exit={result.exit_code} duration={result.duration_seconds}s"
        ]
        if result.timed_out:
            return make_result(
                self.spec,
                status="timeout",
                reason=SCAN_TIMEOUT,
                required=False,
                coverage=coverage,
                diagnostics=diagnostics,
                exit_codes=exit_codes,
            )
        try:
            payload: Any = json.loads(result.stdout) if result.stdout.strip() else {}
        except ValueError:
            payload = None
        if not isinstance(payload, dict):
            return make_result(
                self.spec,
                status="failed",
                reason="SEMGREP_FAILED",
                required=False,
                coverage=coverage,
                diagnostics=[*diagnostics, short(short_line(result.combined()))],
                exit_codes=exit_codes,
            )
        findings: list[ScanFinding] = []
        results = payload.get("results")
        if isinstance(results, list):
            for item in results:
                if not isinstance(item, dict):
                    continue
                rule = item.get("check_id")
                extra = item.get("extra")
                message = extra.get("message") if isinstance(extra, dict) else None
                path = item.get("path")
                start = item.get("start")
                line = start.get("line") if isinstance(start, dict) else None
                severity = extra.get("severity") if isinstance(extra, dict) else None
                if not isinstance(rule, str) or not isinstance(message, str):
                    continue
                findings.append(
                    ScanFinding(
                        scanner_id=self.spec.id,
                        rule_id=short(rule, 200),
                        category="security",
                        severity=self._severity(severity),
                        message=short(message),
                        location=ScanLocation(
                            path=path if isinstance(path, str) else ".",
                            line=line if isinstance(line, int) else None,
                        ),
                    )
                )
        kept, capped = cap_findings(findings, self.spec.resource_limits.max_findings)
        version = self.capabilities.versions.get("semgrep")
        return make_result(
            self.spec,
            status="completed",
            findings=kept,
            required=False,
            coverage=ScannerCoverage(
                files_considered=considered,
                files_scanned=considered,
                notes=[f"mode={mode}", f"rules={self.rules.name}"],
            ),
            diagnostics=diagnostics,
            exit_codes=exit_codes,
            truncated=result.truncated or capped,
            tool_versions={"semgrep": version} if version else {},
        )

    @staticmethod
    def _severity(value: object) -> Severity:
        if isinstance(value, str) and value.upper() == "ERROR":
            return "error"
        if isinstance(value, str) and value.upper() == "INFO":
            return "info"
        return "warning"
