"Immutable snapshots and exact, bounded unified diffs; no candidate host execution."

import difflib
import hashlib
import json
import re
import subprocess
from pathlib import PurePosixPath
from typing import Any

from mgar_v3.domain import DomainError, digest
from mgar_v3.repair.domain import Snapshot
from mgar_v3.settings import ROOT


def blob(value: bytes) -> str:
    return hashlib.sha1(b"blob " + str(len(value)).encode() + b"\0" + value).hexdigest()


def tree(files: dict[str, str]) -> str:
    def build(entries: dict[str, Any]) -> bytes:
        content = b""
        for name, value in sorted(
            entries.items(), key=lambda kv: kv[0] + ("/" if isinstance(kv[1], dict) else "")
        ):
            nested = isinstance(value, dict)
            sha = (
                hashlib.sha1(
                    b"tree " + str(len(build(value))).encode() + b"\0" + build(value)
                ).hexdigest()
                if nested
                else blob(value.encode())
            )
            content += (
                (b"40000" if nested else b"100644")
                + b" "
                + name.encode()
                + b"\0"
                + bytes.fromhex(sha)
            )
        return content

    nodes: dict[str, Any] = {}
    for path, value in files.items():
        validate_path(path)
        current = nodes
        parts = path.split("/")
        for part in parts[:-1]:
            current = current.setdefault(part, {})
        current[parts[-1]] = value
    raw = build(nodes)
    return hashlib.sha1(b"tree " + str(len(raw)).encode() + b"\0" + raw).hexdigest()


def validate_path(value: str) -> None:
    path = PurePosixPath(value)
    if (
        not re.fullmatch(r"[A-Za-z0-9_./-]{1,200}", value)
        or path.is_absolute()
        or any(
            part in {".", "..", ".git", ".env"} or part.startswith(".") for part in value.split("/")
        )
    ):
        raise DomainError("PATCH_PATH_DENIED", "Source path is outside the repair scope", 403)


def fixture_snapshot(repository: str) -> Snapshot:
    if any(p.is_symlink() for p in (ROOT / "fixtures/repair-demo").iterdir()):
        raise DomainError("PATCH_PATH_DENIED", "Symlinks are not permitted in repair sources", 403)
    files = {
        p.name: p.read_text() for p in (ROOT / "fixtures/repair-demo").iterdir() if p.is_file()
    }
    if set(files) != {"orders.go", "orders_test.go", "go.mod"}:
        raise DomainError("REPAIR_SOURCE_CHANGED", "Demo source inventory changed")
    base_tree = tree(files)
    commit = (
        f"tree {base_tree}\n"
        "author MGAR Fixture <fixture@example.invalid> 1761955200 +0000\n"
        "committer MGAR Fixture <fixture@example.invalid> 1761955200 +0000\n"
        "\nV3 owned repair baseline\n"
    ).encode()
    base = hashlib.sha1(b"commit " + str(len(commit)).encode() + b"\0" + commit).hexdigest()
    # Materialize the immutable owned Git snapshot without touching the adjacent repository.
    root = ROOT / ".local/r5/demo.git"
    if not root.exists():
        subprocess.run(["git", "init", "--bare", str(root)], capture_output=True, check=True)
    for content in files.values():
        subprocess.run(
            ["git", "--git-dir=" + str(root), "hash-object", "-w", "--stdin"],
            input=content.encode(),
            capture_output=True,
            check=True,
        )
    entries = "".join(
        f"100644 blob {blob(value.encode())}\t{name}\n" for name, value in sorted(files.items())
    )
    actual = (
        subprocess.check_output(["git", "--git-dir=" + str(root), "mktree"], input=entries.encode())
        .decode()
        .strip()
    )
    if actual != base_tree:
        raise DomainError("GIT_TREE_MISMATCH", "Source tree does not match Git")
    actual = (
        subprocess.check_output(
            ["git", "--git-dir=" + str(root), "hash-object", "-w", "-t", "commit", "--stdin"],
            input=commit,
        )
        .decode()
        .strip()
    )
    if actual != base:
        raise DomainError("GIT_COMMIT_MISMATCH", "Source commit does not match Git")
    return Snapshot(
        repository=repository,
        base_sha=base,
        base_tree=base_tree,
        origin="fixture_git",
        files=files,
        editable_paths=["orders.go"],
        expected_tests=["TestPaidRevenueCents"],
    )


def workspace_hash(files: dict[str, str]) -> str:
    return digest({p: hashlib.sha256(v.encode()).hexdigest() for p, v in sorted(files.items())})


def apply_diff(snapshot: Snapshot, diff: str) -> dict[str, str]:
    if len(diff.encode()) > 32768 or "\r" in diff or "\0" in diff or len(diff.splitlines()) > 250:
        raise DomainError("PATCH_LIMIT", "Patch exceeds its size or line bound")
    lines = diff.splitlines(keepends=True)
    index = 0
    files = dict(snapshot.files)
    changed: set[str] = set()
    while index < len(lines):
        if lines[index].startswith("diff --git "):
            index += 1
            if index < len(lines) and re.fullmatch(
                r"index [a-f0-9]+\.\.[a-f0-9]+ 100644\n", lines[index]
            ):
                index += 1
        if (
            index + 1 >= len(lines)
            or not lines[index].startswith("--- a/")
            or not lines[index + 1].startswith("+++ b/")
        ):
            raise DomainError("PATCH_FORMAT", "Only existing-file unified diffs are supported")
        before = lines[index][6:].strip()
        after = lines[index + 1][6:].strip()
        index += 2
        validate_path(before)
        validate_path(after)
        if (
            before != after
            or after not in snapshot.editable_paths
            or after in changed
            or len(changed) >= 2
        ):
            raise DomainError(
                "PATCH_PATH_DENIED", "Patch may only modify the approved source files", 403
            )
        source = snapshot.files[after].splitlines(keepends=True)
        output = []
        cursor = 0
        hunks = 0
        while index < len(lines) and lines[index].startswith("@@ "):
            match = re.fullmatch(
                r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@[^\n]*\n", lines[index]
            )
            if not match:
                raise DomainError("PATCH_FORMAT", "Hunk header is invalid")
            old_start = int(match[1]) - 1
            old_count = int(match[2] or "1")
            new_start = int(match[3]) - 1
            new_count = int(match[4] or "1")
            if old_start < cursor or old_start > len(source):
                raise DomainError("PATCH_STALE", "Patch hunk is outside its original source")
            output.extend(source[cursor:old_start])
            cursor = old_start
            if len(output) != new_start:
                raise DomainError("PATCH_STALE", "New hunk offset differs")
            index += 1
            old_seen = 0
            new_seen = 0
            while (
                index < len(lines)
                and lines[index][:1] in {" ", "+", "-"}
                and not lines[index].startswith(("--- a/", "+++ b/"))
            ):
                line = lines[index]
                prefix = line[0]
                content = line[1:]
                if prefix in {" ", "-"}:
                    if cursor >= len(source) or source[cursor] != content:
                        raise DomainError(
                            "PATCH_STALE", "Patch context does not match exact source bytes"
                        )
                    cursor += 1
                    old_seen += 1
                if prefix in {" ", "+"}:
                    output.append(content)
                    new_seen += 1
                index += 1
            if (old_seen, new_seen) != (old_count, new_count):
                raise DomainError(
                    "PATCH_FORMAT",
                    f"Hunk counts declare {old_count}/{new_count}; "
                    f"actual removed/context and added/context counts are {old_seen}/{new_seen}",
                )
            hunks += 1
        if not hunks:
            raise DomainError("PATCH_FORMAT", "Patch has no hunks")
        output.extend(source[cursor:])
        files[after] = "".join(output)
        changed.add(after)
        if len(files[after].encode()) > 32768 or not files[after].endswith("\n"):
            raise DomainError("PATCH_LIMIT", "Candidate source exceeds its bound")
    if not changed or files == snapshot.files:
        raise DomainError("PATCH_EMPTY", "Patch makes no source change")
    editable = snapshot.editable_paths[0]
    ast_check(snapshot.files[editable], files[editable])
    return files


def ast_check(before: str, after: str) -> None:
    lock = ROOT / ".local/r5/sandbox-lock.json"
    binary = ROOT / ".local/r5/astcheck"
    if not lock.is_file() or not binary.is_file():
        raise DomainError("SANDBOX_NOT_PREPARED", "Run make sandbox-build first")
    receipt = json.loads(lock.read_text())
    if hashlib.sha256(binary.read_bytes()).hexdigest() != receipt["astcheck_sha256"]:
        raise DomainError("AST_HELPER_CHANGED", "Trusted parser helper hash changed")
    result = subprocess.run(
        [str(binary)],
        input=json.dumps({"Before": before, "After": after}).encode(),
        capture_output=True,
        timeout=3,
        env={"PATH": "/usr/bin:/bin"},
    )
    if result.returncode != 0:
        raise DomainError(
            "PATCH_AST_DENIED", "Only the approved pure function body may change", 403
        )


def fixture_diff(snapshot: Snapshot, attempt: int) -> str:
    # Explicit controlled provider, not a model result or a V2 patch catalogue.
    before = snapshot.files["orders.go"]
    body = """func PaidRevenueCents(orders []Order) int {
    total := 0
    counted := make(map[string]bool)
    for _, order := range orders {
        if order.Status == "paid" && !counted[order.ID] {
            counted[order.ID] = true
            total += order.Amount
        }
    }
    return total
}
"""
    after = before[: before.index("func PaidRevenueCents")] + body
    return "".join(
        difflib.unified_diff(
            before.splitlines(True),
            after.splitlines(True),
            fromfile="a/orders.go",
            tofile="b/orders.go",
        )
    )
