"""Scan contracts shared by every V3.3 language scanner (A02).

A scanner never returns free-form prose: results are typed, bounded and
serializable so a report can distinguish completed, skipped, unsupported,
timed out and failed coverage without over-claiming.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field

from mgar_v3.domain import Model
from mgar_v3.repository_analysis.workspace import FileEntry, Inventory, RepositoryManifest

SCAN_RESULT_SCHEMA = "v33-scan-result-v1"
SCAN_REPORT_SCHEMA = "v33-scan-report-v1"
DEFAULT_GO_IMAGE = "golang:1.27.1-bookworm"

ScanStatus = Literal["completed", "unsupported", "skipped", "timeout", "failed"]
Severity = Literal["error", "warning", "info"]

CAPABILITY_MISSING = "CAPABILITY_MISSING"
NO_SCANNER_FOR_LANGUAGE = "NO_SCANNER_FOR_LANGUAGE"
RACE_CHECK_DISABLED = "RACE_CHECK_DISABLED"
NO_TESTS = "NO_TESTS"
OPTIONAL_NOT_CONFIGURED = "OPTIONAL_NOT_CONFIGURED"
OPTIONAL_TOOL_MISSING = "OPTIONAL_TOOL_MISSING"
SCAN_TIMEOUT = "SCAN_TIMEOUT"
SCANNER_FAILED = "SCANNER_FAILED"
PYTEST_ERROR = "PYTEST_ERROR"

SUPPORTED_LANGUAGES = frozenset({"Go", "Python"})

# Source languages we deliberately report as unsupported instead of silently ignoring.
UNSUPPORTED_SOURCE_LANGUAGES = frozenset(
    {
        "C",
        "C++",
        "C#",
        "Dart",
        "Elixir",
        "Groovy",
        "Haskell",
        "Java",
        "JavaScript",
        "Kotlin",
        "Lua",
        "PHP",
        "Ruby",
        "Rust",
        "Scala",
        "Shell",
        "Svelte",
        "Swift",
        "TypeScript",
        "Vue",
    }
)

NON_SOURCE_LANGUAGES = frozenset(
    {
        "CMake",
        "CSS",
        "Docker",
        "HTML",
        "JSON",
        "Makefile",
        "Markdown",
        "Protocol Buffers",
        "SCSS",
        "SQL",
        "TOML",
        "XML",
        "YAML",
    }
)

_LANGUAGE_BY_EXTENSION: dict[str, str] = {
    ".c": "C",
    ".h": "C",
    ".cc": "C++",
    ".cpp": "C++",
    ".cxx": "C++",
    ".hpp": "C++",
    ".hh": "C++",
    ".cs": "C#",
    ".dart": "Dart",
    ".ex": "Elixir",
    ".exs": "Elixir",
    ".go": "Go",
    ".gradle": "Groovy",
    ".groovy": "Groovy",
    ".hs": "Haskell",
    ".java": "Java",
    ".js": "JavaScript",
    ".mjs": "JavaScript",
    ".cjs": "JavaScript",
    ".jsx": "JavaScript",
    ".kt": "Kotlin",
    ".kts": "Kotlin",
    ".lua": "Lua",
    ".php": "PHP",
    ".py": "Python",
    ".pyi": "Python",
    ".rb": "Ruby",
    ".rs": "Rust",
    ".scala": "Scala",
    ".sh": "Shell",
    ".bash": "Shell",
    ".svelte": "Svelte",
    ".swift": "Swift",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".mts": "TypeScript",
    ".cts": "TypeScript",
    ".vue": "Vue",
}


def file_language(path: str) -> str:
    """Classify one repository path for scanner coverage purposes."""

    return _LANGUAGE_BY_EXTENSION.get(Path(path).suffix.lower(), "Unknown")


def scan_languages(languages: Mapping[str, int]) -> tuple[list[str], list[str]]:
    """Split manifest language counts into supported and explicitly unsupported."""

    present = sorted(name for name, count in languages.items() if count > 0)
    supported = [name for name in present if name in SUPPORTED_LANGUAGES]
    unsupported = [name for name in present if name in UNSUPPORTED_SOURCE_LANGUAGES]
    return supported, unsupported


class ResourceLimits(Model):
    """Bounds a single scanner may consume; every command inherits them."""

    cpu_seconds: Annotated[int, Field(strict=True, ge=1, le=3_600)] = 240
    memory_bytes: Annotated[int, Field(strict=True, ge=1, le=8_589_934_592)] = 2_147_483_648
    max_output_bytes: Annotated[int, Field(strict=True, ge=1_000, le=16_777_216)] = 4_194_304
    max_findings: Annotated[int, Field(strict=True, ge=1, le=10_000)] = 2_000


class ScannerSpec(Model):
    """The contract every scanner must publish before it is allowed to run."""

    id: Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{1,40}$")]
    version: Annotated[str, Field(min_length=1, max_length=40)]
    language: Annotated[str, Field(min_length=1, max_length=40)]
    required_capabilities: list[str]
    timeout: Annotated[float, Field(gt=0, le=1_800)] = 180
    resource_limits: ResourceLimits
    output_schema: str = SCAN_RESULT_SCHEMA
    description: str = ""
    optional: bool = False


class ScanLocation(Model):
    path: Annotated[str, Field(min_length=1, max_length=400)]
    line: Annotated[int, Field(ge=1)] | None = None
    column: Annotated[int, Field(ge=1)] | None = None
    end_line: Annotated[int, Field(ge=1)] | None = None


class ScanFinding(Model):
    scanner_id: Annotated[str, Field(min_length=1, max_length=80)]
    rule_id: Annotated[str, Field(min_length=1, max_length=200)]
    category: Annotated[str, Field(min_length=1, max_length=60)]
    severity: Severity
    confidence: Annotated[float, Field(ge=0, le=1)] = 1.0
    message: Annotated[str, Field(min_length=1, max_length=1_000)]
    location: ScanLocation | None = None


class ScannerCoverage(Model):
    files_considered: Annotated[int, Field(ge=0)] = 0
    files_scanned: Annotated[int, Field(ge=0)] = 0
    notes: list[str] = Field(default_factory=list, max_length=20)


class ScanResult(Model):
    schema_version: str = SCAN_RESULT_SCHEMA
    scanner_id: str
    version: str
    language: str
    status: ScanStatus
    required: bool = True
    findings: list[ScanFinding] = Field(default_factory=list, max_length=10_000)
    diagnostics: list[str] = Field(default_factory=list, max_length=50)
    exit_codes: dict[str, int] = Field(default_factory=dict)
    coverage: ScannerCoverage = Field(default_factory=ScannerCoverage)
    reason: str | None = None
    truncated: bool = False
    tool_versions: dict[str, str] = Field(default_factory=dict)


class ScanReport(Model):
    schema_version: str = SCAN_REPORT_SCHEMA
    repository: str
    commit: str
    results: list[ScanResult]
    unsupported_languages: list[str]
    complete: bool
    coverage: ScannerCoverage
    tool_versions: dict[str, str] = Field(default_factory=dict)


class ToolCapabilities(Model):
    """Trusted host probe; tests may inject their own to stay hermetic."""

    docker: str | None = None
    docker_go_image: str = DEFAULT_GO_IMAGE
    docker_image_present: bool = False
    go: str | None = None
    ruff: str | None = None
    python: str | None = None
    pytest: str | None = None
    pyright: str | None = None
    semgrep: str | None = None
    versions: dict[str, str] = Field(default_factory=dict)

    @staticmethod
    def _binary(name: str) -> str | None:
        found = shutil.which(name)
        if found:
            return found
        candidate = Path(sys.executable).with_name(name)
        return str(candidate) if candidate.is_file() else None

    @staticmethod
    def _output(argv: Sequence[str], timeout: float = 15) -> str | None:
        try:
            result = subprocess.run(
                list(argv),
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode != 0:
            return None
        return result.stdout.strip()

    @classmethod
    def detect(cls, go_image: str = DEFAULT_GO_IMAGE) -> ToolCapabilities:
        docker = cls._binary("docker")
        image_present = False
        image_version: str | None = None
        if docker:
            probe = cls._output(
                [docker, "image", "inspect", go_image, "--format", "{{json .Config.Env}}"], 30
            )
            if probe is not None:
                image_present = True
                try:
                    entries: Any = json.loads(probe)
                except ValueError:
                    entries = []
                if isinstance(entries, list):
                    for item in entries:
                        if isinstance(item, str) and item.startswith("GOLANG_VERSION="):
                            image_version = item.split("=", 1)[1]
        go = cls._binary("go")
        ruff = cls._binary("ruff")
        pyright = cls._binary("pyright")
        semgrep = cls._binary("semgrep")
        python = sys.executable
        pytest_version = cls._output([python, "-m", "pytest", "--version"])
        pytest = python if pytest_version is not None else None
        versions: dict[str, str] = {"python": sys.version.split()[0]}
        if docker:
            server = cls._output([docker, "version", "--format", "{{.Server.Version}}"])
            if server:
                versions["docker"] = server.splitlines()[0]
        if go:
            go_version = cls._output([go, "version"])
            if go_version:
                versions["go"] = go_version.splitlines()[0]
        if ruff:
            ruff_version = cls._output([ruff, "--version"])
            if ruff_version:
                versions["ruff"] = ruff_version.splitlines()[0]
        if pytest_version:
            versions["pytest"] = pytest_version.splitlines()[0]
        if pyright:
            pyright_version = cls._output([pyright, "--version"])
            if pyright_version:
                versions["pyright"] = pyright_version.splitlines()[0]
        if semgrep:
            semgrep_version = cls._output([semgrep, "--version"], 30)
            if semgrep_version:
                versions["semgrep"] = semgrep_version.splitlines()[0]
        if image_version:
            versions[go_image] = image_version
        return cls(
            docker=docker,
            docker_go_image=go_image,
            docker_image_present=image_present,
            go=go,
            ruff=ruff,
            python=python,
            pytest=pytest,
            pyright=pyright,
            semgrep=semgrep,
            versions=versions,
        )


@dataclass(frozen=True)
class ScanContext:
    """One fixed workspace and its manifest, presented to every scanner."""

    repository: str
    commit: str
    worktree: Path
    files: tuple[FileEntry, ...]
    languages: dict[str, int]
    inventory: Inventory
    manifest_hash: str

    @classmethod
    def from_workspace(cls, worktree: Path, manifest: RepositoryManifest) -> ScanContext:
        return cls(
            repository=manifest.repository,
            commit=manifest.commit,
            worktree=Path(worktree),
            files=tuple(manifest.files),
            languages=dict(manifest.languages),
            inventory=manifest.inventory,
            manifest_hash=manifest.manifest_hash,
        )

    def paths(self) -> tuple[str, ...]:
        return tuple(entry.path for entry in self.files)

    def language(self, path: str) -> str:
        return file_language(path)


def make_result(
    spec: ScannerSpec,
    *,
    status: ScanStatus,
    findings: Sequence[ScanFinding] = (),
    coverage: ScannerCoverage | None = None,
    reason: str | None = None,
    required: bool = True,
    language: str | None = None,
    diagnostics: Sequence[str] = (),
    exit_codes: Mapping[str, int] | None = None,
    truncated: bool = False,
    tool_versions: Mapping[str, str] | None = None,
) -> ScanResult:
    return ScanResult(
        scanner_id=spec.id,
        version=spec.version,
        language=language or spec.language,
        status=status,
        required=required,
        findings=list(findings),
        diagnostics=list(diagnostics),
        exit_codes=dict(exit_codes or {}),
        coverage=coverage or ScannerCoverage(),
        reason=reason,
        truncated=truncated,
        tool_versions=dict(tool_versions or {}),
    )


def cap_findings(
    findings: Sequence[ScanFinding], maximum: int
) -> tuple[list[ScanFinding], bool]:
    """Keep the first ``maximum`` findings and disclose the truncation."""

    if len(findings) <= maximum:
        return list(findings), False
    return list(findings[:maximum]), True


def short(value: str, limit: int = 500) -> str:
    text = " ".join(value.strip().split())
    return text[:limit] if text else "No message was reported"
