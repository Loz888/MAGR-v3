"""V3.3 A02 scanner registry, sandbox and language scanner tests."""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

import pytest

from mgar_v3.domain import DomainError, digest
from mgar_v3.repository_analysis.workspace import FileEntry, Inventory, RepositoryWorkspace
from mgar_v3.scanners.domain import (
    NO_SCANNER_FOR_LANGUAGE,
    NO_TESTS,
    OPTIONAL_NOT_CONFIGURED,
    RACE_CHECK_DISABLED,
    SCAN_REPORT_SCHEMA,
    ScanContext,
    ScanFinding,
    ScannerCoverage,
    ScanReport,
    ScanResult,
    ToolCapabilities,
    cap_findings,
    file_language,
)
from mgar_v3.scanners.registry import ScannerRegistry
from mgar_v3.scanners.sandbox import CommandPolicy, materialize

GO_MANIFESTS = {"go.mod", "go.sum", "go.work", "go.work.sum"}
PY_MANIFESTS = {
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "pipfile",
    "pipfile.lock",
    "poetry.lock",
    "uv.lock",
    "pdm.lock",
}


def _blob_sha(content: bytes) -> str:
    return hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest()


def _write(root: Path, files: dict[str, str]) -> None:
    for relative, text in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


def _inventory(paths: list[str]) -> Inventory:
    manifests: list[str] = []
    ecosystems: set[str] = set()
    pipelines: list[str] = []
    for path in paths:
        name = path.rsplit("/", 1)[-1].lower()
        if name in GO_MANIFESTS:
            manifests.append(path)
            ecosystems.add("go")
        elif name in PY_MANIFESTS or (
            name.startswith("requirements") and name.endswith(".txt")
        ):
            manifests.append(path)
            ecosystems.add("python")
        if (
            path.startswith(".github/workflows/") and path.endswith((".yml", ".yaml"))
        ) or name in {".gitlab-ci.yml", "jenkinsfile"}:
            pipelines.append(path)
    directories = sorted(
        {
            path.split("/", 1)[0]
            for path in paths
            if "/" in path and path.split("/", 1)[0] in {"test", "tests", "__tests__", "spec"}
        }
    )
    return Inventory(
        dependency_manifests=sorted(manifests),
        ecosystems=sorted(ecosystems),
        ci_configs=sorted(pipelines),
        test_directories=directories,
    )


def _context(root: Path, files: dict[str, str]) -> ScanContext:
    _write(root, files)
    relative = sorted(files)
    entries = []
    languages: dict[str, int] = {}
    for path in relative:
        content = (root / path).read_bytes()
        entries.append(
            FileEntry(
                path=path,
                mode="100644",
                size=len(content),
                blob=_blob_sha(content),
            )
        )
        language = file_language(path)
        languages[language] = languages.get(language, 0) + len(content)
    return ScanContext(
        repository="octo/fixture",
        commit="a" * 40,
        worktree=root,
        files=tuple(entries),
        languages=languages,
        inventory=_inventory(relative),
        manifest_hash=digest({"paths": relative}),
    )


def _caps(**overrides: object) -> ToolCapabilities:
    values: dict[str, object] = {
        "docker": None,
        "docker_go_image": "golang:1.27.1-bookworm",
        "docker_image_present": False,
        "go": None,
        "ruff": None,
        "python": sys.executable,
        "pytest": None,
        "pyright": None,
        "semgrep": None,
        "versions": {},
    }
    values.update(overrides)
    return ToolCapabilities.model_validate(values)


def _binary(directory: Path, name: str, body: str) -> str:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text("#!/usr/bin/env bash\n" + body)
    path.chmod(0o755)
    return str(path)


def _installed(name: str) -> str | None:
    candidate = Path(sys.executable).with_name(name)
    if candidate.is_file():
        return str(candidate)
    found = subprocess.run(
        ["bash", "-lc", f"command -v {name}"], capture_output=True, text=True, check=False
    ).stdout.strip()
    return found or None


def _result(report: ScanReport, scanner_id: str) -> ScanResult:
    matches = [result for result in report.results if result.scanner_id == scanner_id]
    assert len(matches) == 1, f"expected one {scanner_id} result, found {len(matches)}"
    return matches[0]


def _tree_digest(root: Path) -> str:
    digest_value = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix()
        if relative.startswith(".git/"):
            continue
        digest_value.update(relative.encode() + b"\0")
        digest_value.update(path.read_bytes() + b"\0")
    return digest_value.hexdigest()


def _git(*args: str, cwd: Path) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=False)
    if result.returncode != 0:
        raise AssertionError(result.stderr.decode("utf-8", "replace"))
    return result.stdout.decode("utf-8", "replace").strip()


def _git_repository(root: Path, files: dict[str, str]) -> tuple[Path, str]:
    repo = root
    repo.mkdir(parents=True)
    _git("init", "--quiet", cwd=repo)
    _git("config", "user.email", "fixture@example.invalid", cwd=repo)
    _git("config", "user.name", "MGAR Fixture", cwd=repo)
    _git("config", "uploadpack.allowAnySHA1InWant", "true", cwd=repo)
    _git("config", "uploadpack.allowTipSHA1InWant", "true", cwd=repo)
    _write(repo, files)
    _git("add", "--all", cwd=repo)
    _git("commit", "--quiet", "-m", "scanner fixture", cwd=repo)
    return repo, _git("rev-parse", "HEAD", cwd=repo)


GO_FIXTURE = {
    "go.mod": "module example.com/orders\n\ngo 1.22\n",
    "main.go": (
        "package main\n\nimport \"fmt\"\n\nfunc main() {\n"
        "\tvar value string\n\tfmt.Printf(\"%d\", value)\n}\n"
    ),
    "orders.go": "package main\n\nfunc Add(a, b int) int { return a + b }\n",
    "orders_test.go": (
        "package main\n\nimport \"testing\"\n\n"
        "func TestAdd(t *testing.T) {\n\tif Add(1, 1) != 3 {\n"
        "\t\tt.Fatalf(\"unexpected sum\")\n\t}\n}\n"
    ),
}

PY_FIXTURE = {
    "app.py": "import os\n\n\ndef add(a, b):\n    return a + b\n",
    "tests/test_math.py": (
        "from app import add\n\n\ndef test_add():\n    assert add(1, 2) == 4\n"
    ),
}


def test_scanner_contracts_are_complete_and_unique() -> None:
    registry = ScannerRegistry(capabilities=_caps())
    specs = registry.specs()
    identifiers = [spec.id for spec in specs]
    assert identifiers == [
        "go",
        "go-race",
        "python-ruff",
        "python-pytest",
        "python-pyright",
        "dependency",
        "semgrep",
    ]
    assert len(set(identifiers)) == len(identifiers)
    for spec in specs:
        assert spec.version
        assert spec.language
        assert isinstance(spec.required_capabilities, list)
        if spec.id != "dependency":
            assert spec.required_capabilities
        assert spec.timeout > 0
        assert spec.output_schema == "v33-scan-result-v1"
        assert spec.resource_limits.cpu_seconds > 0
        assert spec.resource_limits.memory_bytes > 0
        assert spec.resource_limits.max_output_bytes > 0
    assert next(item for item in specs if item.id == "go-race").optional is True
    assert next(item for item in specs if item.id == "semgrep").optional is True


def test_unsupported_language_is_explicit_and_never_complete(tmp_path: Path) -> None:
    context = _context(
        tmp_path / "repo",
        {
            "Cargo.toml": "[package]\nname = \"engine\"\nversion = \"0.1.0\"\n",
            "Cargo.lock": "version = 3\n",
            "src/lib.rs": "pub fn run() -> usize { 0 }\n",
            "README.md": "# engine\n",
        },
    )
    report = ScannerRegistry(capabilities=_caps()).scan(context)
    assert report.complete is False
    assert report.unsupported_languages == ["Rust"]
    rust = _result(report, "language-rust")
    assert rust.status == "unsupported"
    assert rust.required is True
    assert rust.reason == NO_SCANNER_FOR_LANGUAGE
    completed_languages = {
        result.scanner_id for result in report.results if result.status == "completed"
    }
    assert completed_languages == {"dependency"}
    payload = report.model_dump(mode="json")
    assert payload["schema_version"] == SCAN_REPORT_SCHEMA
    assert ScanReport.model_validate(payload) == report


def test_missing_toolchains_are_reported_not_claimed(tmp_path: Path) -> None:
    context = _context(
        tmp_path / "repo",
        {
            "go.mod": "module example.com/orders\n\ngo 1.22\n",
            "main.go": "package main\n\nfunc main() {}\n",
            "pyproject.toml": "[project]\nname = \"orders\"\nversion = \"0.1.0\"\n",
            "app.py": "def run():\n    return 1\n",
        },
    )
    report = ScannerRegistry(capabilities=_caps()).scan(context)
    assert report.complete is False
    for scanner_id in ("go", "python-ruff", "python-pytest"):
        result = _result(report, scanner_id)
        assert result.status == "skipped"
        assert result.reason == "CAPABILITY_MISSING"
        assert result.required is True
        assert result.findings == []
    dependency = _result(report, "dependency")
    assert dependency.status == "completed"
    assert report.coverage.files_scanned == 2
    assert report.coverage.files_considered == len(context.files)


def test_docker_go_repository_yields_structured_findings(tmp_path: Path) -> None:
    capabilities = ToolCapabilities.detect()
    assert capabilities.docker is not None, "Docker CLI is required for the real Go gate"
    assert capabilities.docker_image_present, "Pinned Go image golang:1.27.1-bookworm is required"
    repo, commit = _git_repository(tmp_path / "fixture", GO_FIXTURE)
    workspace = RepositoryWorkspace(
        tmp_path / "workspace",
        repository="octo/fixture",
        commit=commit,
        remote=str(repo),
        run_id="run-a02",
    )
    manifest = workspace.prepare()
    context = ScanContext.from_workspace(workspace.worktree, manifest)
    before = _tree_digest(workspace.worktree)
    report = ScannerRegistry(
        capabilities=capabilities, sandbox_root=tmp_path / "sandbox"
    ).scan(context)
    go = _result(report, "go")
    assert go.status == "completed"
    assert go.required is True
    assert go.exit_codes["go vet"] != 0
    assert go.exit_codes["go test"] != 0
    assert any("mode=docker" in diagnostic for diagnostic in go.diagnostics)
    rules = {finding.rule_id for finding in go.findings}
    assert {"go-vet", "go-test"} <= rules
    vet = next(finding for finding in go.findings if finding.rule_id == "go-vet")
    assert vet.category == "vet"
    assert vet.location is not None
    assert vet.location.path.endswith("main.go")
    assert vet.location.line == 7
    assert _tree_digest(workspace.worktree) == before
    assert report.complete is True


def test_race_check_is_independent_and_opt_in(tmp_path: Path) -> None:
    fake_go = _binary(
        tmp_path / "bin",
        "go",
        """
case "$1" in
  vet|build) exit 0 ;;
  test)
    case "$*" in
      *-race*)
        echo '{"Action":"output","Test":"T","Output":"WARNING: DATA RACE\\n"}'
        echo '{"Action":"fail","Package":"orders","Test":"TestRace","Elapsed":1}'
        exit 1 ;;
      *)
        echo '{"Action":"pass","Package":"orders","Elapsed":0}'
        exit 0 ;;
    esac ;;
esac
exit 0
""",
    )
    context = _context(
        tmp_path / "repo",
        {
            "go.mod": "module example.com/orders\n\ngo 1.22\n",
            "orders.go": "package orders\n\nfunc Add(a, b int) int { return a + b }\n",
            "orders_test.go": (
                "package orders\n\nimport \"testing\"\n\n"
                "func TestAdd(t *testing.T) {}\n"
            ),
        },
    )
    capabilities = _caps(go=fake_go)
    registry = ScannerRegistry(capabilities=capabilities)
    plain = registry.scan(context)
    disabled = _result(plain, "go-race")
    assert disabled.status == "skipped"
    assert disabled.reason == RACE_CHECK_DISABLED
    assert disabled.required is False
    assert _result(plain, "go").status == "completed"
    raced = registry.scan(context, include_race=True)
    race = _result(raced, "go-race")
    assert race.status == "completed"
    assert race.required is True
    assert race.exit_codes["go test -race"] != 0
    assert [finding.rule_id for finding in race.findings] == ["go-race"]
    assert race.findings[0].category == "bug"
    assert any("mode=local" in item for item in race.diagnostics)
    specs = {spec.id: spec for spec in registry.specs()}
    assert specs["go"].timeout == 180
    assert specs["go-race"].timeout == 300
    assert specs["go-race"].resource_limits.cpu_seconds == 300


def test_ruff_and_pytest_report_real_findings_without_touching_source(tmp_path: Path) -> None:
    ruff = _installed("ruff")
    assert ruff is not None, "Ruff is required for the real lint gate"
    pytest_binary = Path(sys.executable).with_name("pytest")
    assert pytest_binary.is_file(), "pytest is required for the real test gate"
    context = _context(tmp_path / "repo", PY_FIXTURE)
    before = _tree_digest(context.worktree)
    report = ScannerRegistry(
        capabilities=_caps(ruff=ruff, python=sys.executable, pytest=sys.executable),
        sandbox_root=tmp_path / "sandbox",
    ).scan(context)
    lint = _result(report, "python-ruff")
    assert lint.status == "completed"
    assert any(finding.rule_id == "F401" for finding in lint.findings)
    assert next(finding for finding in lint.findings if finding.rule_id == "F401").category == (
        "lint"
    )
    tests = _result(report, "python-pytest")
    assert tests.status == "completed"
    assert any(finding.category == "test_failure" for finding in tests.findings)
    assert _result(report, "python-pyright").reason == OPTIONAL_NOT_CONFIGURED
    assert report.complete is True
    assert _tree_digest(context.worktree) == before
    assert not list(context.worktree.rglob("__pycache__"))
    assert not (context.worktree / ".pytest_cache").exists()


def test_pytest_without_tests_is_skipped_not_passed(tmp_path: Path) -> None:
    context = _context(tmp_path / "repo", {"app.py": "def run():\n    return 1\n"})
    report = ScannerRegistry(
        capabilities=_caps(
            ruff=_installed("ruff"),
            python=sys.executable,
            pytest=sys.executable,
        ),
        sandbox_root=tmp_path / "sandbox",
    ).scan(context)
    tests = _result(report, "python-pytest")
    assert tests.status == "skipped"
    assert tests.reason == NO_TESTS
    assert tests.findings == []
    assert report.complete is True


def test_pyright_runs_only_when_the_project_configures_it(tmp_path: Path) -> None:
    fake_pyright = _binary(
        tmp_path / "bin",
        "pyright",
        """
printf '%s' '{"generalDiagnostics":[{"file":"app.py","severity":"error",'
printf '%s\\n' '"message":"Type is not assignable","rule":"reportAssignmentType",'
printf '%s\\n' '"range":{"start":{"line":3,"character":4}}}]}'
exit 1
""",
    )
    configured = _context(
        tmp_path / "configured",
        {"pyrightconfig.json": "{}\n", "app.py": "value = 1\n"},
    )
    report = ScannerRegistry(capabilities=_caps(pyright=fake_pyright)).scan(configured)
    result = _result(report, "python-pyright")
    assert result.status == "completed"
    assert result.required is True
    finding = result.findings[0]
    assert finding.rule_id == "reportAssignmentType"
    assert finding.category == "type"
    assert finding.location is not None
    assert finding.location.line == 4
    assert finding.location.column == 5

    plain = _context(tmp_path / "plain", {"app.py": "value = 1\n"})
    report = ScannerRegistry(capabilities=_caps(pyright=fake_pyright)).scan(plain)
    optional = _result(report, "python-pyright")
    assert optional.status == "skipped"
    assert optional.reason == OPTIONAL_NOT_CONFIGURED
    assert optional.required is False


def test_dependency_and_ci_supply_chain_signals(tmp_path: Path) -> None:
    context = _context(
        tmp_path / "repo",
        {
            "go.mod": "module example.com/orders\n\ngo 1.22\n",
            "pyproject.toml": (
                "[project]\nname = \"orders\"\nversion = \"0.1.0\"\n"
                "dependencies = [\"httpx>=0.28\", \"requests==2.32.3\"]\n"
            ),
            "requirements.txt": "flask>=2.0\npytest==8.4.0\n",
            ".github/workflows/ci.yml": (
                "on: push\njobs:\n  build:\n    runs-on: ubuntu-latest\n    steps:\n"
                "      - uses: actions/checkout@v4\n"
                "      - run: curl -fsSL https://example.com/install.sh | bash\n"
            ),
        },
    )
    report = ScannerRegistry(capabilities=_caps()).scan(context)
    dependency = _result(report, "dependency")
    assert dependency.status == "completed"
    rules = {finding.rule_id for finding in dependency.findings}
    assert rules == {
        "DEP_LOCKFILE_MISSING",
        "DEP_UNPINNED",
        "CI_PIPE_SHELL",
        "CI_UNPINNED_ACTION",
    }
    pipe = next(
        finding for finding in dependency.findings if finding.rule_id == "CI_PIPE_SHELL"
    )
    assert pipe.severity == "error"
    assert pipe.location is not None and pipe.location.line == 7
    unpinned = [finding for finding in dependency.findings if finding.rule_id == "DEP_UNPINNED"]
    assert len(unpinned) == 2


def test_semgrep_adapter_requires_explicit_rules(tmp_path: Path) -> None:
    rules = tmp_path / "rules.yml"
    rules.write_text("rules: []\n")
    fake_semgrep = _binary(
        tmp_path / "bin",
        "semgrep",
        """
printf '%s' '{"results":[{"check_id":"python.lang.security.audit",'
printf '%s\\n' '"path":"app.py","start":{"line":2},'
printf '%s\\n' '"extra":{"message":"Review this call","severity":"ERROR"}}],"errors":[]}'
exit 0
""",
    )
    context = _context(tmp_path / "repo", {"app.py": "def run():\n    return 1\n"})
    report = ScannerRegistry(
        capabilities=_caps(semgrep=fake_semgrep), semgrep_rules=rules
    ).scan(context)
    semgrep = _result(report, "semgrep")
    assert semgrep.status == "completed"
    assert semgrep.required is False
    finding = semgrep.findings[0]
    assert finding.category == "security"
    assert finding.severity == "error"
    assert finding.rule_id == "python.lang.security.audit"
    assert finding.location is not None and finding.location.line == 2

    unprepared = ScannerRegistry(capabilities=_caps(semgrep=fake_semgrep)).scan(context)
    skipped = _result(unprepared, "semgrep")
    assert skipped.status == "skipped"
    assert skipped.reason == OPTIONAL_NOT_CONFIGURED
    assert skipped.required is False


def test_command_policy_rejects_unapproved_commands() -> None:
    policy = CommandPolicy()
    assert policy.check(["go", "vet", "./..."]) == ["go", "vet", "./..."]
    assert policy.check(["go", "build", "./..."])[1] == "build"
    assert policy.check(["ruff", "check", "--output-format", "json", "."])
    assert policy.check(["python", "-m", "pytest", "-q"])
    assert policy.check(["semgrep", "scan", "--config", "rules.yml"])
    assert policy.check(["pyright", "--outputjson", "."])
    denied = [
        ["pip", "install", "requests"],
        ["python", "-m", "pip", "install", "requests"],
        ["go", "get", "example.com/module"],
        ["go", "mod", "download"],
        ["bash", "-c", "rm -rf /"],
        ["rm", "-rf", "/"],
        ["go", "test", "-run", "a|b"],
        ["go", "vet", "$(id)"],
    ]
    for argv in denied:
        with pytest.raises(DomainError) as raised:
            policy.check(argv)
        assert raised.value.code == "SANDBOX_COMMAND_DENIED"


def test_snapshot_detects_a_changed_workspace(tmp_path: Path) -> None:
    context = _context(tmp_path / "repo", {"app.py": "value = 1\n"})
    snapshot = materialize(context, base=tmp_path / "snap")
    assert snapshot.files == 1
    assert (snapshot.path / "app.py").read_text() == "value = 1\n"
    (context.worktree / "app.py").write_text("value = 2\n")
    with pytest.raises(DomainError) as raised:
        materialize(context, base=tmp_path / "second")
    assert raised.value.code == "SANDBOX_SNAPSHOT_MISMATCH"


def test_finding_cap_and_result_serialization() -> None:
    findings = [
        ScanFinding(
            scanner_id="demo",
            rule_id=f"RULE-{index}",
            category="lint",
            severity="warning",
            message="Demo finding",
        )
        for index in range(5)
    ]
    kept, truncated = cap_findings(findings, 2)
    assert len(kept) == 2 and truncated is True
    result = ScanResult(
        scanner_id="demo",
        version="1",
        language="Go",
        status="completed",
        findings=kept,
        coverage=ScannerCoverage(files_considered=3, files_scanned=3),
        truncated=True,
    )
    report = ScanReport(
        repository="octo/fixture",
        commit="a" * 40,
        results=[result],
        unsupported_languages=[],
        complete=True,
        coverage=ScannerCoverage(files_considered=3, files_scanned=3),
    )
    assert ScanReport.model_validate_json(report.model_dump_json()) == report
