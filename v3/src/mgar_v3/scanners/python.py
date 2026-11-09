"""Python scanners: Ruff lint, bounded pytest and configured-only Pyright (A02)."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from mgar_v3.domain import DomainError
from mgar_v3.scanners.domain import (
    CAPABILITY_MISSING,
    NO_TESTS,
    OPTIONAL_NOT_CONFIGURED,
    PYTEST_ERROR,
    SCAN_TIMEOUT,
    ResourceLimits,
    ScanContext,
    ScanFinding,
    ScanLocation,
    ScannerCoverage,
    ScannerSpec,
    ScanResult,
    ToolCapabilities,
    cap_findings,
    make_result,
    short,
)
from mgar_v3.scanners.sandbox import ScanRuntime, short_line


def _python_files(context: ScanContext) -> list[str]:
    return [entry.path for entry in context.files if entry.path.endswith((".py", ".pyi"))]


def _relative(path: str, runtime: ScanRuntime | None) -> str:
    if runtime is None:
        return path
    try:
        return str(Path(path).resolve().relative_to(runtime.snapshot.path.resolve()))
    except (OSError, ValueError):
        return path


def _read_manifest_file(context: ScanContext, path: str) -> str | None:
    if path not in context.paths():
        return None
    source = context.worktree.joinpath(*path.split("/"))
    try:
        if source.is_symlink() or not source.is_file():
            return None
        raw = source.read_bytes()[:262_144]
    except OSError:
        return None
    return raw.decode("utf-8", "replace")


def _pyright_configured(context: ScanContext) -> bool:
    if "pyrightconfig.json" in context.paths():
        return True
    pyproject = _read_manifest_file(context, "pyproject.toml")
    return pyproject is not None and "[tool.pyright]" in pyproject


class RuffScanner:
    @property
    def spec(self) -> ScannerSpec:
        return self._spec

    def __init__(self, capabilities: ToolCapabilities):
        self.capabilities = capabilities
        self._spec = ScannerSpec(
            id="python-ruff",
            version="1",
            language="Python",
            required_capabilities=["ruff"],
            timeout=120,
            resource_limits=ResourceLimits(cpu_seconds=120, memory_bytes=2_147_483_648),
            description="Ruff lint over the isolated Python snapshot",
        )

    def required(self, context: ScanContext) -> bool:
        return "Python" in context.languages

    def scan(self, context: ScanContext, runtime: ScanRuntime | None) -> ScanResult:
        files = _python_files(context)
        coverage = ScannerCoverage(files_considered=len(files))
        if runtime is None or self.capabilities.ruff is None:
            return make_result(
                self.spec, status="skipped", reason=CAPABILITY_MISSING, coverage=coverage
            )
        try:
            result, mode = runtime.run(
                [
                    self.capabilities.ruff,
                    "check",
                    "--output-format",
                    "json",
                    "--no-cache",
                    ".",
                ],
                self.spec.timeout,
            )
        except DomainError as exc:
            return make_result(
                self.spec,
                status="failed",
                reason="SANDBOX_ERROR",
                coverage=coverage,
                diagnostics=[f"{exc.code}: {exc}"],
            )
        diagnostics = [
            f"ruff mode={mode} exit={result.exit_code} duration={result.duration_seconds}s"
        ]
        if result.timed_out:
            return make_result(
                self.spec,
                status="timeout",
                reason=SCAN_TIMEOUT,
                coverage=coverage,
                diagnostics=diagnostics,
                exit_codes={"ruff check": result.exit_code},
            )
        try:
            payload: Any = json.loads(result.stdout) if result.stdout.strip() else []
        except ValueError:
            payload = None
        if not isinstance(payload, list):
            return make_result(
                self.spec,
                status="failed",
                reason="RUFF_FAILED",
                coverage=coverage,
                diagnostics=[*diagnostics, short(short_line(result.combined()))],
                exit_codes={"ruff check": result.exit_code},
            )
        findings: list[ScanFinding] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            code = item.get("code")
            message = item.get("message")
            filename = item.get("filename")
            location = item.get("location")
            if not isinstance(code, str) or not isinstance(message, str):
                continue
            line = column = None
            end_line = None
            if isinstance(location, dict):
                raw_line = location.get("row")
                raw_column = location.get("column")
                raw_end = location.get("end_row")
                line = raw_line if isinstance(raw_line, int) else None
                column = raw_column if isinstance(raw_column, int) else None
                end_line = raw_end if isinstance(raw_end, int) else None
            findings.append(
                ScanFinding(
                    scanner_id=self.spec.id,
                    rule_id=code,
                    category="lint",
                    severity="error" if code[:1] in {"E", "F"} else "warning",
                    message=short(message),
                    location=ScanLocation(
                        path=_relative(filename, runtime) if isinstance(filename, str) else ".",
                        line=max(line, 1) if line else None,
                        column=max(column, 1) if column else None,
                        end_line=max(end_line, 1) if end_line else None,
                    ),
                )
            )
        kept, capped = cap_findings(findings, self.spec.resource_limits.max_findings)
        version = self.capabilities.versions.get("ruff")
        return make_result(
            self.spec,
            status="completed",
            findings=kept,
            coverage=ScannerCoverage(
                files_considered=len(files), files_scanned=len(files), notes=[f"mode={mode}"]
            ),
            diagnostics=diagnostics,
            exit_codes={"ruff check": result.exit_code},
            truncated=result.truncated or capped,
            tool_versions={"ruff": version} if version else {},
        )


class PytestScanner:
    @property
    def spec(self) -> ScannerSpec:
        return self._spec

    def __init__(self, capabilities: ToolCapabilities):
        self.capabilities = capabilities
        self._spec = ScannerSpec(
            id="python-pytest",
            version="1",
            language="Python",
            required_capabilities=["python", "pytest"],
            timeout=240,
            resource_limits=ResourceLimits(cpu_seconds=240, memory_bytes=2_147_483_648),
            description="Bounded pytest with JUnit parsing; no tests is skipped, never a pass",
        )

    def required(self, context: ScanContext) -> bool:
        return "Python" in context.languages

    def scan(self, context: ScanContext, runtime: ScanRuntime | None) -> ScanResult:
        files = _python_files(context)
        coverage = ScannerCoverage(files_considered=len(files))
        python = self.capabilities.python
        if runtime is None or python is None or self.capabilities.pytest is None:
            return make_result(
                self.spec, status="skipped", reason=CAPABILITY_MISSING, coverage=coverage
            )
        report = runtime.snapshot.path.parent / "pytest-junit.xml"
        try:
            result, mode = runtime.run(
                [
                    python,
                    "-m",
                    "pytest",
                    "-q",
                    "--no-header",
                    "--tb=short",
                    "-p",
                    "no:cacheprovider",
                    f"--junitxml={report}",
                    ".",
                ],
                self.spec.timeout,
            )
        except DomainError as exc:
            return make_result(
                self.spec,
                status="failed",
                reason="SANDBOX_ERROR",
                coverage=coverage,
                diagnostics=[f"{exc.code}: {exc}"],
            )
        diagnostics = [
            f"pytest mode={mode} exit={result.exit_code} duration={result.duration_seconds}s"
        ]
        exit_codes = {"pytest": result.exit_code}
        if result.timed_out:
            return make_result(
                self.spec,
                status="timeout",
                reason=SCAN_TIMEOUT,
                coverage=coverage,
                diagnostics=diagnostics,
                exit_codes=exit_codes,
            )
        if result.exit_code == 5:
            return make_result(
                self.spec,
                status="skipped",
                reason=NO_TESTS,
                coverage=ScannerCoverage(
                    files_considered=len(files), notes=["pytest collected no tests"]
                ),
                diagnostics=diagnostics,
                exit_codes=exit_codes,
            )
        if result.exit_code not in {0, 1}:
            return make_result(
                self.spec,
                status="failed",
                reason=PYTEST_ERROR,
                coverage=coverage,
                diagnostics=[*diagnostics, short(short_line(result.combined()))],
                exit_codes=exit_codes,
            )
        findings = self._junit_findings(report)
        kept, capped = cap_findings(findings, self.spec.resource_limits.max_findings)
        version = self.capabilities.versions.get("pytest")
        return make_result(
            self.spec,
            status="completed",
            findings=kept,
            coverage=ScannerCoverage(
                files_considered=len(files), files_scanned=len(files), notes=[f"mode={mode}"]
            ),
            diagnostics=diagnostics,
            exit_codes=exit_codes,
            truncated=result.truncated or capped,
            tool_versions={"pytest": version} if version else {},
        )

    def _junit_findings(self, report: Path) -> list[ScanFinding]:
        if not report.is_file():
            return []
        try:
            root = ET.parse(report).getroot()
        except (ET.ParseError, OSError):
            return []
        findings: list[ScanFinding] = []
        for case in root.iter("testcase"):
            for outcome in ("failure", "error"):
                node = case.find(outcome)
                if node is None:
                    continue
                name = case.get("name") or "unknown"
                classname = case.get("classname") or ""
                message = node.get("message") or (node.text or "")
                findings.append(
                    ScanFinding(
                        scanner_id=self.spec.id,
                        rule_id=short(f"pytest-{classname}.{name}".strip("."), 200),
                        category="test_failure",
                        severity="error",
                        message=short(message),
                    )
                )
        return findings


class PyrightScanner:
    @property
    def spec(self) -> ScannerSpec:
        return self._spec

    def __init__(self, capabilities: ToolCapabilities):
        self.capabilities = capabilities
        self._spec = ScannerSpec(
            id="python-pyright",
            version="1",
            language="Python",
            required_capabilities=["pyright"],
            timeout=180,
            resource_limits=ResourceLimits(cpu_seconds=180, memory_bytes=2_147_483_648),
            description="Type check only when the project already configures Pyright",
        )

    def required(self, context: ScanContext) -> bool:
        return "Python" in context.languages

    def scan(self, context: ScanContext, runtime: ScanRuntime | None) -> ScanResult:
        files = _python_files(context)
        coverage = ScannerCoverage(files_considered=len(files))
        if not _pyright_configured(context):
            return make_result(
                self.spec,
                status="skipped",
                reason=OPTIONAL_NOT_CONFIGURED,
                required=False,
                coverage=coverage,
                diagnostics=["No pyrightconfig.json or [tool.pyright] section"],
            )
        if runtime is None or self.capabilities.pyright is None:
            return make_result(
                self.spec, status="skipped", reason=CAPABILITY_MISSING, coverage=coverage
            )
        try:
            result, mode = runtime.run(
                [self.capabilities.pyright, "--outputjson", "."], self.spec.timeout
            )
        except DomainError as exc:
            return make_result(
                self.spec,
                status="failed",
                reason="SANDBOX_ERROR",
                coverage=coverage,
                diagnostics=[f"{exc.code}: {exc}"],
            )
        diagnostics = [
            f"pyright mode={mode} exit={result.exit_code} duration={result.duration_seconds}s"
        ]
        exit_codes = {"pyright": result.exit_code}
        if result.timed_out:
            return make_result(
                self.spec,
                status="timeout",
                reason=SCAN_TIMEOUT,
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
                reason="PYRIGHT_FAILED",
                coverage=coverage,
                diagnostics=[*diagnostics, short(short_line(result.combined()))],
                exit_codes=exit_codes,
            )
        findings: list[ScanFinding] = []
        diagnostics_raw = payload.get("generalDiagnostics")
        if isinstance(diagnostics_raw, list):
            for item in diagnostics_raw:
                if not isinstance(item, dict):
                    continue
                message = item.get("message")
                severity = item.get("severity")
                rule = item.get("rule")
                path = item.get("file")
                raw_range = item.get("range")
                start = raw_range.get("start") if isinstance(raw_range, dict) else None
                line = column = None
                if isinstance(start, dict):
                    raw_line = start.get("line")
                    raw_character = start.get("character")
                    line = raw_line + 1 if isinstance(raw_line, int) else None
                    column = raw_character + 1 if isinstance(raw_character, int) else None
                if not isinstance(message, str):
                    continue
                findings.append(
                    ScanFinding(
                        scanner_id=self.spec.id,
                        rule_id=str(rule) if isinstance(rule, str) else "pyright",
                        category="type",
                        severity="error" if severity == "error" else "warning",
                        message=short(message),
                        location=ScanLocation(
                            path=_relative(path, runtime) if isinstance(path, str) else ".",
                            line=line,
                            column=column,
                        ),
                    )
                )
        kept, capped = cap_findings(findings, self.spec.resource_limits.max_findings)
        version = self.capabilities.versions.get("pyright")
        return make_result(
            self.spec,
            status="completed",
            findings=kept,
            coverage=ScannerCoverage(
                files_considered=len(files), files_scanned=len(files), notes=[f"mode={mode}"]
            ),
            diagnostics=diagnostics,
            exit_codes=exit_codes,
            truncated=result.truncated or capped,
            tool_versions={"pyright": version} if version else {},
        )
