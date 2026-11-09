"""Go scanners: vet, compile check, controlled tests and isolated race check (A02)."""

from __future__ import annotations

import json
import re
from typing import Any

from mgar_v3.domain import DomainError
from mgar_v3.scanners.domain import (
    CAPABILITY_MISSING,
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
from mgar_v3.scanners.sandbox import CommandResult, ScanRuntime, short_line

_VET_LINE = re.compile(r"^(?P<path>.+?\.go):(?P<line>\d+):(?P<column>\d+): (?P<message>.+)$")
_BUILD_LINE = re.compile(
    r"^(?P<path>.+?\.go):(?P<line>\d+)(?::(?P<column>\d+))?: (?P<message>.+)$"
)
_SOURCE_LOCATION = re.compile(r"(?P<path>[\w./-]+\.go):(?P<line>\d+):(?P<column>\d+):")


def _count_go(context: ScanContext) -> int:
    return sum(1 for entry in context.files if entry.path.endswith(".go"))


def _location(path: str, line: int, column: int | None = None) -> ScanLocation:
    return ScanLocation(path=path, line=line, column=column)


def _parse_vet(output: str, spec: ScannerSpec) -> list[ScanFinding]:
    findings: list[ScanFinding] = []
    for raw in output.splitlines():
        match = _VET_LINE.match(raw.strip())
        if not match:
            continue
        findings.append(
            ScanFinding(
                scanner_id=spec.id,
                rule_id="go-vet",
                category="vet",
                severity="warning",
                message=short(match.group("message")),
                location=_location(
                    match.group("path"), int(match.group("line")), int(match.group("column"))
                ),
            )
        )
    return findings


def _parse_build(output: str, spec: ScannerSpec) -> list[ScanFinding]:
    findings: list[ScanFinding] = []
    for raw in output.splitlines():
        match = _BUILD_LINE.match(raw.strip())
        if not match:
            continue
        column = match.group("column")
        findings.append(
            ScanFinding(
                scanner_id=spec.id,
                rule_id="go-build",
                category="compile_error",
                severity="error",
                message=short(match.group("message")),
                location=_location(
                    match.group("path"),
                    int(match.group("line")),
                    int(column) if column else None,
                ),
            )
        )
    return findings


def _test_location(text: str) -> ScanLocation | None:
    match = _SOURCE_LOCATION.search(text)
    if not match:
        return None
    return _location(match.group("path"), int(match.group("line")), int(match.group("column")))


def _parse_test_json(output: str, spec: ScannerSpec) -> list[ScanFinding]:
    findings: list[ScanFinding] = []
    build_output: dict[str, list[str]] = {}
    test_output: dict[str, list[str]] = {}
    failed_tests: list[str] = []
    failed_builds: list[str] = []
    for raw in output.splitlines():
        try:
            event: Any = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        action = event.get("Action")
        package = event.get("ImportPath")
        test = event.get("Test")
        line = event.get("Output")
        if not isinstance(package, str):
            package = ""
        if action == "build-output" and isinstance(line, str):
            build_output.setdefault(package, []).append(line)
        elif action == "build-fail":
            failed_builds.append(package)
        elif isinstance(test, str) and isinstance(line, str) and action == "output":
            test_output.setdefault(test, []).append(line)
        elif isinstance(test, str) and action == "fail" and test not in failed_tests:
            failed_tests.append(test)
    for package in failed_builds:
        lines = [item for item in build_output.get(package, []) if item.strip()]
        text = "".join(lines[-3:]) if lines else f"Package {package or 'unknown'} failed to build"
        findings.append(
            ScanFinding(
                scanner_id=spec.id,
                rule_id="go-build",
                category="compile_error",
                severity="error",
                message=short(text),
                location=_test_location("".join(lines)),
            )
        )
    for test in failed_tests:
        lines = [item for item in test_output.get(test, []) if item.strip()]
        interesting = [item for item in lines if "FAIL" in item or "panic" in item]
        text = (interesting[-1] if interesting else (lines[-1] if lines else f"{test} failed"))
        findings.append(
            ScanFinding(
                scanner_id=spec.id,
                rule_id="go-test",
                category="test_failure",
                severity="error",
                message=short(text),
                location=_test_location("".join(lines)),
            )
        )
    return findings


def _truncated_output(result: CommandResult) -> bool:
    return result.truncated


class _GoBase:
    def __init__(self, capabilities: ToolCapabilities):
        self.capabilities = capabilities

    def _tool_versions(self, mode: str) -> dict[str, str]:
        if mode == "docker":
            version = self.capabilities.versions.get(self.capabilities.docker_go_image)
            return {self.capabilities.docker_go_image: version} if version else {}
        version = self.capabilities.versions.get("go")
        return {"go": version} if version else {}

    def _ready(self) -> bool:
        docker_ready = (
            self.capabilities.docker is not None and self.capabilities.docker_image_present
        )
        return docker_ready or self.capabilities.go is not None


class GoScanner(_GoBase):
    @property
    def spec(self) -> ScannerSpec:
        return self._spec

    def __init__(self, capabilities: ToolCapabilities):
        super().__init__(capabilities)
        self._spec = ScannerSpec(
            id="go",
            version="1",
            language="Go",
            required_capabilities=["go-or-docker-go-image"],
            timeout=180,
            resource_limits=ResourceLimits(cpu_seconds=180, memory_bytes=2_147_483_648),
            description="go vet, go build and bounded go test for the fixed revision",
        )

    def required(self, context: ScanContext) -> bool:
        return "Go" in context.languages

    def scan(self, context: ScanContext, runtime: ScanRuntime | None) -> ScanResult:
        considered = _count_go(context)
        coverage = ScannerCoverage(files_considered=considered)
        if runtime is None or not self._ready():
            return make_result(
                self.spec, status="skipped", reason=CAPABILITY_MISSING, coverage=coverage
            )
        prefer_docker = (
            self.capabilities.docker is not None
            and self.capabilities.docker_image_present
            and runtime.docker is not None
        )
        findings: list[ScanFinding] = []
        diagnostics: list[str] = []
        exit_codes: dict[str, int] = {}
        truncated = False
        mode = "docker" if prefer_docker else "local"
        go_binary = "go" if prefer_docker else (self.capabilities.go or "go")
        try:
            vet = runtime.run(
                [go_binary, "vet", "./..."], self.spec.timeout, prefer_docker=prefer_docker
            )
            vet_result, mode = vet
            exit_codes["go vet"] = vet_result.exit_code
            diagnostics.append(
                f"go vet mode={mode} exit={vet_result.exit_code} "
                f"duration={vet_result.duration_seconds}s"
            )
            truncated = truncated or _truncated_output(vet_result)
            if vet_result.timed_out:
                return make_result(
                    self.spec,
                    status="timeout",
                    reason=SCAN_TIMEOUT,
                    coverage=coverage,
                    diagnostics=diagnostics,
                    exit_codes=exit_codes,
                    tool_versions=self._tool_versions(mode),
                )
            if vet_result.exit_code != 0:
                findings.extend(_parse_vet(vet_result.combined(), self.spec))
            build, mode = runtime.run(
                [go_binary, "build", "./..."], self.spec.timeout, prefer_docker=prefer_docker
            )
            exit_codes["go build"] = build.exit_code
            diagnostics.append(
                f"go build mode={mode} exit={build.exit_code} duration={build.duration_seconds}s"
            )
            truncated = truncated or _truncated_output(build)
            if build.timed_out:
                return make_result(
                    self.spec,
                    status="timeout",
                    reason=SCAN_TIMEOUT,
                    coverage=coverage,
                    diagnostics=diagnostics,
                    exit_codes=exit_codes,
                    tool_versions=self._tool_versions(mode),
                )
            if build.exit_code != 0:
                parsed_build = _parse_build(build.combined(), self.spec)
                if parsed_build:
                    findings.extend(parsed_build)
                else:
                    findings.append(
                        ScanFinding(
                            scanner_id=self.spec.id,
                            rule_id="go-build",
                            category="compile_error",
                            severity="error",
                            message=short(
                                short_line(build.combined()) or "go build failed"
                            ),
                        )
                    )
            tests, mode = runtime.run(
                [go_binary, "test", "-json", "-vet=off", "./..."],
                self.spec.timeout,
                prefer_docker=prefer_docker,
            )
            exit_codes["go test"] = tests.exit_code
            diagnostics.append(
                f"go test mode={mode} exit={tests.exit_code} duration={tests.duration_seconds}s"
            )
            truncated = truncated or _truncated_output(tests)
            if tests.timed_out:
                return make_result(
                    self.spec,
                    status="timeout",
                    reason=SCAN_TIMEOUT,
                    coverage=coverage,
                    diagnostics=diagnostics,
                    exit_codes=exit_codes,
                    tool_versions=self._tool_versions(mode),
                )
            if tests.exit_code != 0:
                parsed_tests = _parse_test_json(tests.stdout, self.spec)
                if parsed_tests:
                    findings.extend(parsed_tests)
                else:
                    findings.append(
                        ScanFinding(
                            scanner_id=self.spec.id,
                            rule_id="go-test",
                            category="test_failure",
                            severity="error",
                            message=short(short_line(tests.combined()) or "go test failed"),
                        )
                    )
        except DomainError as exc:
            diagnostics.append(f"{exc.code}: {exc}")
            return make_result(
                self.spec,
                status="failed",
                reason="SANDBOX_ERROR",
                coverage=coverage,
                diagnostics=diagnostics,
                exit_codes=exit_codes,
            )
        kept, capped = cap_findings(findings, self.spec.resource_limits.max_findings)
        return make_result(
            self.spec,
            status="completed",
            findings=kept,
            coverage=ScannerCoverage(
                files_considered=considered,
                files_scanned=considered,
                notes=[f"mode={mode}", "executed=3"],
            ),
            diagnostics=diagnostics,
            exit_codes=exit_codes,
            truncated=truncated or capped,
            tool_versions=self._tool_versions(mode),
        )


class GoRaceScanner(_GoBase):
    """An independent, more expensive data-race check with its own budget."""

    @property
    def spec(self) -> ScannerSpec:
        return self._spec

    def __init__(self, capabilities: ToolCapabilities):
        super().__init__(capabilities)
        self._spec = ScannerSpec(
            id="go-race",
            version="1",
            language="Go",
            required_capabilities=["go-or-docker-go-image", "cgo-toolchain"],
            timeout=300,
            resource_limits=ResourceLimits(
                cpu_seconds=300, memory_bytes=4_294_967_296, max_output_bytes=8_388_608
            ),
            description="bounded go test -race executed only when explicitly requested",
            optional=True,
        )

    def required(self, context: ScanContext) -> bool:
        return "Go" in context.languages

    def scan(self, context: ScanContext, runtime: ScanRuntime | None) -> ScanResult:
        considered = _count_go(context)
        coverage = ScannerCoverage(files_considered=considered)
        if runtime is None or not self._ready():
            return make_result(
                self.spec,
                status="skipped",
                reason=CAPABILITY_MISSING,
                required=True,
                coverage=coverage,
            )
        prefer_docker = (
            self.capabilities.docker is not None
            and self.capabilities.docker_image_present
            and runtime.docker is not None
        )
        mode = "docker" if prefer_docker else "local"
        diagnostics: list[str] = []
        exit_codes: dict[str, int] = {}
        go_binary = "go" if prefer_docker else (self.capabilities.go or "go")
        try:
            result, mode = runtime.run(
                [go_binary, "test", "-race", "-json", "-vet=off", "./..."],
                self.spec.timeout,
                prefer_docker=prefer_docker,
            )
        except DomainError as exc:
            return make_result(
                self.spec,
                status="failed",
                reason="SANDBOX_ERROR",
                coverage=coverage,
                diagnostics=[f"{exc.code}: {exc}"],
            )
        exit_codes["go test -race"] = result.exit_code
        diagnostics.append(
            f"go test -race mode={mode} exit={result.exit_code} "
            f"duration={result.duration_seconds}s"
        )
        if result.timed_out:
            return make_result(
                self.spec,
                status="timeout",
                reason=SCAN_TIMEOUT,
                coverage=coverage,
                diagnostics=diagnostics,
                exit_codes=exit_codes,
                tool_versions=self._tool_versions(mode),
            )
        findings: list[ScanFinding] = []
        if "WARNING: DATA RACE" in result.combined():
            findings = self._race_findings(result.stdout)
        if result.exit_code != 0 and not findings:
            return make_result(
                self.spec,
                status="failed",
                reason="RACE_CHECK_FAILED",
                coverage=coverage,
                diagnostics=diagnostics,
                exit_codes=exit_codes,
                tool_versions=self._tool_versions(mode),
            )
        kept, capped = cap_findings(findings, self.spec.resource_limits.max_findings)
        return make_result(
            self.spec,
            status="completed",
            findings=kept,
            coverage=ScannerCoverage(
                files_considered=considered,
                files_scanned=considered,
                notes=[f"mode={mode}", "race=observed"],
            ),
            diagnostics=diagnostics,
            exit_codes=exit_codes,
            truncated=result.truncated or capped,
            tool_versions=self._tool_versions(mode),
        )

    def _race_findings(self, output: str) -> list[ScanFinding]:
        findings: list[ScanFinding] = []
        current: str | None = None
        for raw in output.splitlines():
            try:
                event: Any = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            test = event.get("Test")
            line = event.get("Output")
            if isinstance(test, str):
                current = test
            if isinstance(line, str) and "WARNING: DATA RACE" in line:
                findings.append(
                    ScanFinding(
                        scanner_id=self.spec.id,
                        rule_id="go-race",
                        category="bug",
                        severity="error",
                        confidence=0.95,
                        message=short(f"Data race detected in {current or 'package tests'}"),
                        location=_test_location(line),
                    )
                )
        if not findings and "WARNING: DATA RACE" in output:
            findings.append(
                ScanFinding(
                    scanner_id=self.spec.id,
                    rule_id="go-race",
                    category="bug",
                    severity="error",
                    confidence=0.95,
                    message="Data race detected by the race detector",
                )
            )
        return findings
