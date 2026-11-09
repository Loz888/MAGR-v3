"""Scanner Registry: typed contracts, explicit unsupported languages (A02)."""

from __future__ import annotations

import shutil
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from mgar_v3.scanners.dependency import DependencyScanner
from mgar_v3.scanners.domain import (
    DEFAULT_GO_IMAGE,
    NO_SCANNER_FOR_LANGUAGE,
    NO_TESTS,
    RACE_CHECK_DISABLED,
    SCANNER_FAILED,
    SUPPORTED_LANGUAGES,
    ResourceLimits,
    ScanContext,
    ScannerCoverage,
    ScannerSpec,
    ScanReport,
    ScanResult,
    ToolCapabilities,
    make_result,
    scan_languages,
)
from mgar_v3.scanners.go import GoRaceScanner, GoScanner
from mgar_v3.scanners.python import PyrightScanner, PytestScanner, RuffScanner
from mgar_v3.scanners.sandbox import (
    CommandPolicy,
    ScanRuntime,
    Snapshot,
    build_runtime,
    materialize,
)
from mgar_v3.scanners.semgrep import SemgrepScanner


class Scanner(Protocol):
    @property
    def spec(self) -> ScannerSpec: ...

    def required(self, context: ScanContext) -> bool: ...

    def scan(self, context: ScanContext, runtime: ScanRuntime | None) -> ScanResult: ...


class ScannerRegistry:
    """Runs every registered scanner and reports coverage honestly."""

    def __init__(
        self,
        *,
        capabilities: ToolCapabilities | None = None,
        go_image: str = DEFAULT_GO_IMAGE,
        semgrep_rules: Path | None = None,
        sandbox_root: Path | None = None,
        policy: CommandPolicy | None = None,
        extra_scanners: Sequence[Scanner] | None = None,
    ):
        self.capabilities = capabilities or ToolCapabilities.detect(go_image)
        self.go_image = self.capabilities.docker_go_image or DEFAULT_GO_IMAGE
        self.sandbox_root = Path(sandbox_root) if sandbox_root is not None else None
        self.policy = policy or CommandPolicy()
        self.scanners: list[Scanner] = [
            GoScanner(self.capabilities),
            GoRaceScanner(self.capabilities),
            RuffScanner(self.capabilities),
            PytestScanner(self.capabilities),
            PyrightScanner(self.capabilities),
            DependencyScanner(),
            SemgrepScanner(self.capabilities, semgrep_rules),
            *(extra_scanners or ()),
        ]

    def specs(self) -> list[ScannerSpec]:
        return [scanner.spec for scanner in self.scanners]

    def scan(self, context: ScanContext, *, include_race: bool = False) -> ScanReport:
        _, unsupported = scan_languages(context.languages)
        snapshot = materialize(context, base=self.sandbox_root)
        results: list[ScanResult] = []
        try:
            environment = self._environment(snapshot)
            docker = (
                self.capabilities.docker
                if self.capabilities.docker and self.capabilities.docker_image_present
                else None
            )
            for scanner in self.scanners:
                if isinstance(scanner, GoRaceScanner):
                    if "Go" not in context.languages:
                        continue
                    if not include_race:
                        results.append(
                            make_result(
                                scanner.spec,
                                status="skipped",
                                reason=RACE_CHECK_DISABLED,
                                required=False,
                                coverage=ScannerCoverage(
                                    files_considered=self._language_files(context, "Go")
                                ),
                                diagnostics=["Race check was not requested"],
                            )
                        )
                        continue
                elif not self._should_run(scanner.spec, context):
                    continue
                runtime = build_runtime(
                    snapshot,
                    limits=scanner.spec.resource_limits,
                    policy=self.policy,
                    docker=docker,
                    docker_image=self.go_image if docker is not None else None,
                    environment=environment,
                )
                results.append(self._run(scanner, context, runtime))
            for language in unsupported:
                results.append(self._unsupported(context, language))
        finally:
            shutil.rmtree(snapshot.path.parent, ignore_errors=True)
        return ScanReport(
            repository=context.repository,
            commit=context.commit,
            results=results,
            unsupported_languages=unsupported,
            complete=self._complete(results, unsupported),
            coverage=self._coverage(context, results),
            tool_versions=self._tool_versions(results),
        )

    def _environment(self, snapshot: Snapshot) -> dict[str, str]:
        go_cache = snapshot.path.parent / "go-cache"
        go_cache.mkdir(parents=True, exist_ok=True)
        return {
            "PYTHONPATH": str(snapshot.path),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONHASHSEED": "0",
            "GOCACHE": str(go_cache),
            "GOFLAGS": "-mod=readonly",
            "GOPROXY": "off",
            "GOSUMDB": "off",
            "GOTOOLCHAIN": "local",
            "GOMAXPROCS": "1",
        }

    @staticmethod
    def _should_run(spec: ScannerSpec, context: ScanContext) -> bool:
        if spec.language in SUPPORTED_LANGUAGES:
            return spec.language in context.languages
        return True

    def _run(self, scanner: Scanner, context: ScanContext, runtime: ScanRuntime) -> ScanResult:
        try:
            return scanner.scan(context, runtime)
        except Exception as exc:  # noqa: BLE001 - a scanner crash must not hide coverage
            return make_result(
                scanner.spec,
                status="failed",
                reason=SCANNER_FAILED,
                coverage=ScannerCoverage(
                    files_considered=self._language_files(context, scanner.spec.language)
                ),
                diagnostics=[f"{type(exc).__name__}: {exc}"],
            )

    def _unsupported(self, context: ScanContext, language: str) -> ScanResult:
        identifier = language.lower().replace("+", "p").replace("#", "s").replace(" ", "-")
        spec = ScannerSpec(
            id=f"language-{identifier}",
            version="0",
            language=language,
            required_capabilities=[],
            timeout=1,
            resource_limits=ResourceLimits(cpu_seconds=1),
        )
        return make_result(
            spec,
            status="unsupported",
            reason=NO_SCANNER_FOR_LANGUAGE,
            required=True,
            coverage=ScannerCoverage(files_considered=self._language_files(context, language)),
            diagnostics=[f"No V3.3 scanner is registered for {language}"],
        )

    @staticmethod
    def _language_files(context: ScanContext, language: str) -> int:
        suffixes = {
            "Go": (".go",),
            "Python": (".py", ".pyi"),
            "Rust": (".rs",),
            "JavaScript": (".js", ".mjs", ".cjs", ".jsx"),
            "TypeScript": (".ts", ".tsx", ".mts", ".cts"),
            "Java": (".java",),
            "Kotlin": (".kt", ".kts"),
            "C": (".c", ".h"),
            "C++": (".cc", ".cpp", ".cxx", ".hpp", ".hh"),
            "C#": (".cs",),
            "Ruby": (".rb",),
            "PHP": (".php",),
            "Scala": (".scala",),
            "Swift": (".swift",),
            "Dart": (".dart",),
            "Elixir": (".ex", ".exs"),
            "Haskell": (".hs",),
            "Lua": (".lua",),
            "Shell": (".sh", ".bash"),
            "Groovy": (".groovy", ".gradle"),
            "Vue": (".vue",),
            "Svelte": (".svelte",),
        }.get(language)
        if suffixes is None:
            return 0
        return sum(1 for entry in context.files if entry.path.endswith(suffixes))

    @staticmethod
    def _complete(results: Sequence[ScanResult], unsupported: Sequence[str]) -> bool:
        if unsupported:
            return False
        for result in results:
            if not result.required:
                continue
            if result.status == "completed":
                continue
            if result.status == "skipped" and result.reason == NO_TESTS:
                continue
            return False
        return True

    def _coverage(self, context: ScanContext, results: Sequence[ScanResult]) -> ScannerCoverage:
        go_files = self._language_files(context, "Go")
        py_files = self._language_files(context, "Python")
        go_ready = "Go" in context.languages and self._language_ready(results, "go")
        py_ready = "Python" in context.languages and self._language_ready(results, "python")
        dependency = next((result for result in results if result.scanner_id == "dependency"), None)
        manifests = dependency.coverage.files_scanned if dependency is not None else 0
        scanned = (go_files if go_ready else 0) + (py_files if py_ready else 0) + manifests
        notes = [
            f"go={'scanned' if go_ready else 'not-scanned'}",
            f"python={'scanned' if py_ready else 'not-scanned'}",
        ]
        return ScannerCoverage(
            files_considered=len(context.files),
            files_scanned=min(scanned, len(context.files)),
            notes=notes,
        )

    @staticmethod
    def _language_ready(results: Sequence[ScanResult], prefix: str) -> bool:
        relevant = [
            result
            for result in results
            if result.required and result.scanner_id.startswith(prefix)
        ]
        if not relevant:
            return False
        for result in relevant:
            if result.status == "completed":
                continue
            if result.status == "skipped" and result.reason == NO_TESTS:
                continue
            return False
        return True

    def _tool_versions(self, results: Sequence[ScanResult]) -> dict[str, str]:
        versions = dict(self.capabilities.versions)
        for result in results:
            versions.update(result.tool_versions)
        return versions
