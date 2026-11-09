"""V3.4 R02 generic worktree and patch editor tests.

The editor must apply multi-file, cross-language patches inside a private
worktree, support new source and test files, enforce configurable file/line/byte
quotas, expose per-patch tree hashes, and refuse traversal, symlink escape,
protected configuration and stale pre-images.  The host project is never touched.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from mgar_v3.domain import DomainError
from mgar_v3.repair.worktree import (
    FileEdit,
    PatchLimits,
    PatchWorktree,
)

CALC = "def add(a, b):\n    return a - b\n"
CALC_FIXED = "def add(a, b):\n    return a + b\n"
UTIL = "def clamp(value, low, high):\n    return max(low, min(high, value))\n"
UTIL_FIXED = (
    "def clamp(value, low, high):\n"
    "    high = max(low, high)\n"
    "    return min(high, max(low, value))\n"
)
TEST_CALC = "from src.calc import add\n\n\ndef test_add():\n    assert add(2, 2) == 4\n"
EXTRA = "def double(value):\n    return value * 2\n"
TEST_EXTRA = "from src.extra import double\n\n\ndef test_double():\n    assert double(3) == 6\n"

GO_CALC = "package mathx\n\nfunc Add(a, b int) int {\n\treturn a - b\n}\n"
GO_CALC_FIXED = "package mathx\n\nfunc Add(a, b int) int {\n\treturn a + b\n}\n"
GO_TEST = (
    'package mathx\n\nimport "testing"\n\n'
    'func TestAdd(t *testing.T) {\n\tif Add(2, 2) != 4 {\n\t\tt.Fatalf("unexpected")\n\t}\n}\n'
)
GO_TEST_FIXED = GO_TEST.replace('"unexpected"', '"unexpected sum"')
GO_HELPER = "package mathx\n\nfunc Double(value int) int {\n\treturn value * 2\n}\n"

PYTHON_FILES = {
    "src/calc.py": CALC,
    "src/util.py": UTIL,
    "tests/test_calc.py": TEST_CALC,
}
PYTHON_PATHS = [
    "src/calc.py",
    "src/util.py",
    "src/extra.py",
    "tests/test_calc.py",
    "tests/test_extra.py",
]
GO_FILES = {
    "internal/mathx/calc.go": GO_CALC,
    "internal/mathx/calc_test.go": GO_TEST,
}
GO_PATHS = ["internal/mathx/calc.go", "internal/mathx/calc_test.go", "internal/mathx/helper.go"]


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _update(path: str, content: str, before: str) -> FileEdit:
    return FileEdit(path=path, operation="update", content=content, expected_sha256=_sha(before))


def _create(path: str, content: str) -> FileEdit:
    return FileEdit(path=path, operation="create", content=content)


def _delete(path: str, before: str) -> FileEdit:
    return FileEdit(path=path, operation="delete", expected_sha256=_sha(before))


def _worktree(
    files: dict[str, str],
    paths: list[str],
    *,
    limits: PatchLimits | None = None,
    worktree_root: Path | None = None,
) -> PatchWorktree:
    return PatchWorktree(
        files,
        allowed_paths=paths,
        limits=limits,
        worktree_root=worktree_root,
    )


def test_python_multi_file_patch_applies_and_reverts() -> None:
    with _worktree(PYTHON_FILES, PYTHON_PATHS) as worktree:
        baseline = worktree.baseline_hash()
        current = worktree.read_worktree_file("src/calc.py")
        assert current.content == CALC
        assert current.sha256 == _sha(CALC)

        result = worktree.apply_patch(
            [
                _update("src/calc.py", CALC_FIXED, CALC),
                _update("src/util.py", UTIL_FIXED, UTIL),
                _create("src/extra.py", EXTRA),
                _create("tests/test_extra.py", TEST_EXTRA),
            ]
        )

        assert result.tree_before == baseline
        assert result.tree_after == worktree.tree_hash()
        assert result.tree_after != baseline
        assert result.files_changed == 4
        assert result.lines_added > 0
        assert result.bytes_written == sum(
            len(value.encode()) for value in (CALC_FIXED, UTIL_FIXED, EXTRA, TEST_EXTRA)
        )
        assert worktree.read_worktree_file("src/calc.py").content == CALC_FIXED
        changed = {item.path: item for item in worktree.get_changed_files()}
        assert changed["src/calc.py"].operation == "update"
        assert changed["src/extra.py"].operation == "create"
        assert changed["tests/test_extra.py"].operation == "create"

        diff = worktree.get_diff()
        assert "diff --git a/src/calc.py b/src/calc.py" in diff
        assert "+    return a + b" in diff
        assert "new file mode 100644" in diff
        assert "tests/test_extra.py" in diff

        revert = worktree.revert_patch()
        assert revert.tree_before == result.tree_after
        assert revert.tree_after == baseline
        assert worktree.tree_hash() == baseline
        assert worktree.get_changed_files() == []
        assert worktree.read_worktree_file("src/calc.py").content == CALC
        with pytest.raises(DomainError) as gone:
            worktree.read_worktree_file("src/extra.py")
        assert gone.value.code == "PATCH_NOT_FOUND"


def test_go_multi_file_patch_uses_the_same_editor() -> None:
    with _worktree(GO_FILES, GO_PATHS) as worktree:
        result = worktree.apply_patch(
            [
                _update("internal/mathx/calc.go", GO_CALC_FIXED, GO_CALC),
                _update("internal/mathx/calc_test.go", GO_TEST_FIXED, GO_TEST),
                _create("internal/mathx/helper.go", GO_HELPER),
            ]
        )
        assert result.files_changed == 3
        assert worktree.read_worktree_file("internal/mathx/calc.go").content == GO_CALC_FIXED
        assert worktree.read_worktree_file("internal/mathx/helper.go").content == GO_HELPER
        serialized = worktree.get_diff()
        assert "internal/mathx/calc.go" in serialized
        assert "orders.go" not in serialized


def test_patch_quotas_and_new_file_policy_are_configurable() -> None:
    with _worktree(PYTHON_FILES, PYTHON_PATHS, limits=PatchLimits(max_files=1)) as files_first:
        first = files_first.read_worktree_file("src/calc.py")
        with pytest.raises(DomainError) as too_many:
            files_first.apply_patch(
                [
                    _update("src/calc.py", CALC_FIXED, CALC),
                    _update("src/util.py", UTIL_FIXED, UTIL),
                ]
            )
        assert too_many.value.code == "PATCH_LIMIT"
        assert first.content == CALC

    with _worktree(
        PYTHON_FILES, PYTHON_PATHS, limits=PatchLimits(max_changed_lines=2)
    ) as lines_first:
        with pytest.raises(DomainError) as too_long:
            lines_first.apply_patch(
                [
                    FileEdit(
                        path="src/calc.py",
                        operation="update",
                        content="def add(a, b):\n    # sum\n    return a + b\n",
                        expected_sha256=_sha(CALC),
                    )
                ]
            )
        assert too_long.value.code == "PATCH_LIMIT"

    with _worktree(
        PYTHON_FILES, PYTHON_PATHS, limits=PatchLimits(max_total_bytes=10)
    ) as bytes_first:
        with pytest.raises(DomainError) as too_large:
            bytes_first.apply_patch([_update("src/calc.py", CALC_FIXED, CALC)])
        assert too_large.value.code == "PATCH_LIMIT"

    with _worktree(PYTHON_FILES, PYTHON_PATHS) as no_delete:
        with pytest.raises(DomainError) as deletion:
            no_delete.apply_patch([_delete("src/util.py", UTIL)])
        assert deletion.value.code == "PATCH_DELETE_DENIED"
        assert no_delete.read_worktree_file("src/util.py").content == UTIL

    with _worktree(
        PYTHON_FILES, PYTHON_PATHS, limits=PatchLimits(max_deleted_files=1)
    ) as one_delete:
        result = one_delete.apply_patch([_delete("src/util.py", UTIL)])
        assert result.changed_files[0].operation == "delete"
        assert one_delete.revert_patch().tree_after == one_delete.baseline_hash()

    with _worktree(
        PYTHON_FILES, PYTHON_PATHS, limits=PatchLimits(allow_new_files=False)
    ) as no_create:
        with pytest.raises(DomainError) as creation:
            no_create.apply_patch([_create("src/extra.py", EXTRA)])
        assert creation.value.code == "PATCH_CREATE_DENIED"


def test_paths_symlinks_and_protected_configuration_are_rejected(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_text("host secret\n", encoding="utf-8")
    paths = [*PYTHON_PATHS, "link/secret.txt", "go.mod", "pyproject.toml", "Dockerfile"]

    with _worktree(PYTHON_FILES, paths) as worktree:
        for path in ("../outside.txt", "/etc/passwd", ".git/config", "src/../escape.py"):
            with pytest.raises(DomainError) as denied:
                worktree.apply_patch([_create(path, "x\n")])
            assert denied.value.code == "PATCH_PATH_DENIED"

        with pytest.raises(DomainError) as unauthorized:
            worktree.apply_patch([_create("src/unlisted.py", "x\n")])
        assert unauthorized.value.code == "PATCH_PATH_DENIED"

        for path in ("go.mod", "pyproject.toml", "Dockerfile"):
            with pytest.raises(DomainError) as protected:
                worktree.apply_patch([_create(path, "x\n")])
            assert protected.value.code == "PATCH_PROTECTED"

        os.symlink(outside, worktree.root / "link")
        with pytest.raises(DomainError) as symlink:
            worktree.apply_patch([_create("link/secret.txt", "overwritten\n")])
        assert symlink.value.code == "PATCH_PATH_DENIED"
        assert secret.read_text(encoding="utf-8") == "host secret\n"

        with pytest.raises(DomainError) as read_symlink:
            worktree.read_worktree_file("link/secret.txt")
        assert read_symlink.value.code == "PATCH_PATH_DENIED"


def test_stale_conflict_and_missing_preimages_are_rejected() -> None:
    with _worktree(PYTHON_FILES, PYTHON_PATHS) as worktree:
        with pytest.raises(DomainError) as stale:
            worktree.apply_patch(
                [
                    FileEdit(
                        path="src/calc.py",
                        operation="update",
                        content=CALC_FIXED,
                        expected_sha256="0" * 64,
                    )
                ]
            )
        assert stale.value.code == "PATCH_STALE"

        with pytest.raises(DomainError) as conflict:
            worktree.apply_patch([_create("src/calc.py", CALC_FIXED)])
        assert conflict.value.code == "PATCH_CONFLICT"

        with pytest.raises(DomainError) as missing_update:
            worktree.apply_patch([_update("src/extra.py", EXTRA, EXTRA)])
        assert missing_update.value.code == "PATCH_NOT_FOUND"

        with _worktree(
            PYTHON_FILES, PYTHON_PATHS, limits=PatchLimits(max_deleted_files=1)
        ) as delete_ready:
            with pytest.raises(DomainError) as missing_delete:
                delete_ready.apply_patch([_delete("src/extra.py", EXTRA)])
            assert missing_delete.value.code == "PATCH_NOT_FOUND"

        with pytest.raises(DomainError) as empty:
            worktree.apply_patch([])
        assert empty.value.code == "PATCH_EMPTY"

        with pytest.raises(DomainError) as empty_revert:
            worktree.revert_patch()
        assert empty_revert.value.code == "PATCH_REVERT_EMPTY"

        first = worktree.apply_patch([_update("src/calc.py", CALC_FIXED, CALC)])
        second = worktree.apply_patch([_update("src/util.py", UTIL_FIXED, UTIL)])
        assert second.tree_before == first.tree_after
        assert second.patch_id != first.patch_id

        first_revert = worktree.revert_patch()
        assert first_revert.tree_after == first.tree_after
        assert worktree.read_worktree_file("src/util.py").content == UTIL
        assert worktree.read_worktree_file("src/calc.py").content == CALC_FIXED

        worktree.revert_patch()
        assert worktree.tree_hash() == worktree.baseline_hash()
        with pytest.raises(DomainError) as exhausted:
            worktree.revert_patch()
        assert exhausted.value.code == "PATCH_REVERT_EMPTY"


def test_host_project_is_never_the_worktree(tmp_path: Path) -> None:
    host = tmp_path / "host-project"
    (host / "src").mkdir(parents=True)
    sentinel = host / "src" / "calc.py"
    sentinel.write_text("HOST ORIGINAL\n", encoding="utf-8")
    root = tmp_path / "worktrees"
    root.mkdir()

    with _worktree(PYTHON_FILES, PYTHON_PATHS, worktree_root=root) as worktree:
        assert worktree.root.parent == root.resolve()
        assert worktree.root != host
        assert host not in worktree.root.parents
        worktree.apply_patch([_update("src/calc.py", CALC_FIXED, CALC)])
        assert worktree.read_worktree_file("src/calc.py").content == CALC_FIXED
        assert sentinel.read_text(encoding="utf-8") == "HOST ORIGINAL\n"

    assert not worktree.root.exists()
    assert sentinel.read_text(encoding="utf-8") == "HOST ORIGINAL\n"


def test_worktree_can_inherit_an_r01_handoff_scope() -> None:
    scope = SimpleNamespace(allowed_paths=PYTHON_PATHS)
    with PatchWorktree.from_handoff(scope, PYTHON_FILES) as worktree:
        result = worktree.apply_patch([_update("src/calc.py", CALC_FIXED, CALC)])
        assert result.files_changed == 1
        assert worktree.read_worktree_file("src/calc.py").content == CALC_FIXED

    with pytest.raises(DomainError) as denied:
        PatchWorktree.from_handoff(SimpleNamespace(allowed_paths=[]), PYTHON_FILES)
    assert denied.value.code == "PATCH_PATH_DENIED"


def test_worktree_requires_an_authorized_scope() -> None:
    with pytest.raises(DomainError) as empty:
        PatchWorktree(PYTHON_FILES, allowed_paths=[])
    assert empty.value.code == "PATCH_PATH_DENIED"

    with pytest.raises(DomainError) as hidden:
        PatchWorktree(PYTHON_FILES, allowed_paths=["src/calc.py", ".github/workflows/ci.yml"])
    assert hidden.value.code == "PATCH_PATH_DENIED"
