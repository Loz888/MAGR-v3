"""Generic isolated worktree and patch editor (V3.4 R02).

A repair may edit only the paths R01 authorized, inside a private temporary
directory that is populated from an explicit base tree.  The editor supports
multiple files in one atomic patch, new source/test files, configurable file,
line and byte budgets, per-patch Git-style tree hashes, and full revert.

Nothing here knows about ``orders.go`` or any fixed test name.  Every write is
checked for traversal, symlink escape and protected configuration, and the host
project directory is never used as a worktree root.
"""

from __future__ import annotations

import difflib
import hashlib
import os
import re
import shutil
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal, Protocol, Self

from pydantic import Field, model_validator

from mgar_v3.domain import DomainError, Model, digest
from mgar_v3.repair.workspace import tree
from mgar_v3.repository_analysis.workspace import validate_manifest_path

_PATH = re.compile(r"[A-Za-z0-9_./-]{1,300}")
_SHA256 = r"^[a-f0-9]{64}$"

DEFAULT_PROTECTED_GLOBS = [
    ".git/**",
    ".github/**",
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "Dockerfile",
    "docker-compose*.yml",
    "go.mod",
    "go.sum",
    "pyproject.toml",
    "requirements*.txt",
    "package.json",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "poetry.lock",
]


def validate_patch_path(value: str) -> str:
    """Reject anything that is not a safe, visible, repository-relative path."""

    try:
        validate_manifest_path(value)
    except DomainError as exc:
        raise DomainError("PATCH_PATH_DENIED", "Patch path is outside the worktree", 403) from exc
    if not _PATH.fullmatch(value):
        raise DomainError("PATCH_PATH_DENIED", "Patch path contains unsupported characters", 403)
    if any(segment.startswith(".") for segment in value.split("/")):
        raise DomainError(
            "PATCH_PATH_DENIED", "Patch may not touch hidden or Git control paths", 403
        )
    return value


def is_protected_path(path: str, globs: Sequence[str]) -> bool:
    """Report whether an edit target is a protected configuration or manifest."""

    pure = PurePosixPath(path)
    return any(pure.match(pattern) for pattern in globs)


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def line_delta(before: str, after: str) -> tuple[int, int]:
    before_lines = before.splitlines()
    after_lines = after.splitlines()
    added = removed = 0
    matcher = difflib.SequenceMatcher(None, before_lines, after_lines, autojunk=False)
    for tag, old_start, old_end, new_start, new_end in matcher.get_opcodes():
        if tag in {"replace", "delete"}:
            removed += old_end - old_start
        if tag in {"replace", "insert"}:
            added += new_end - new_start
    return added, removed


class PatchLimits(Model):
    """Configurable repair quotas; defaults are deliberately narrow."""

    max_files: Annotated[int, Field(ge=1, le=100)] = 8
    max_changed_lines: Annotated[int, Field(ge=1, le=50_000)] = 400
    max_total_bytes: Annotated[int, Field(ge=1, le=10_000_000)] = 65_536
    max_file_bytes: Annotated[int, Field(ge=1, le=10_000_000)] = 65_536
    max_deleted_files: Annotated[int, Field(ge=0, le=100)] = 0
    allow_new_files: bool = True
    protected_globs: list[str] = Field(default_factory=lambda: list(DEFAULT_PROTECTED_GLOBS))


class FileEdit(Model):
    """One update, creation or deletion with an optional pre-image hash."""

    path: Annotated[str, Field(min_length=1, max_length=300)]
    operation: Literal["update", "create", "delete"]
    content: Annotated[str, Field(max_length=10_000_000)] | None = None
    expected_sha256: Annotated[str, Field(pattern=_SHA256)] | None = None

    @model_validator(mode="after")
    def edit_shape(self) -> Self:
        if self.operation in {"update", "create"} and self.content is None:
            raise ValueError("Update and create edits require content")
        if self.operation == "delete" and self.content is not None:
            raise ValueError("Delete edits cannot carry content")
        if self.operation != "create" and self.expected_sha256 is None:
            raise ValueError("Update and delete edits require the pre-image hash")
        if self.operation == "create" and self.expected_sha256 is not None:
            raise ValueError("Create edits must not declare a pre-image hash")
        return self


class FileView(Model):
    path: str
    content: str
    sha256: str
    bytes: int
    lines: int


class ChangedFile(Model):
    path: str
    operation: Literal["create", "update", "delete"]
    lines_added: int
    lines_removed: int
    bytes: int
    sha256: str | None


class PatchResult(Model):
    patch_id: str
    tree_before: str
    tree_after: str
    changed_files: list[ChangedFile]
    files_changed: int
    lines_added: int
    lines_removed: int
    bytes_written: int


@dataclass(frozen=True)
class _Receipt:
    result: PatchResult
    before: dict[str, str]
    after: dict[str, str]


class HandoffScope(Protocol):
    """The R01 handoff surface a worktree is allowed to inherit."""

    allowed_paths: list[str]


class PatchWorktree:
    """An isolated copy of a base tree with a transactional patch editor."""

    def __init__(
        self,
        files: Mapping[str, str],
        *,
        allowed_paths: Sequence[str],
        limits: PatchLimits | None = None,
        worktree_root: str | Path | None = None,
    ):
        self.limits = limits or PatchLimits()
        self.allowed_paths = {validate_patch_path(path) for path in allowed_paths}
        if not self.allowed_paths:
            raise DomainError("PATCH_PATH_DENIED", "At least one allowed path is required", 403)
        initial: dict[str, str] = {}
        for path, content in files.items():
            normalized = validate_patch_path(path)
            if not isinstance(content, str):
                raise DomainError("PATCH_FORMAT", "Worktree files must be text", 415)
            validate_patch_content(normalized, content, self.limits)
            initial[normalized] = content
        parent = Path(worktree_root) if worktree_root is not None else None
        self.root = Path(
            tempfile.mkdtemp(prefix="mgar-patch-", dir=str(parent) if parent else None)
        )
        self._files: dict[str, str] = dict(initial)
        self._baseline: dict[str, str] = dict(initial)
        self._base_tree = self._tree(initial)
        self._receipts: list[_Receipt] = []
        try:
            self._materialize(initial)
        except Exception:
            self.cleanup()
            raise

    # -- lifecycle -----------------------------------------------------

    @classmethod
    def from_handoff(
        cls,
        handoff: HandoffScope,
        files: Mapping[str, str],
        *,
        limits: PatchLimits | None = None,
        worktree_root: str | Path | None = None,
    ) -> PatchWorktree:
        """Build a worktree whose write scope is exactly the R01 authorization."""

        return cls(
            files,
            allowed_paths=handoff.allowed_paths,
            limits=limits,
            worktree_root=worktree_root,
        )

    def cleanup(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def __enter__(self) -> PatchWorktree:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.cleanup()

    # -- tools ---------------------------------------------------------

    def read_worktree_file(self, path: str, *, max_bytes: int | None = None) -> FileView:
        normalized = validate_patch_path(path)
        target = self._target(normalized)
        if not target.is_file():
            raise DomainError(
                "PATCH_NOT_FOUND", f"Worktree file {normalized!r} does not exist", 404
            )
        limit = max_bytes if max_bytes is not None else self.limits.max_file_bytes
        raw = target.read_bytes()
        if len(raw) > limit:
            raise DomainError("PATCH_LIMIT", "Worktree file exceeds the read bound", 413)
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise DomainError("PATCH_FORMAT", "Worktree file is not UTF-8 text", 415) from exc
        return FileView(
            path=normalized,
            content=content,
            sha256=sha256_text(content),
            bytes=len(raw),
            lines=len(content.splitlines()),
        )

    def apply_patch(self, edits: Sequence[FileEdit]) -> PatchResult:
        if not edits:
            raise DomainError("PATCH_EMPTY", "Patch contains no edits", 400)
        seen: set[str] = set()
        for edit in edits:
            if edit.path in seen:
                raise DomainError("PATCH_FORMAT", "Patch edits the same path twice", 400)
            seen.add(edit.path)
        if len(edits) > self.limits.max_files:
            raise DomainError("PATCH_LIMIT", "Patch changes too many files", 413)
        deletions = sum(edit.operation == "delete" for edit in edits)
        if deletions > self.limits.max_deleted_files:
            raise DomainError(
                "PATCH_DELETE_DENIED", "Patch deletes more files than the repair allows", 403
            )

        files = dict(self._files)
        changed: list[ChangedFile] = []
        added_lines = removed_lines = 0
        written = 0
        for edit in edits:
            path = validate_patch_path(edit.path)
            self._require_authorized(path)
            self._require_unprotected(path)
            before = files.get(path)
            if edit.operation == "create":
                if not self.limits.allow_new_files:
                    raise DomainError("PATCH_CREATE_DENIED", "New files are disabled", 403)
                if before is not None:
                    raise DomainError("PATCH_CONFLICT", f"{path!r} already exists", 409)
                assert edit.content is not None
                validate_patch_content(path, edit.content, self.limits)
                files[path] = edit.content
                added, removed = line_delta("", edit.content)
                changed.append(
                    ChangedFile(
                        path=path,
                        operation="create",
                        lines_added=added,
                        lines_removed=removed,
                        bytes=len(edit.content.encode()),
                        sha256=sha256_text(edit.content),
                    )
                )
                written += len(edit.content.encode())
            elif edit.operation == "update":
                if before is None:
                    raise DomainError("PATCH_NOT_FOUND", f"{path!r} does not exist", 404)
                if sha256_text(before) != edit.expected_sha256:
                    raise DomainError("PATCH_STALE", f"{path!r} changed since it was read", 409)
                assert edit.content is not None
                validate_patch_content(path, edit.content, self.limits)
                files[path] = edit.content
                added, removed = line_delta(before, edit.content)
                changed.append(
                    ChangedFile(
                        path=path,
                        operation="update",
                        lines_added=added,
                        lines_removed=removed,
                        bytes=len(edit.content.encode()),
                        sha256=sha256_text(edit.content),
                    )
                )
                written += len(edit.content.encode())
            else:
                if before is None:
                    raise DomainError("PATCH_NOT_FOUND", f"{path!r} does not exist", 404)
                if sha256_text(before) != edit.expected_sha256:
                    raise DomainError("PATCH_STALE", f"{path!r} changed since it was read", 409)
                del files[path]
                added, removed = line_delta(before, "")
                changed.append(
                    ChangedFile(
                        path=path,
                        operation="delete",
                        lines_added=added,
                        lines_removed=removed,
                        bytes=0,
                        sha256=None,
                    )
                )
            added_lines += added
            removed_lines += removed

        if files == self._files:
            raise DomainError("PATCH_EMPTY", "Patch makes no content change", 400)
        if added_lines + removed_lines > self.limits.max_changed_lines:
            raise DomainError("PATCH_LIMIT", "Patch changes too many lines", 413)
        if written > self.limits.max_total_bytes:
            raise DomainError("PATCH_LIMIT", "Patch exceeds the total byte budget", 413)

        tree_before = self.tree_hash()
        tree_after = self._tree(files)
        if tree_after == tree_before:
            raise DomainError("PATCH_EMPTY", "Patch makes no tree change", 400)
        before_files = dict(self._files)
        self._replace(files)
        result = PatchResult(
            patch_id=digest(
                {
                    "tree_before": tree_before,
                    "tree_after": tree_after,
                    "edits": [edit.model_dump(mode="json") for edit in edits],
                }
            ),
            tree_before=tree_before,
            tree_after=tree_after,
            changed_files=changed,
            files_changed=len(changed),
            lines_added=added_lines,
            lines_removed=removed_lines,
            bytes_written=written,
        )
        self._receipts.append(_Receipt(result=result, before=before_files, after=dict(files)))
        return result

    def revert_patch(self) -> PatchResult:
        if not self._receipts:
            raise DomainError("PATCH_REVERT_EMPTY", "No applied patch to revert", 409)
        receipt = self._receipts[-1]
        if self.tree_hash() != receipt.result.tree_after:
            raise DomainError("PATCH_STALE", "Worktree changed after the patch was applied", 409)
        self._replace(receipt.before)
        self._receipts.pop()
        return PatchResult(
            patch_id=receipt.result.patch_id,
            tree_before=receipt.result.tree_after,
            tree_after=receipt.result.tree_before,
            changed_files=receipt.result.changed_files,
            files_changed=receipt.result.files_changed,
            lines_added=receipt.result.lines_removed,
            lines_removed=receipt.result.lines_added,
            bytes_written=sum(
                len(receipt.before[item.path].encode())
                for item in receipt.result.changed_files
                if item.path in receipt.before
            ),
        )

    def get_diff(self) -> str:
        parts: list[str] = []
        for item in self.get_changed_files():
            before = self._baseline.get(item.path, "")
            after = self._files.get(item.path, "")
            parts.append(f"diff --git a/{item.path} b/{item.path}\n")
            if item.operation == "create":
                parts.append("new file mode 100644\n")
            elif item.operation == "delete":
                parts.append("deleted file mode 100644\n")
            parts.extend(
                difflib.unified_diff(
                    before.splitlines(keepends=True),
                    after.splitlines(keepends=True),
                    fromfile=f"a/{item.path}",
                    tofile=f"b/{item.path}",
                )
            )
        return "".join(parts)

    def get_changed_files(self) -> list[ChangedFile]:
        changed: list[ChangedFile] = []
        for path in sorted(set(self._baseline) | set(self._files)):
            before = self._baseline.get(path)
            after = self._files.get(path)
            if before == after:
                continue
            if before is None:
                assert after is not None
                added, removed = line_delta("", after)
                changed.append(
                    ChangedFile(
                        path=path,
                        operation="create",
                        lines_added=added,
                        lines_removed=removed,
                        bytes=len(after.encode()),
                        sha256=sha256_text(after),
                    )
                )
            elif after is None:
                added, removed = line_delta(before, "")
                changed.append(
                    ChangedFile(
                        path=path,
                        operation="delete",
                        lines_added=added,
                        lines_removed=removed,
                        bytes=0,
                        sha256=None,
                    )
                )
            else:
                added, removed = line_delta(before, after)
                changed.append(
                    ChangedFile(
                        path=path,
                        operation="update",
                        lines_added=added,
                        lines_removed=removed,
                        bytes=len(after.encode()),
                        sha256=sha256_text(after),
                    )
                )
        return changed

    def tree_hash(self) -> str:
        return self._tree(self._files)

    def baseline_hash(self) -> str:
        return self._base_tree

    def snapshot(self) -> dict[str, str]:
        return dict(self._files)

    # -- internals -----------------------------------------------------

    def _tree(self, files: Mapping[str, str]) -> str:
        try:
            return tree(dict(files))
        except DomainError as exc:
            raise DomainError(
                "PATCH_PATH_DENIED", "Worktree contains an unsupported path", 403
            ) from exc

    def _require_authorized(self, path: str) -> None:
        if path not in self.allowed_paths:
            raise DomainError(
                "PATCH_PATH_DENIED", f"{path!r} is not an authorized repair path", 403
            )

    def _require_unprotected(self, path: str) -> None:
        if is_protected_path(path, self.limits.protected_globs):
            raise DomainError("PATCH_PROTECTED", f"{path!r} is a protected configuration file", 403)

    def _target(self, path: str) -> Path:
        root = self.root.resolve()
        current = self.root
        for part in path.split("/"):
            current = current / part
            if current.is_symlink():
                raise DomainError("PATCH_PATH_DENIED", "Patch path traverses a symbolic link", 403)
        parent = current.parent.resolve()
        if parent != root and root not in parent.parents:
            raise DomainError("PATCH_PATH_DENIED", "Patch path escapes the worktree", 403)
        return current

    def _replace(self, files: dict[str, str]) -> None:
        previous = dict(self._files)
        try:
            self._materialize(files)
        except DomainError:
            self._files = previous
            raise
        except Exception as exc:
            self._files = previous
            try:
                self._materialize(previous)
            except Exception:
                pass
            raise DomainError("PATCH_IO", "Patch could not be written atomically", 500) from exc
        self._files = dict(files)

    def _materialize(self, files: Mapping[str, str]) -> None:
        root = self.root.resolve()
        if not root.is_dir():
            raise DomainError("PATCH_IO", "Worktree root is unavailable", 500)
        for path in sorted(set(self._files) | set(files)):
            target = self._target(path)
            if path not in files:
                if target.exists():
                    target.unlink()
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target = self._target(path)
            payload = files[path].encode()
            flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
            with os.fdopen(os.open(str(target), flags, 0o644), "wb") as handle:
                handle.write(payload)
        on_disk = {
            path: (self.root / path).read_text(encoding="utf-8")
            for path in files
            if (self.root / path).is_file()
        }
        if on_disk != dict(files):
            raise DomainError("PATCH_IO", "Worktree bytes diverged after writing", 500)


def validate_patch_content(path: str, content: str, limits: PatchLimits) -> None:
    if "\0" in content:
        raise DomainError("PATCH_FORMAT", f"{path!r} contains NUL bytes", 415)
    if len(content.encode()) > limits.max_file_bytes:
        raise DomainError("PATCH_LIMIT", f"{path!r} exceeds the per-file byte bound", 413)
