"""Verified GitHub commit metadata plus official MCP file reads; never executes source."""

import asyncio
import hashlib
import os
import re
import subprocess
from collections.abc import Callable
from pathlib import PurePosixPath
from typing import Any

from mgar_v3.domain import DomainError, digest
from mgar_v3.mcp.client import ReadOnlyGitHubMCP
from mgar_v3.repair.domain import GitEntry, Snapshot
from mgar_v3.repair.workspace import blob, tree, validate_path
from mgar_v3.settings import ROOT


def git_tree(entries: dict[str, GitEntry]) -> str:
    nodes: dict[str, Any] = {}
    for name, entry in entries.items():
        parts = name.split("/")
        if (
            PurePosixPath(name).is_absolute()
            or "\\" in name
            or any(p in {"", ".", "..", ".git"} for p in parts)
        ):
            raise DomainError("SOURCE_TREE_DENIED", "Git tree contains an unsupported path")
        current = nodes
        for part in parts[:-1]:
            current = current.setdefault(part, {})
        if parts[-1] in current:
            raise DomainError("SOURCE_TREE_DENIED", "Git entry paths overlap")
        current[parts[-1]] = entry

    def build(values: dict[str, Any]) -> str:
        raw = b""
        for name, value in sorted(
            values.items(), key=lambda kv: kv[0] + ("/" if isinstance(kv[1], dict) else "")
        ):
            mode = "40000" if isinstance(value, dict) else value.mode
            sha = build(value) if isinstance(value, dict) else value.sha
            raw += mode.encode() + b" " + name.encode() + b"\0" + bytes.fromhex(sha)
        return hashlib.sha1(b"tree " + str(len(raw)).encode() + b"\0" + raw).hexdigest()

    return build(nodes)


def candidate_tree(snapshot: Snapshot, files: dict[str, str]) -> str:
    if not snapshot.tree_entries:
        return tree(files)
    entries = dict(snapshot.tree_entries)
    for path, content in files.items():
        old = entries.get(path)
        if old is None or old.mode != "100644":
            raise DomainError(
                "SOURCE_TREE_DENIED", "Repair source must be an existing regular file"
            )
        entries[path] = GitEntry(mode="100644", sha=blob(content.encode()))
    return git_tree(entries)


def working_files(snapshot: Snapshot, files: dict[str, str]) -> dict[str, str]:
    prefix = snapshot.workspace_prefix + "/" if snapshot.workspace_prefix else ""
    if any(not p.startswith(prefix) for p in files):
        raise DomainError("SOURCE_SCOPE_DENIED", "Source escaped the approved test subdirectory")
    result = {p[len(prefix) :]: v for p, v in files.items()}
    if set(result) != {"go.mod", "orders.go", "orders_test.go"}:
        raise DomainError("SOURCE_SCOPE_DENIED", "The Go repair workspace must contain three files")
    return result


def scope_hash(snapshot: Snapshot) -> str:
    payload = snapshot.model_dump(mode="json")
    if (
        snapshot.origin == "fixture_git"
        and not snapshot.workspace_prefix
        and not snapshot.tree_entries
    ):
        payload.pop("workspace_prefix", None)
        payload.pop("tree_entries", None)
    return digest(payload)


def verify_snapshot(snapshot: Snapshot) -> None:
    if candidate_tree(snapshot, snapshot.files) != snapshot.base_tree:
        raise DomainError("SOURCE_TREE_CHANGED", "Frozen source differs from its Git tree")
    if snapshot.origin == "github_commit":
        if not snapshot.tree_entries or not snapshot.workspace_prefix:
            raise DomainError(
                "SOURCE_PROVENANCE_REQUIRED", "GitHub sources require full Git tree proof"
            )
        for path, content in snapshot.files.items():
            if snapshot.tree_entries[path].sha != blob(content.encode()):
                raise DomainError(
                    "SOURCE_BLOB_CHANGED", "MCP source bytes differ from verified Git blobs"
                )
        work = working_files(snapshot, snapshot.files)
        if work["go.mod"] != "module example.invalid/mgar-v3/orders\n\ngo 1.23\n":
            raise DomainError(
                "SOURCE_BUILD_DENIED", "Repair supports the offline order example module"
            )


def _git_repository(repository: str) -> Callable[[list[str]], bytes]:
    directory = ROOT / ".local/r5/git" / hashlib.sha256(repository.encode()).hexdigest()[:24]
    directory.parent.mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="/dev/null", GIT_TERMINAL_PROMPT="0")

    def git(args: list[str]) -> bytes:
        result = subprocess.run(
            ["git", "--git-dir=" + str(directory), "-c", "core.hooksPath=/dev/null", *args],
            env=env,
            capture_output=True,
            timeout=60,
        )
        if result.returncode:
            raise DomainError("SOURCE_FETCH_FAILED", "GitHub commit metadata could not be verified")
        if len(result.stdout) > 1024 * 1024:
            raise DomainError("SOURCE_TREE_LIMIT", "Git tree metadata exceeds its bound")
        return result.stdout

    if not directory.exists():
        subprocess.run(
            ["git", "init", "--bare", str(directory)], env=env, capture_output=True, check=True
        )
    return git


def github_commit(repository: str, ref: str) -> tuple[dict[str, Any], dict[str, GitEntry]]:
    repository = repository.lower()
    if not re.fullmatch(r"[a-z0-9_.-]+/[a-z0-9_.-]+", repository) or not re.fullmatch(
        r"[a-f0-9]{40}", ref
    ):
        raise DomainError(
            "SOURCE_REF_DENIED", "A configured repository and immutable commit are required"
        )
    git = _git_repository(repository)
    git(["fetch", "--no-tags", "--depth=2", "https://github.com/" + repository + ".git", ref])
    headers = git(["cat-file", "commit", ref]).split(b"\n\n", 1)[0].decode().splitlines()
    root = next(line[5:] for line in headers if line.startswith("tree "))
    parents = [{"sha": line[7:]} for line in headers if line.startswith("parent ")]
    entries = {}
    for line in git(["ls-tree", "-rz", ref]).split(b"\0"):
        if not line:
            continue
        info, path = line.split(b"\t", 1)
        mode, _, sha = info.decode().split()
        entries[path.decode()] = GitEntry.model_validate({"mode": mode, "sha": sha})
    if len(entries) > 2048 or git_tree(entries) != root:
        raise DomainError("SOURCE_TREE_CHANGED", "Commit tree proof failed")
    files = []
    if len(parents) == 1:
        names = git(
            ["diff-tree", "--no-commit-id", "--name-only", "-r", "-z", parents[0]["sha"], ref]
        ).split(b"\0")
        files = [
            {
                "filename": name.decode(),
                "sha": entries[name.decode()].sha if name.decode() in entries else None,
            }
            for name in names
            if name
        ]
    return {
        "sha": ref,
        "parents": parents,
        "commit": {"tree": {"sha": root}},
        "files": files,
    }, entries


def freeze_github(
    repository: str, ref: str, prefix: str, branch: str, reader: ReadOnlyGitHubMCP
) -> Snapshot:
    repository = repository.lower()
    validate_path(prefix)
    metadata, entries = github_commit(repository, ref)
    root = metadata["commit"]["tree"]["sha"]

    async def reads() -> dict[str, str]:
        result = {}
        for name in ("go.mod", "orders.go", "orders_test.go"):
            path = prefix + "/" + name
            entry = entries.get(path)
            if entry is None or entry.mode != "100644":
                raise DomainError(
                    "SOURCE_PATH_DENIED", "Repair target is not a regular tracked file"
                )
            source = await reader.read_file(path, ref)
            if source.version != entry.sha or blob(source.content) != entry.sha:
                raise DomainError("SOURCE_BLOB_CHANGED", "MCP source and fetched Git commit differ")
            result[path] = source.content.decode()
        return result

    snapshot = Snapshot(
        repository=repository,
        base_branch=branch,
        base_sha=ref,
        base_tree=root,
        origin="github_commit",
        files=asyncio.run(reads()),
        editable_paths=[prefix + "/orders.go"],
        expected_tests=["TestPaidRevenueCents"],
        workspace_prefix=prefix,
        tree_entries=entries,
    )
    verify_snapshot(snapshot)
    return snapshot
