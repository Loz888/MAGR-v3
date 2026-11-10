"""Isolated repository snapshots and reproducible inventories (V3.3 A01).

The workspace owns a private Git object store and a materialized tree for one
Run and one immutable commit.  Repository code is never executed: we do not
run ``git checkout``, hooks, filters, submodule scripts or build tools.  Blobs
are read from the object database and written directly after path validation,
and every written blob is re-hashed against its Git object id.

Manifests contain no timestamps, host paths or run identifiers, so the same
commit and limits always produce the same manifest hash.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field

from mgar_v3.domain import DomainError, Model, digest
from mgar_v3.repository_analysis.domain import parse_repository_url, validate_commit

MANIFEST_VERSION = "v33-repository-manifest-v1"
WORKSPACE_VERSION = "v33-workspace-v1"
SNAPSHOT_VERSION = "v35-repository-snapshot-v1"
WORKSPACE_REF = "refs/heads/mgar-workspace"

_REPOSITORY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,98}/[A-Za-z0-9][A-Za-z0-9_.-]{0,98}$")
_RUN_ID = re.compile(r"^[A-Za-z0-9_-]{1,100}$")

_EXTENSIONS: dict[str, str] = {
    ".c": "C",
    ".h": "C",
    ".cc": "C++",
    ".cpp": "C++",
    ".cxx": "C++",
    ".hpp": "C++",
    ".hh": "C++",
    ".cs": "C#",
    ".css": "CSS",
    ".dart": "Dart",
    ".ex": "Elixir",
    ".exs": "Elixir",
    ".go": "Go",
    ".gradle": "Groovy",
    ".groovy": "Groovy",
    ".hs": "Haskell",
    ".html": "HTML",
    ".htm": "HTML",
    ".java": "Java",
    ".js": "JavaScript",
    ".mjs": "JavaScript",
    ".cjs": "JavaScript",
    ".jsx": "JavaScript",
    ".json": "JSON",
    ".kt": "Kotlin",
    ".kts": "Kotlin",
    ".lua": "Lua",
    ".md": "Markdown",
    ".php": "PHP",
    ".proto": "Protocol Buffers",
    ".py": "Python",
    ".pyi": "Python",
    ".rb": "Ruby",
    ".rs": "Rust",
    ".scala": "Scala",
    ".scss": "SCSS",
    ".sh": "Shell",
    ".bash": "Shell",
    ".sql": "SQL",
    ".svelte": "Svelte",
    ".swift": "Swift",
    ".toml": "TOML",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".mts": "TypeScript",
    ".cts": "TypeScript",
    ".vue": "Vue",
    ".yaml": "YAML",
    ".yml": "YAML",
    ".xml": "XML",
}

_FILENAMES: dict[str, str] = {
    "dockerfile": "Docker",
    "makefile": "Makefile",
    "cmakelists.txt": "CMake",
}

_DEPENDENCY_ECOSYSTEMS: dict[str, str] = {
    "go.mod": "go",
    "go.sum": "go",
    "go.work": "go",
    "go.work.sum": "go",
    "pyproject.toml": "python",
    "setup.py": "python",
    "setup.cfg": "python",
    "pipfile": "python",
    "pipfile.lock": "python",
    "poetry.lock": "python",
    "uv.lock": "python",
    "pdm.lock": "python",
    "package.json": "node",
    "package-lock.json": "node",
    "yarn.lock": "node",
    "pnpm-lock.yaml": "node",
    "bun.lockb": "node",
    "cargo.toml": "rust",
    "cargo.lock": "rust",
    "pom.xml": "java",
    "build.gradle": "java",
    "build.gradle.kts": "java",
    "settings.gradle": "java",
    "gemfile": "ruby",
    "gemfile.lock": "ruby",
    "composer.json": "php",
    "composer.lock": "php",
    "pubspec.yaml": "dart",
}

_CI_FILENAMES = {
    ".gitlab-ci.yml",
    "azure-pipelines.yml",
    "appveyor.yml",
    ".travis.yml",
    "bitbucket-pipelines.yml",
    ".drone.yml",
    "cloudbuild.yaml",
    "cloudbuild.yml",
    "jenkinsfile",
}

_TEST_DIRECTORIES = {"test", "tests", "__tests__", "spec", "specs"}

_EXCLUSION_REASONS = Literal[
    "PATH_DENIED",
    "SYMLINK",
    "SUBMODULE",
    "UNSUPPORTED_MODE",
    "FILE_TOO_LARGE",
    "FILE_COUNT_LIMIT",
    "TOTAL_BYTES_LIMIT",
]


class WorkspaceLimits(Model):
    """Hard bounds for one Run workspace; the denominator stays visible."""

    max_files: Annotated[int, Field(strict=True, ge=1, le=200_000)] = 5_000
    max_total_bytes: Annotated[int, Field(strict=True, ge=1, le=1_000_000_000)] = 50_000_000
    max_file_bytes: Annotated[int, Field(strict=True, ge=1, le=100_000_000)] = 8_000_000


class FileEntry(Model):
    path: str
    mode: Literal["100644", "100755"]
    size: Annotated[int, Field(ge=0)]
    blob: str


class DirectoryEntry(Model):
    path: str
    files: Annotated[int, Field(ge=1)]
    bytes: Annotated[int, Field(ge=0)]
    subdirectories: Annotated[int, Field(ge=0)]


class GitState(Model):
    head: str
    ref: str
    clean: bool


class Inventory(Model):
    dependency_manifests: list[str]
    ecosystems: list[str]
    ci_configs: list[str]
    test_directories: list[str]


class Exclusion(Model):
    path: str
    reason: _EXCLUSION_REASONS


class Coverage(Model):
    """Scan coverage: the Git-tree denominator, included files and reasons."""

    tree_entries: Annotated[int, Field(ge=0)]
    scanned_files: Annotated[int, Field(ge=0)]
    excluded_entries: Annotated[int, Field(ge=0)]
    scanned_bytes: Annotated[int, Field(ge=0)]


class RepositoryManifest(Model):
    manifest_version: str = MANIFEST_VERSION
    repository: str
    commit: str
    tree: str
    limits: WorkspaceLimits
    git: GitState
    files: list[FileEntry]
    directories: list[DirectoryEntry]
    languages: dict[str, int]
    inventory: Inventory
    exclusions: list[Exclusion]
    coverage: Coverage
    manifest_hash: str

    def content(self) -> dict[str, Any]:
        """The canonical, host-independent payload that defines the hash."""

        return {
            "manifest_version": self.manifest_version,
            "repository": self.repository,
            "commit": self.commit,
            "tree": self.tree,
            "limits": self.limits.model_dump(mode="json"),
            "git": self.git.model_dump(mode="json"),
            "files": [item.model_dump(mode="json") for item in self.files],
            "directories": [item.model_dump(mode="json") for item in self.directories],
            "languages": self.languages,
            "inventory": self.inventory.model_dump(mode="json"),
            "exclusions": [item.model_dump(mode="json") for item in self.exclusions],
            "coverage": self.coverage.model_dump(mode="json"),
        }

    def computed_hash(self) -> str:
        return digest(self.content())


class RepositorySnapshot(Model):
    """Byte-exact text of a fixed commit, bound to Git blobs and SHA-256.

    The snapshot is the only source a repair worktree may materialize from.
    ``files`` keeps insertion order sorted by path, and every entry's content
    hash must be reproducible from ``file_sha256``; ``tree`` remains the Git
    tree id so a worktree can prove that its base tree was rebuilt unchanged.
    """

    snapshot_version: str = SNAPSHOT_VERSION
    repository: str
    commit: str
    tree: str
    manifest_hash: str
    files: dict[str, str]
    file_sha256: dict[str, str]
    binary_paths: list[str]
    total_bytes: Annotated[int, Field(ge=0)]
    snapshot_hash: str

    def content(self) -> dict[str, Any]:
        return {
            "snapshot_version": self.snapshot_version,
            "repository": self.repository,
            "commit": self.commit,
            "tree": self.tree,
            "manifest_hash": self.manifest_hash,
            "files": self.files,
            "file_sha256": self.file_sha256,
            "binary_paths": self.binary_paths,
            "total_bytes": self.total_bytes,
        }

    def computed_hash(self) -> str:
        return digest(self.content())


def validate_manifest_path(value: str) -> str:
    """Reject absolute paths, traversal, Git metadata and control characters."""

    if not value or len(value) > 300:
        raise DomainError("WORKSPACE_PATH_DENIED", "Repository path length is unsupported", 403)
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise DomainError(
            "WORKSPACE_PATH_DENIED", "Repository path contains control characters", 403
        )
    if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise DomainError("WORKSPACE_PATH_DENIED", "Repository path is not valid UTF-8", 403)
    if value.startswith(("/", "~")) or "\\" in value or ":" in value:
        raise DomainError("WORKSPACE_PATH_DENIED", "Repository path must be relative", 403)
    segments = value.split("/")
    if any(
        not segment or segment in {".", ".."} or segment.lower() == ".git"
        for segment in segments
    ):
        raise DomainError(
            "WORKSPACE_PATH_DENIED", "Repository path traverses outside its tree", 403
        )
    return value


def _language(path: str) -> str:
    name = path.rsplit("/", 1)[-1].lower()
    if name in _FILENAMES:
        return _FILENAMES[name]
    return _EXTENSIONS.get(Path(name).suffix, "Unknown")


def _ecosystem(path: str) -> str | None:
    name = path.rsplit("/", 1)[-1].lower()
    if name in _DEPENDENCY_ECOSYSTEMS:
        return _DEPENDENCY_ECOSYSTEMS[name]
    if name.startswith("requirements") and name.endswith(".txt"):
        return "python"
    return None


def _is_ci(path: str) -> bool:
    lower = path.lower()
    if lower.startswith(".github/workflows/") and lower.endswith((".yml", ".yaml")):
        return True
    if lower == ".circleci/config.yml" or lower == ".circleci/config.yaml":
        return True
    if lower.startswith(".buildkite/") and lower.endswith((".yml", ".yaml")):
        return True
    return lower.rsplit("/", 1)[-1] in _CI_FILENAMES


@dataclass(frozen=True)
class _TreeEntry:
    mode: str
    kind: str
    sha: str
    size: int | None
    path: str


def _git_environment() -> dict[str, str]:
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_LFS_SKIP_SMUDGE": "1",
            "GIT_ALLOW_PROTOCOL": "https:file",
            "GIT_OPTIONAL_LOCKS": "0",
        }
    )
    return environment


class _BlobReader:
    """Read-only ``git cat-file --batch`` pipe; never materializes a working tree."""

    def __init__(self, git: list[str], cwd: Path, environment: dict[str, str]):
        self._command = [*git, "-C", str(cwd), "cat-file", "--batch"]
        self._environment = environment
        self._process: subprocess.Popen[bytes] | None = None

    def __enter__(self) -> _BlobReader:
        self._process = subprocess.Popen(
            self._command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self._environment,
        )
        return self

    def read(self, sha: str) -> bytes:
        assert self._process is not None and self._process.stdin is not None
        assert self._process.stdout is not None
        self._process.stdin.write(sha.encode() + b"\n")
        self._process.stdin.flush()
        header = self._process.stdout.readline().decode("ascii", "replace").strip()
        parts = header.split()
        if len(parts) != 3 or parts[1] != "blob" or parts[0] != sha:
            raise DomainError(
                "WORKSPACE_BLOB_MISSING", "Git blob is unavailable or has an unexpected type", 502
            )
        size = int(parts[2])
        data = self._process.stdout.read(size)
        self._process.stdout.read(1)
        if len(data) != size:
            raise DomainError("WORKSPACE_BLOB_MISSING", "Git blob was truncated", 502)
        actual = hashlib.sha1(b"blob " + str(size).encode() + b"\0" + data).hexdigest()
        if actual != sha:
            raise DomainError(
                "WORKSPACE_BLOB_MISMATCH", "Git blob bytes do not match their id", 502
            )
        return data

    def __exit__(self, *args: object) -> None:
        if self._process is None:
            return
        if self._process.stdin is not None:
            self._process.stdin.close()
        self._process.wait(timeout=30)
        assert self._process.stdout is not None
        self._process.stdout.close()
        if self._process.stderr is not None:
            self._process.stderr.close()


class RepositoryWorkspace:
    """One Run, one repository, one fixed commit, one isolated directory."""

    def __init__(
        self,
        root: Path,
        *,
        repository: str,
        commit: str,
        remote: str,
        run_id: str,
        limits: WorkspaceLimits | None = None,
    ):
        normalized = repository.strip().lower()
        if not _REPOSITORY.fullmatch(normalized):
            raise DomainError("WORKSPACE_REPOSITORY_DENIED", "Repository identity is invalid", 400)
        try:
            fixed_commit = validate_commit(commit)
        except ValueError as exc:
            raise DomainError(
                "WORKSPACE_COMMIT_DENIED", "A 40-character commit is required", 400
            ) from exc
        run_key = str(run_id).strip()
        if not _RUN_ID.fullmatch(run_key):
            raise DomainError("WORKSPACE_RUN_DENIED", "Run identifier is not canonical", 400)
        self.repository = normalized
        self.commit = fixed_commit
        self.run_id = run_key
        self.limits = limits or WorkspaceLimits()
        self.root = Path(root)
        self.remote, self.remote_kind = self._validate_remote(remote)
        token = digest(
            {
                "workspace_version": WORKSPACE_VERSION,
                "repository": self.repository,
                "commit": self.commit,
                "run_id": self.run_id,
            }
        )[:20]
        self.directory = self.root / token
        self.worktree = self.directory / "repo"
        self.marker = self.directory / "workspace.json"
        self.hooks = self.directory / "empty-hooks"
        self._git = [
            "git",
            "-c",
            f"core.hooksPath={self.hooks}",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "submodule.recurse=false",
            "-c",
            "fetch.recurseSubmodules=false",
            "-c",
            "advice.detachedHead=false",
        ]
        self._environment = _git_environment()

    def _validate_remote(self, remote: str) -> tuple[str, str]:
        value = remote.strip()
        if value.startswith("https://"):
            try:
                identity, _, _ = parse_repository_url(value)
            except ValueError as exc:
                raise DomainError(
                    "WORKSPACE_REMOTE_DENIED",
                    "Repository remote is not a canonical GitHub URL",
                    403,
                ) from exc
            if identity.full_name.lower() != self.repository:
                raise DomainError(
                    "WORKSPACE_REMOTE_MISMATCH",
                    "Remote does not match the requested repository",
                    403,
                )
            return f"https://github.com/{identity.full_name.lower()}", "github"
        if value.startswith("file://"):
            path = Path(value[7:])
            if path.is_absolute() and path.is_dir():
                return value, "local_fixture"
        else:
            path = Path(value)
            if path.is_absolute() and path.is_dir():
                return str(path), "local_fixture"
        raise DomainError(
            "WORKSPACE_REMOTE_DENIED",
            "Remote must be a canonical GitHub URL or an existing local fixture",
            403,
        )

    def _run_git(
        self, args: list[str], *, cwd: Path | None = None, timeout: int = 120
    ) -> str:
        try:
            result = subprocess.run(
                [*self._git, "-C", str(cwd or self.worktree), *args],
                capture_output=True,
                env=self._environment,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise DomainError(
                "WORKSPACE_GIT_TIMEOUT", "Git command exceeded its time bound", 504
            ) from exc
        if result.returncode != 0:
            detail = result.stderr.decode("utf-8", "replace").strip().splitlines()
            message = detail[-1][:300] if detail else "git command failed"
            raise DomainError("WORKSPACE_GIT_FAILED", message, 502)
        return result.stdout.decode("utf-8", "replace").strip()

    def prepare(self) -> RepositoryManifest:
        """Clone the fixed commit if needed, materialize it and build the manifest."""

        if self.root.is_symlink():
            raise DomainError("WORKSPACE_PATH_DENIED", "Workspace root is a symlink", 403)
        self.root.mkdir(parents=True, exist_ok=True)
        if self.directory.exists():
            self._check_marker()
        else:
            self._clone()
        self.hooks.mkdir(parents=True, exist_ok=True)
        entries = self._tree_entries()
        files, exclusions, scanned_bytes = self._materialize(entries)
        tree = self._run_git(["rev-parse", f"{self.commit}^{{tree}}"])
        head = self._run_git(["rev-parse", "HEAD"])
        if head != self.commit:
            raise DomainError("WORKSPACE_GIT_MISMATCH", "Workspace HEAD does not match commit", 502)
        ref = self._run_git(["symbolic-ref", "HEAD"])
        status = self._run_git(["status", "--porcelain", "--untracked-files=no"])
        manifest = self._manifest(
            entries=entries,
            files=files,
            exclusions=exclusions,
            scanned_bytes=scanned_bytes,
            tree=tree,
            ref=ref,
            clean=status == "",
        )
        self._write_marker(manifest)
        return manifest

    def snapshot(self, paths: Sequence[str] | None = None) -> RepositorySnapshot:
        """Read the materialized fixed commit as a byte-exact repair snapshot.

        Every included file is re-read from the isolated worktree and its Git
        blob id is recomputed, so a tampered materialization cannot enter a
        repair worktree.  Binary blobs are listed but never decoded; symlinks,
        submodules and unsupported modes are already excluded by the manifest.
        """

        if not self.marker.is_file():
            raise DomainError(
                "WORKSPACE_NOT_PREPARED",
                "Workspace must be prepared before a repository snapshot",
                409,
            )
        self._check_marker()
        entries = [
            entry
            for entry in self._tree_entries()
            if entry.kind == "blob" and entry.mode in {"100644", "100755"}
        ]
        by_path = {entry.path: entry for entry in entries}
        selected = (
            sorted(by_path)
            if paths is None
            else [validate_manifest_path(path) for path in paths]
        )
        files: dict[str, str] = {}
        hashes: dict[str, str] = {}
        binary_paths: list[str] = []
        total_bytes = 0
        for path in selected:
            entry = by_path.get(path)
            if entry is None:
                raise DomainError(
                    "WORKSPACE_SNAPSHOT_PATH_MISSING",
                    f"{path!r} is not a regular file in the fixed commit",
                    404,
                )
            data = self._read_snapshot_blob(entry)
            total_bytes += len(data)
            if total_bytes > self.limits.max_total_bytes:
                raise DomainError(
                    "WORKSPACE_SNAPSHOT_TOO_LARGE",
                    "Snapshot exceeds the workspace byte budget",
                    413,
                )
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                binary_paths.append(path)
                continue
            if "\0" in text:
                binary_paths.append(path)
                continue
            files[path] = text
            hashes[path] = hashlib.sha256(data).hexdigest()
        tree = self._run_git(["rev-parse", f"{self.commit}^{{tree}}"])
        head = self._run_git(["rev-parse", "HEAD"])
        if head != self.commit:
            raise DomainError("WORKSPACE_GIT_MISMATCH", "Workspace HEAD does not match commit", 502)
        try:
            marker = json.loads(self.marker.read_text())
        except (OSError, ValueError) as exc:
            raise DomainError(
                "WORKSPACE_CONFLICT", "Workspace marker is missing or unreadable", 409
            ) from exc
        payload = {
            "snapshot_version": SNAPSHOT_VERSION,
            "repository": self.repository,
            "commit": self.commit,
            "tree": tree,
            "manifest_hash": str(marker.get("manifest_hash", "")),
            "files": files,
            "file_sha256": hashes,
            "binary_paths": binary_paths,
            "total_bytes": total_bytes,
        }
        return RepositorySnapshot(
            repository=self.repository,
            commit=self.commit,
            tree=tree,
            manifest_hash=payload["manifest_hash"],
            files=files,
            file_sha256=hashes,
            binary_paths=binary_paths,
            total_bytes=total_bytes,
            snapshot_hash=digest(payload),
        )

    def _read_snapshot_blob(self, entry: _TreeEntry) -> bytes:
        destination = self.worktree.joinpath(*entry.path.split("/"))
        root = self.worktree.resolve()
        current = self.worktree
        for part in entry.path.split("/"):
            current = current / part
            if current.is_symlink():
                raise DomainError(
                    "WORKSPACE_SYMLINK_DENIED", "Workspace path is a symlink", 403
                )
        if not destination.resolve().is_relative_to(root):
            raise DomainError("WORKSPACE_PATH_DENIED", "Workspace path escapes its root", 403)
        try:
            data = destination.read_bytes()
        except FileNotFoundError as exc:
            raise DomainError(
                "WORKSPACE_SNAPSHOT_PATH_MISSING",
                "Workspace file is missing from the materialized commit",
                404,
            ) from exc
        actual = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
        if actual != entry.sha:
            raise DomainError(
                "WORKSPACE_FILE_MISMATCH",
                "Workspace file bytes do not match the fixed commit",
                502,
            )
        return data

    def _check_marker(self) -> None:
        if not self.worktree.is_dir() or not (self.worktree / ".git").exists():
            raise DomainError(
                "WORKSPACE_CONFLICT", "Workspace directory exists without its repository", 409
            )
        try:
            marker = self.marker.read_text()
            values = json.loads(marker)
        except (OSError, ValueError) as exc:
            raise DomainError(
                "WORKSPACE_CONFLICT", "Workspace marker is missing or unreadable", 409
            ) from exc
        if (
            not isinstance(values, dict)
            or values.get("workspace_version") != WORKSPACE_VERSION
            or values.get("repository") != self.repository
            or values.get("commit") != self.commit
        ):
            raise DomainError(
                "WORKSPACE_CONFLICT", "Workspace belongs to a different Run or commit", 409
            )

    def _clone(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=".tmp-workspace-", dir=self.root))
        try:
            repo = temporary / "repo"
            template = temporary / "template"
            template.mkdir()
            result = subprocess.run(
                ["git", "init", "--quiet", f"--template={template}", str(repo)],
                capture_output=True,
                env=self._environment,
                check=False,
            )
            if result.returncode != 0:
                raise DomainError("WORKSPACE_GIT_FAILED", "Git repository init failed", 502)
            self._run_git(["remote", "add", "origin", self.remote], cwd=repo)
            try:
                self._run_git(
                    ["fetch", "--quiet", "--depth=1", "--no-tags", "origin", self.commit],
                    cwd=repo,
                    timeout=300,
                )
            except DomainError:
                # Some servers refuse fetch-by-sha; a branch fetch is the bounded fallback.
                self._run_git(
                    [
                        "fetch",
                        "--quiet",
                        "--no-tags",
                        "origin",
                        "+refs/heads/*:refs/remotes/origin/*",
                    ],
                    cwd=repo,
                    timeout=300,
                )
            fetched = self._run_git(
                ["rev-parse", "--verify", f"{self.commit}^{{commit}}"], cwd=repo
            )
            if fetched != self.commit:
                raise DomainError(
                    "WORKSPACE_COMMIT_NOT_FOUND", "Requested commit is not reachable", 404
                )
            self._run_git(["symbolic-ref", "HEAD", WORKSPACE_REF], cwd=repo)
            self._run_git(["update-ref", WORKSPACE_REF, self.commit], cwd=repo)
            self._run_git(["read-tree", self.commit], cwd=repo)
            self.directory.mkdir()
            repo.rename(self.worktree)
        except Exception:
            shutil.rmtree(self.directory, ignore_errors=True)
            shutil.rmtree(temporary, ignore_errors=True)
            raise
        shutil.rmtree(temporary, ignore_errors=True)

    def _tree_entries(self) -> list[_TreeEntry]:
        raw = subprocess.run(
            [*self._git, "-C", str(self.worktree), "ls-tree", "-r", "-z", "--long", self.commit],
            capture_output=True,
            env=self._environment,
            check=False,
            timeout=120,
        )
        if raw.returncode != 0:
            raise DomainError("WORKSPACE_GIT_FAILED", "Git tree listing failed", 502)
        entries: list[_TreeEntry] = []
        for record in raw.stdout.split(b"\0"):
            if not record:
                continue
            meta, separator, path_bytes = record.partition(b"\t")
            if not separator:
                raise DomainError("WORKSPACE_GIT_FAILED", "Git tree record is malformed", 502)
            fields = meta.decode("ascii", "replace").split()
            if len(fields) < 3:
                raise DomainError("WORKSPACE_GIT_FAILED", "Git tree record is malformed", 502)
            mode, kind, sha = fields[0], fields[1], fields[2]
            size = int(fields[3]) if len(fields) > 3 and fields[3].isdigit() else None
            try:
                path = path_bytes.decode("utf-8")
            except UnicodeDecodeError:
                path = f"<invalid-utf8:{sha[:12]}>"
            entries.append(_TreeEntry(mode=mode, kind=kind, sha=sha, size=size, path=path))
        entries.sort(key=lambda entry: entry.path)
        return entries

    def _materialize(
        self, entries: list[_TreeEntry]
    ) -> tuple[list[FileEntry], list[Exclusion], int]:
        files: list[FileEntry] = []
        exclusions: list[Exclusion] = []
        scanned_bytes = 0
        pending: list[tuple[_TreeEntry, int]] = []
        for entry in entries:
            if entry.path.startswith("<invalid-utf8:"):
                exclusions.append(Exclusion(path=entry.path, reason="PATH_DENIED"))
                continue
            try:
                validate_manifest_path(entry.path)
            except DomainError:
                exclusions.append(Exclusion(path=entry.path[:300], reason="PATH_DENIED"))
                continue
            if entry.mode == "120000":
                exclusions.append(Exclusion(path=entry.path, reason="SYMLINK"))
                continue
            if entry.mode == "160000" or entry.kind == "commit":
                exclusions.append(Exclusion(path=entry.path, reason="SUBMODULE"))
                continue
            if entry.mode not in {"100644", "100755"} or entry.kind != "blob":
                exclusions.append(Exclusion(path=entry.path, reason="UNSUPPORTED_MODE"))
                continue
            size = entry.size
            if size is None:
                size = 0
            if size > self.limits.max_file_bytes:
                exclusions.append(Exclusion(path=entry.path, reason="FILE_TOO_LARGE"))
                continue
            if len(pending) >= self.limits.max_files:
                exclusions.append(Exclusion(path=entry.path, reason="FILE_COUNT_LIMIT"))
                continue
            if scanned_bytes + size > self.limits.max_total_bytes:
                exclusions.append(Exclusion(path=entry.path, reason="TOTAL_BYTES_LIMIT"))
                continue
            pending.append((entry, size))
            scanned_bytes += size
        with _BlobReader(self._git, self.worktree, self._environment) as reader:
            for entry, expected in pending:
                data = reader.read(entry.sha)
                if len(data) != expected:
                    raise DomainError(
                        "WORKSPACE_BLOB_MISMATCH", "Git blob size does not match the tree", 502
                    )
                self._write(entry.path, data, entry.mode == "100755")
                files.append(
                    FileEntry(path=entry.path, mode=entry.mode, size=len(data), blob=entry.sha)
                )
        return files, exclusions, scanned_bytes

    def _write(self, path: str, data: bytes, executable: bool) -> None:
        destination = self.worktree.joinpath(*path.split("/"))
        root = self.worktree.resolve()
        current = self.worktree
        for part in destination.parent.relative_to(self.worktree).parts:
            current = current / part
            if current.is_symlink():
                raise DomainError("WORKSPACE_SYMLINK_DENIED", "Workspace path is a symlink", 403)
            if not current.exists():
                current.mkdir()
        if destination.is_symlink():
            raise DomainError("WORKSPACE_SYMLINK_DENIED", "Workspace file is a symlink", 403)
        if not destination.resolve().is_relative_to(root):
            raise DomainError("WORKSPACE_PATH_DENIED", "Workspace path escapes its root", 403)
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(destination, flags, 0o755 if executable else 0o644)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)

    def _manifest(
        self,
        *,
        entries: list[_TreeEntry],
        files: list[FileEntry],
        exclusions: list[Exclusion],
        scanned_bytes: int,
        tree: str,
        ref: str,
        clean: bool,
    ) -> RepositoryManifest:
        directories = self._directories(files)
        languages = self._languages(files)
        inventory = self._inventory(entries)
        payload = {
            "manifest_version": MANIFEST_VERSION,
            "repository": self.repository,
            "commit": self.commit,
            "tree": tree,
            "limits": self.limits.model_dump(mode="json"),
            "git": {"head": self.commit, "ref": ref, "clean": clean},
            "files": [item.model_dump(mode="json") for item in files],
            "directories": [item.model_dump(mode="json") for item in directories],
            "languages": languages,
            "inventory": inventory.model_dump(mode="json"),
            "exclusions": [item.model_dump(mode="json") for item in exclusions],
            "coverage": {
                "tree_entries": len(entries),
                "scanned_files": len(files),
                "excluded_entries": len(exclusions),
                "scanned_bytes": scanned_bytes,
            },
        }
        return RepositoryManifest(
            repository=self.repository,
            commit=self.commit,
            tree=tree,
            limits=self.limits,
            git=GitState(head=self.commit, ref=ref, clean=clean),
            files=files,
            directories=directories,
            languages=languages,
            inventory=inventory,
            exclusions=exclusions,
            coverage=Coverage(
                tree_entries=len(entries),
                scanned_files=len(files),
                excluded_entries=len(exclusions),
                scanned_bytes=scanned_bytes,
            ),
            manifest_hash=digest(payload),
        )

    @staticmethod
    def _directories(files: list[FileEntry]) -> list[DirectoryEntry]:
        counts: dict[str, list[int]] = {}
        for item in files:
            segments = item.path.split("/")[:-1]
            for index in range(1, len(segments) + 1):
                path = "/".join(segments[:index])
                counts.setdefault(path, [0, 0])[0] += 1
                counts[path][1] += item.size
        children: dict[str, int] = {}
        for path in counts:
            parent = path.rsplit("/", 1)[0] if "/" in path else ""
            children[parent] = children.get(parent, 0) + 1
        return [
            DirectoryEntry(
                path=path,
                files=counts[path][0],
                bytes=counts[path][1],
                subdirectories=children.get(path, 0),
            )
            for path in sorted(counts)
        ]

    @staticmethod
    def _languages(files: list[FileEntry]) -> dict[str, int]:
        totals: dict[str, int] = {}
        for item in files:
            language = _language(item.path)
            totals[language] = totals.get(language, 0) + item.size
        return dict(sorted(totals.items()))

    @staticmethod
    def _inventory(entries: list[_TreeEntry]) -> Inventory:
        paths = [
            entry.path
            for entry in entries
            if entry.kind == "blob" and entry.mode in {"100644", "100755"}
        ]
        manifests = sorted({path for path in paths if _ecosystem(path) is not None})
        ecosystems = sorted(
            {ecosystem for path in manifests if (ecosystem := _ecosystem(path)) is not None}
        )
        ci = sorted({path for path in paths if _is_ci(path)})
        tests = sorted(
            {
                "/".join(path.split("/")[: index + 1])
                for path in paths
                for index, segment in enumerate(path.split("/")[:-1])
                if segment.lower() in _TEST_DIRECTORIES
            }
        )
        return Inventory(
            dependency_manifests=manifests,
            ecosystems=ecosystems,
            ci_configs=ci,
            test_directories=tests,
        )

    def _write_marker(self, manifest: RepositoryManifest) -> None:
        marker = {
            "workspace_version": WORKSPACE_VERSION,
            "repository": self.repository,
            "commit": self.commit,
            "remote_kind": self.remote_kind,
            "manifest_hash": manifest.manifest_hash,
        }
        temporary = self.marker.with_suffix(".tmp")
        temporary.write_text(__import__("json").dumps(marker, indent=2, sort_keys=True) + "\n")
        temporary.replace(self.marker)
