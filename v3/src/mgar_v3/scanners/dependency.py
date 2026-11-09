"""In-process dependency and CI configuration signals (A02).

This scanner executes nothing: it only reads manifest-listed files with a size
cap and reports lock-file, pinning and pipeline supply-chain risks.
"""

from __future__ import annotations

import re
import tomllib

from mgar_v3.scanners.domain import (
    ResourceLimits,
    ScanContext,
    ScanFinding,
    ScanLocation,
    ScannerCoverage,
    ScannerSpec,
    ScanResult,
    cap_findings,
    make_result,
    short,
)
from mgar_v3.scanners.sandbox import ScanRuntime

_MAX_BYTES = 262_144
_PYTHON_LOCKS = {"uv.lock", "poetry.lock", "pipfile.lock", "pdm.lock"}
_CI_SHELL = re.compile(r"(?:curl|wget)\s+[^|\n;]*\|\s*(?:sudo\s+)?(?:bash|sh)\b")
_CI_USES = re.compile(r"^[ \t]*(?:-[ \t]*)?uses:[ \t]*([^\s#]+)")
_SHA = re.compile(r"^[0-9a-f]{40}$")


def _read(context: ScanContext, path: str) -> str | None:
    source = context.worktree.joinpath(*path.split("/"))
    try:
        if source.is_symlink() or not source.is_file():
            return None
        raw = source.read_bytes()
    except OSError:
        return None
    if len(raw) > _MAX_BYTES:
        return None
    return raw.decode("utf-8", "replace")


def _line_of(text: str, needle: str) -> int:
    for index, line in enumerate(text.splitlines(), start=1):
        if needle in line:
            return index
    return 1


def _pinned(requirement: str) -> bool:
    stripped = requirement.split("#", 1)[0].strip()
    if not stripped or stripped.startswith("-") or "://" in stripped:
        return True
    without_marker = stripped.split(";", 1)[0]
    return any(operator in without_marker for operator in ("==", "===", "~="))


class DependencyScanner:
    @property
    def spec(self) -> ScannerSpec:
        return self._spec

    def __init__(self) -> None:
        self._spec = ScannerSpec(
            id="dependency",
            version="1",
            language="repository",
            required_capabilities=[],
            timeout=60,
            resource_limits=ResourceLimits(
                cpu_seconds=60, memory_bytes=536_870_912, max_output_bytes=1_048_576
            ),
            description="read-only lock-file, pinning and CI supply-chain signals",
        )

    def required(self, context: ScanContext) -> bool:
        return True

    def scan(self, context: ScanContext, runtime: ScanRuntime | None = None) -> ScanResult:
        manifests = [
            path for path in context.inventory.dependency_manifests if _exists(context, path)
        ]
        pipelines = [path for path in context.inventory.ci_configs if _exists(context, path)]
        findings: list[ScanFinding] = []
        diagnostics: list[str] = []
        findings.extend(self._lockfiles(context, manifests, diagnostics))
        findings.extend(self._pinning(context, diagnostics))
        findings.extend(self._pipelines(context, pipelines, diagnostics))
        kept, capped = cap_findings(findings, self.spec.resource_limits.max_findings)
        considered = len(manifests) + len(pipelines)
        return make_result(
            self.spec,
            status="completed",
            findings=kept,
            coverage=ScannerCoverage(
                files_considered=considered,
                files_scanned=considered,
                notes=[f"manifests={len(manifests)}", f"pipelines={len(pipelines)}"],
            ),
            diagnostics=diagnostics,
            truncated=capped,
        )

    def _lockfiles(
        self, context: ScanContext, manifests: list[str], diagnostics: list[str]
    ) -> list[ScanFinding]:
        findings: list[ScanFinding] = []
        names = {path.rsplit("/", 1)[-1].lower() for path in manifests}
        if "go.mod" in names and "go.sum" not in names:
            path = next(item for item in manifests if item.rsplit("/", 1)[-1].lower() == "go.mod")
            findings.append(
                ScanFinding(
                    scanner_id=self.spec.id,
                    rule_id="DEP_LOCKFILE_MISSING",
                    category="dependency",
                    severity="warning",
                    message="go.mod has no go.sum lock file; dependency bytes are unpinned",
                    location=ScanLocation(path=path),
                )
            )
        python_manifest = next(
            (
                item
                for item in manifests
                if item.rsplit("/", 1)[-1].lower() in {"pyproject.toml", "setup.py", "setup.cfg"}
            ),
            None,
        )
        if python_manifest is not None:
            has_lock = any(name in names for name in _PYTHON_LOCKS) or any(
                name.startswith("requirements") and name.endswith(".txt") for name in names
            )
            if not has_lock:
                findings.append(
                    ScanFinding(
                        scanner_id=self.spec.id,
                        rule_id="DEP_LOCKFILE_MISSING",
                        category="dependency",
                        severity="warning",
                        message="Python project declares dependencies without a lock file",
                        location=ScanLocation(path=python_manifest),
                    )
                )
        diagnostics.append(f"lock-check manifests={sorted(names)}")
        return findings

    def _pinning(self, context: ScanContext, diagnostics: list[str]) -> list[ScanFinding]:
        findings: list[ScanFinding] = []
        for path in context.paths():
            name = path.rsplit("/", 1)[-1].lower()
            if not (name.startswith("requirements") and name.endswith(".txt")):
                continue
            text = _read(context, path)
            if text is None:
                diagnostics.append(f"unreadable={path}")
                continue
            for index, line in enumerate(text.splitlines(), start=1):
                stripped = line.split("#", 1)[0].strip()
                if not stripped or stripped.startswith("-"):
                    continue
                if not _pinned(stripped):
                    findings.append(
                        ScanFinding(
                            scanner_id=self.spec.id,
                            rule_id="DEP_UNPINNED",
                            category="dependency",
                            severity="warning",
                            message=short(f"Requirement is not pinned: {stripped}", 200),
                            location=ScanLocation(path=path, line=index),
                        )
                    )
        pyproject = next(
            (path for path in context.paths() if path.rsplit("/", 1)[-1] == "pyproject.toml"),
            None,
        )
        if pyproject is not None:
            text = _read(context, pyproject)
            if text is None:
                diagnostics.append(f"unreadable={pyproject}")
                return findings
            try:
                payload = tomllib.loads(text)
            except tomllib.TOMLDecodeError:
                diagnostics.append(f"invalid-toml={pyproject}")
                return findings
            project = payload.get("project")
            dependencies = project.get("dependencies") if isinstance(project, dict) else None
            if isinstance(dependencies, list):
                for dependency in dependencies:
                    if isinstance(dependency, str) and not _pinned(dependency):
                        findings.append(
                            ScanFinding(
                                scanner_id=self.spec.id,
                                rule_id="DEP_UNPINNED",
                                category="dependency",
                                severity="warning",
                                message=short(f"Dependency is not pinned: {dependency}", 200),
                                location=ScanLocation(
                                    path=pyproject, line=_line_of(text, dependency)
                                ),
                            )
                        )
        return findings

    def _pipelines(
        self, context: ScanContext, pipelines: list[str], diagnostics: list[str]
    ) -> list[ScanFinding]:
        findings: list[ScanFinding] = []
        for path in pipelines:
            text = _read(context, path)
            if text is None:
                diagnostics.append(f"unreadable={path}")
                continue
            for index, line in enumerate(text.splitlines(), start=1):
                if _CI_SHELL.search(line):
                    findings.append(
                        ScanFinding(
                            scanner_id=self.spec.id,
                            rule_id="CI_PIPE_SHELL",
                            category="ci",
                            severity="error",
                            message="Pipeline pipes a downloaded script directly into a shell",
                            location=ScanLocation(path=path, line=index),
                        )
                    )
                match = _CI_USES.match(line)
                if match:
                    reference = match.group(1)
                    revision = reference.rsplit("@", 1)[-1] if "@" in reference else ""
                    if not _SHA.fullmatch(revision):
                        findings.append(
                            ScanFinding(
                                scanner_id=self.spec.id,
                                rule_id="CI_UNPINNED_ACTION",
                                category="ci",
                                severity="warning",
                                message=short(
                                    f"Action is not pinned to a commit SHA: {reference}", 200
                                ),
                                location=ScanLocation(path=path, line=index),
                            )
                        )
        diagnostics.append(f"pipeline-check files={len(pipelines)}")
        return findings


def _exists(context: ScanContext, path: str) -> bool:
    source = context.worktree.joinpath(*path.split("/"))
    return source.is_file() and not source.is_symlink()
