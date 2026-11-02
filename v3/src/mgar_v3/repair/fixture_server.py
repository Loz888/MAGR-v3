"""Explicit persistent MCP protocol fixture. It never contacts GitHub."""

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from mgar_v3.repair.domain import GitEntry
from mgar_v3.repair.source import git_tree
from mgar_v3.repair.workspace import blob, tree

server = FastMCP("r5-controlled-draft-remote")
state_path = Path(sys.argv[1])


def load() -> dict[str, Any]:
    assert not any(
        os.environ.get(k)
        for k in (
            "GITHUB_PERSONAL_ACCESS_TOKEN",
            "MGAR_V3_DEEPSEEK_API_KEY",
            "MGAR_V3_DATABASE_URL",
            "MGAR_V3_LOCAL_TOKEN",
        )
    )
    return dict(json.loads(state_path.read_text()))


def save(state: dict[str, Any], kind: str) -> None:
    state["writes"].append(kind)
    temporary = state_path.with_suffix(".pending")
    temporary.write_text(json.dumps(state))
    temporary.replace(state_path)
    if state.get("lose_response") == kind:
        raise RuntimeError("Controlled lost response after acceptance")


def scope(state: dict[str, Any], owner: str, repo: str) -> None:
    if owner + "/" + repo != state["repository"]:
        raise ValueError("Repository denied")


@server.tool()
def get_commit(
    owner: str, repo: str, sha: str, perPage: int = 100, page: int = 1, detail: str = "stats"
) -> dict[str, Any]:
    state = load()
    scope(state, owner, repo)
    if page != 1 or perPage != 100:
        raise ValueError("Unsupported page")
    key = state["branches"].get(sha, sha)
    value = dict(state["commits"][key])
    if state.get("minimal_commit"):
        return {
            "sha": value["sha"],
            "commit": {"message": "Native MCP minimal shape"},
            "files": [{"filename": v["filename"], "status": "modified"} for v in value["files"]],
        }
    return value


@server.tool()
def create_branch(owner: str, repo: str, branch: str, from_branch: str) -> dict[str, Any]:
    state = load()
    scope(state, owner, repo)
    if branch in state["branches"]:
        raise ValueError("Branch exists")
    state["branches"][branch] = state["branches"][from_branch]
    save(state, "branch")
    return {"ref": "refs/heads/" + branch, "object": {"sha": state["branches"][branch]}}


@server.tool()
def push_files(
    owner: str, repo: str, branch: str, files: list[dict[str, str]], message: str
) -> dict[str, Any]:
    state = load()
    scope(state, owner, repo)
    parent = state["branches"][branch]
    all_files = {
        **state["commits"][parent]["source_files"],
        **{f["path"]: f["content"] for f in files},
    }
    entries = {
        p: GitEntry.model_validate(v)
        for p, v in state["commits"][parent].get("tree_entries", {}).items()
    }
    if entries:
        for f in files:
            entries[f["path"]] = GitEntry(mode="100644", sha=blob(f["content"].encode()))
    tree_sha = git_tree(entries) if entries else tree(all_files)
    raw = (
        f"tree {tree_sha}\nparent {parent}\n"
        "author Fixture <fixture@example.invalid> 1761955200 +0000\n"
        "committer Fixture <fixture@example.invalid> 1761955200 +0000\n"
        f"\n{message}\n"
    ).encode()
    sha = hashlib.sha1(b"commit " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
    state["commits"][sha] = {
        "sha": sha,
        "parents": [{"sha": parent}],
        "commit": {"tree": {"sha": tree_sha}},
        "files": [{"filename": f["path"], "sha": blob(f["content"].encode())} for f in files],
        "source_files": all_files,
        "tree_entries": {p: v.model_dump() for p, v in entries.items()},
    }
    state["branches"][branch] = sha
    save(state, "commit")
    return {"ref": "refs/heads/" + branch, "object": {"sha": sha}}


@server.tool()
def create_pull_request(
    owner: str,
    repo: str,
    base: str,
    head: str,
    title: str,
    body: str,
    draft: bool,
    maintainer_can_modify: bool = False,
) -> dict[str, Any]:
    state = load()
    scope(state, owner, repo)
    if not draft or maintainer_can_modify:
        raise ValueError("Draft scope denied")
    if any(pr["head"]["ref"] == head for pr in state["prs"]):
        raise ValueError("PR exists")
    pr = {
        "number": len(state["prs"]) + 1,
        "draft": True,
        "body": body,
        "title": title,
        "head": {
            "ref": head,
            "sha": state["branches"][head],
            "repo": {"full_name": owner + "/" + repo},
        },
        "base": {"ref": base, "sha": state["branches"][base]},
        "html_url": "fixture://draft/1",
    }
    state["prs"].append(pr)
    save(state, "pull_request")
    return pr


@server.tool()
def list_pull_requests(
    owner: str,
    repo: str,
    head: str,
    base: str,
    state: str = "all",
    perPage: int = 100,
    page: int = 1,
) -> list[dict[str, Any]]:
    value = load()
    scope(value, owner, repo)
    return [
        p
        for p in value["prs"]
        if owner + ":" + p["head"]["ref"] == head and p["base"]["ref"] == base
    ]


@server.tool()
def pull_request_read(owner: str, repo: str, pullNumber: int, method: str) -> dict[str, Any]:
    state = load()
    scope(state, owner, repo)
    if method != "get":
        raise ValueError("Read method denied")
    return dict(next(p for p in state["prs"] if p["number"] == pullNumber))


if __name__ == "__main__":
    server.run(transport="stdio")
