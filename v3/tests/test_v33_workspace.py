"""V3.3 A01 workspace and manifest tests (no network, no PostgreSQL)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from mgar_v3.domain import DomainError
from mgar_v3.repository_analysis.workspace import (
    MANIFEST_VERSION,
    RepositoryManifest,
    RepositoryWorkspace,
    WorkspaceLimits,
    _TreeEntry,
    validate_manifest_path,
)

GO_FILES = {
    "README.md": "# Orders\n",
    "Makefile": "build:\n\tgo build ./...\n",
    "go.mod": "module example.com/orders\n\ngo 1.22\n",
    "main.go": "package main\n\nfunc main() {}\n",
    "pkg/orders.go": "package orders\n\nfunc Total() int { return 0 }\n",
    "pkg/orders_test.go": (
        "package orders\n\nimport \"testing\"\n\nfunc TestTotal(t *testing.T) {}\n"
    ),
    "tests/smoke_test.go": (
        "package tests\n\nimport \"testing\"\n\nfunc TestSmoke(t *testing.T) {}\n"
    ),
    ".github/workflows/ci.yml": "name: ci\non: [push]\n",
}

PY_FILES = {
    "pyproject.toml": "[project]\nname = \"service\"\n",
    "package.json": '{"name": "web"}\n',
    "src/app/__init__.py": "VERSION = \"1\"\n",
    "src/app/core.py": "def run():\n    return 1\n",
    "tests/test_core.py": "from app.core import run\n",
    "docs/guide.md": "# Guide\n",
    ".gitlab-ci.yml": "stages: [test]\n",
}


def git(*args: str, cwd: Path | None = None, stdin: bytes | None = None) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, input=stdin, capture_output=True, check=False
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr.decode("utf-8", "replace"))
    return result.stdout.decode("utf-8", "replace").strip()


def build_repo(root: Path, name: str, files: dict[str, str]) -> tuple[Path, str]:
    repo = root / name
    repo.mkdir(parents=True)
    git("init", "--quiet", cwd=repo)
    git("config", "user.email", "fixture@example.invalid", cwd=repo)
    git("config", "user.name", "MGAR Fixture", cwd=repo)
    git("config", "uploadpack.allowAnySHA1InWant", "true", cwd=repo)
    git("config", "uploadpack.allowTipSHA1InWant", "true", cwd=repo)
    for relative, text in files.items():
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    git("add", "--all", cwd=repo)
    git("commit", "--quiet", "-m", "fixture baseline", cwd=repo)
    return repo, git("rev-parse", "HEAD", cwd=repo)


def workspace(
    root: Path, repo: Path, commit: str, *, run_id: str, limits: WorkspaceLimits | None = None
) -> RepositoryWorkspace:
    return RepositoryWorkspace(
        root,
        repository="octo/fixture",
        commit=commit,
        remote=str(repo),
        run_id=run_id,
        limits=limits,
    )


def test_different_repository_shapes_produce_stable_manifests(tmp_path: Path) -> None:
    go_repo, go_commit = build_repo(tmp_path / "src", "orders", GO_FILES)
    py_repo, py_commit = build_repo(tmp_path / "src", "service", PY_FILES)

    go_manifest = workspace(tmp_path / "ws-go", go_repo, go_commit, run_id="run-a01").prepare()
    py_manifest = workspace(tmp_path / "ws-py", py_repo, py_commit, run_id="run-a01").prepare()

    assert go_manifest.manifest_version == MANIFEST_VERSION
    assert go_manifest.manifest_hash == go_manifest.computed_hash()
    assert go_manifest.repository == "octo/fixture"
    assert go_manifest.commit == go_commit
    assert go_manifest.git.head == go_commit
    assert go_manifest.git.ref == "refs/heads/mgar-workspace"
    assert go_manifest.git.clean is True
    assert go_manifest.coverage.tree_entries == len(GO_FILES)
    assert go_manifest.coverage.scanned_files == len(GO_FILES)
    assert go_manifest.coverage.excluded_entries == 0
    assert go_manifest.inventory.ecosystems == ["go"]
    assert go_manifest.inventory.dependency_manifests == ["go.mod"]
    assert go_manifest.inventory.ci_configs == [".github/workflows/ci.yml"]
    assert go_manifest.inventory.test_directories == ["tests"]
    assert "Go" in go_manifest.languages
    assert "Markdown" in go_manifest.languages

    assert py_manifest.manifest_hash == py_manifest.computed_hash()
    assert py_manifest.coverage.tree_entries == len(PY_FILES)
    assert py_manifest.inventory.ecosystems == ["node", "python"]
    assert py_manifest.inventory.ci_configs == [".gitlab-ci.yml"]
    assert py_manifest.inventory.test_directories == ["tests"]
    assert {"Python", "JSON", "TOML", "Markdown"} <= set(py_manifest.languages)

    assert go_manifest.manifest_hash != py_manifest.manifest_hash


def test_same_commit_same_manifest_hash_across_workspaces(tmp_path: Path) -> None:
    repo, commit = build_repo(tmp_path / "src", "orders", GO_FILES)
    first = workspace(tmp_path / "one", repo, commit, run_id="run-a01").prepare()
    second = workspace(tmp_path / "two", repo, commit, run_id="run-a01").prepare()

    assert first.manifest_hash == second.manifest_hash
    assert first.files == second.files
    assert first.directories == second.directories
    assert first.languages == second.languages
    assert first.inventory == second.inventory


def test_workspace_directories_are_isolated_per_run(tmp_path: Path) -> None:
    repo, commit = build_repo(tmp_path / "src", "orders", GO_FILES)
    first = workspace(tmp_path / "ws", repo, commit, run_id="run-one")
    second = workspace(tmp_path / "ws", repo, commit, run_id="run-two")
    first.prepare()
    second.prepare()

    assert first.directory != second.directory
    assert first.worktree.is_dir() and second.worktree.is_dir()
    assert (first.worktree / "go.mod").is_file()
    assert (second.worktree / "go.mod").is_file()


def test_symlink_and_submodule_entries_are_excluded(tmp_path: Path) -> None:
    repo, base = build_repo(tmp_path / "src", "hostile", {"README.md": "# demo\n"})
    target = git("hash-object", "-w", "--stdin", cwd=repo, stdin=b"/etc/passwd")
    git("update-index", "--add", "--cacheinfo", f"120000,{target},docs/escape", cwd=repo)
    git("update-index", "--add", "--cacheinfo", f"160000,{base},vendor/dep", cwd=repo)
    git("commit", "--quiet", "-m", "hostile entries", cwd=repo)
    commit = git("rev-parse", "HEAD", cwd=repo)

    instance = workspace(tmp_path / "ws", repo, commit, run_id="run-a01")
    manifest = instance.prepare()

    reasons = {item.path: item.reason for item in manifest.exclusions}
    assert reasons == {"docs/escape": "SYMLINK", "vendor/dep": "SUBMODULE"}
    assert manifest.coverage.tree_entries == 3
    assert manifest.coverage.scanned_files == 1
    assert manifest.coverage.excluded_entries == 2
    assert not (instance.worktree / "docs").exists()
    assert not (instance.worktree / "vendor").exists()
    assert not (instance.worktree / "docs" / "escape").is_symlink()
    assert (instance.worktree / "README.md").read_text() == "# demo\n"
    assert manifest.git.clean is False


@pytest.mark.parametrize(
    "value",
    [
        "../escape",
        "/etc/passwd",
        "a/../../b",
        "..",
        "./secret",
        ".git/config",
        "C:\\windows\\system32",
        "a//b",
        "a/\x00b",
        "~/credentials",
    ],
)
def test_manifest_paths_reject_traversal(value: str) -> None:
    with pytest.raises(DomainError) as raised:
        validate_manifest_path(value)
    assert raised.value.code == "WORKSPACE_PATH_DENIED"


def test_malicious_tree_entry_cannot_escape_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, commit = build_repo(tmp_path / "src", "orders", {"ok.txt": "ok\n"})
    instance = workspace(tmp_path / "ws", repo, commit, run_id="run-a01")
    baseline = instance.prepare()
    blob = baseline.files[0].blob
    crafted = [
        *instance._tree_entries(),
        _TreeEntry(mode="100644", kind="blob", sha=blob, size=2, path="../escape.txt"),
        _TreeEntry(mode="100644", kind="blob", sha=blob, size=2, path="/abs/escape.txt"),
    ]
    monkeypatch.setattr(instance, "_tree_entries", lambda: crafted)
    manifest = instance.prepare()

    denied = {item.path for item in manifest.exclusions if item.reason == "PATH_DENIED"}
    assert denied == {"../escape.txt", "/abs/escape.txt"}
    assert not (tmp_path / "escape.txt").exists()
    assert not (tmp_path / "abs").exists()
    assert manifest.coverage.tree_entries == baseline.coverage.tree_entries + 2


def test_file_limits_and_coverage_denominator(tmp_path: Path) -> None:
    files = {f"f{index:02d}.txt": "x" * 100 for index in range(1, 7)}
    files["zz_big.bin"] = "y" * 500
    repo, commit = build_repo(tmp_path / "src", "limits", files)
    manifest = workspace(
        tmp_path / "ws",
        repo,
        commit,
        run_id="run-a01",
        limits=WorkspaceLimits(max_files=2, max_total_bytes=10_000, max_file_bytes=200),
    ).prepare()

    included = [item.path for item in manifest.files]
    assert included == ["f01.txt", "f02.txt"]
    reasons = {item.path: item.reason for item in manifest.exclusions}
    assert reasons["zz_big.bin"] == "FILE_TOO_LARGE"
    assert {reasons[f"f0{index}.txt"] for index in range(3, 7)} == {"FILE_COUNT_LIMIT"}
    assert manifest.coverage.tree_entries == 7
    assert manifest.coverage.scanned_files == 2
    assert manifest.coverage.excluded_entries == 5
    assert manifest.coverage.scanned_bytes == 200
    assert manifest.git.clean is False


def test_total_byte_limit_is_recorded(tmp_path: Path) -> None:
    files = {f"f{index:02d}.txt": "x" * 100 for index in range(1, 7)}
    repo, commit = build_repo(tmp_path / "src", "bytes", files)
    manifest = workspace(
        tmp_path / "ws",
        repo,
        commit,
        run_id="run-a01",
        limits=WorkspaceLimits(max_files=10, max_total_bytes=250, max_file_bytes=200),
    ).prepare()

    assert [item.path for item in manifest.files] == ["f01.txt", "f02.txt"]
    reasons = {item.path: item.reason for item in manifest.exclusions}
    assert set(reasons.values()) == {"TOTAL_BYTES_LIMIT"}
    assert manifest.coverage.scanned_bytes == 200
    assert manifest.coverage.excluded_entries == 4


def test_tampered_file_is_restored_and_hash_is_unchanged(tmp_path: Path) -> None:
    repo, commit = build_repo(tmp_path / "src", "orders", GO_FILES)
    instance = workspace(tmp_path / "ws", repo, commit, run_id="run-a01")
    first = instance.prepare()
    (instance.worktree / "README.md").write_text("tampered\n")
    second = instance.prepare()

    assert second.manifest_hash == first.manifest_hash
    assert (instance.worktree / "README.md").read_text() == GO_FILES["README.md"]
    assert second.git.clean is True


def test_remote_must_match_repository_identity(tmp_path: Path) -> None:
    with pytest.raises(DomainError) as raised:
        RepositoryWorkspace(
            tmp_path / "ws",
            repository="octo/example",
            commit="a" * 40,
            remote="https://github.com/other/example.git",
            run_id="run-a01",
        )
    assert raised.value.code == "WORKSPACE_REMOTE_MISMATCH"

    with pytest.raises(DomainError) as denied:
        RepositoryWorkspace(
            tmp_path / "ws",
            repository="octo/example",
            commit="a" * 40,
            remote="git@github.com:octo/example.git",
            run_id="run-a01",
        )
    assert denied.value.code == "WORKSPACE_REMOTE_DENIED"


def test_marker_conflict_is_rejected(tmp_path: Path) -> None:
    repo, commit = build_repo(tmp_path / "src", "orders", {"README.md": "# demo\n"})
    instance = workspace(tmp_path / "ws", repo, commit, run_id="run-a01")
    instance.prepare()
    marker = json.loads(instance.marker.read_text())
    marker["commit"] = "b" * 40
    instance.marker.write_text(json.dumps(marker))

    with pytest.raises(DomainError) as raised:
        instance.prepare()
    assert raised.value.code == "WORKSPACE_CONFLICT"


def test_invalid_identity_and_commit_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(DomainError) as raised:
        RepositoryWorkspace(
            tmp_path / "ws",
            repository="../escape",
            commit="a" * 40,
            remote=str(tmp_path),
            run_id="run-a01",
        )
    assert raised.value.code == "WORKSPACE_REPOSITORY_DENIED"

    with pytest.raises(DomainError) as commit_error:
        RepositoryWorkspace(
            tmp_path / "ws",
            repository="octo/example",
            commit="not-a-commit",
            remote=str(tmp_path),
            run_id="run-a01",
        )
    assert commit_error.value.code == "WORKSPACE_COMMIT_DENIED"

    with pytest.raises(DomainError) as run_error:
        RepositoryWorkspace(
            tmp_path / "ws",
            repository="octo/example",
            commit="a" * 40,
            remote=str(tmp_path),
            run_id="run/one",
        )
    assert run_error.value.code == "WORKSPACE_RUN_DENIED"


def test_manifest_contains_no_host_paths_or_timestamps(tmp_path: Path) -> None:
    repo, commit = build_repo(tmp_path / "src", "orders", GO_FILES)
    manifest: RepositoryManifest = workspace(
        tmp_path / "ws", repo, commit, run_id="run-a01"
    ).prepare()
    payload = json.dumps(manifest.content(), sort_keys=True)

    assert str(tmp_path) not in payload
    assert "workspace.json" not in payload
    assert "created_at" not in payload
    assert "timestamp" not in payload
