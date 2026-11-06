"""Operator-only registration of owned demo sources; no model or GitHub writes."""

import base64
import hashlib
import json
import subprocess
from typing import TYPE_CHECKING, Any

from mgar_v3.agents.data import provision
from mgar_v3.domain import digest
from mgar_v3.rag.domain import Upload
from mgar_v3.repair.domain import GitEntry, Snapshot
from mgar_v3.repair.source import verify_snapshot
from mgar_v3.settings import ROOT

if TYPE_CHECKING:
    from mgar_v3.coordination.engine import Engine


def owned_snapshot(repository: str) -> Snapshot:
    repository_root = ROOT.parent

    def git(*args: str) -> str:
        return subprocess.check_output(["git", *args], cwd=repository_root).decode()

    base = git("rev-parse", "HEAD").strip()
    entries = {}
    for item in git("ls-tree", "-r", "-z", base).split("\0"):
        if not item:
            continue
        meta, path = item.split("\t", 1)
        mode, kind, sha = meta.split()
        if kind == "blob":
            entries[path] = GitEntry(mode=mode, sha=sha)
    prefix = "v3/examples/repair-target"
    files = {
        prefix + "/" + p: git("show", base + ":" + prefix + "/" + p)
        for p in ("go.mod", "orders.go", "orders_test.go")
    }
    snapshot = Snapshot(
        repository=repository,
        base_sha=base,
        base_tree=git("rev-parse", base + "^{tree}").strip(),
        origin="fixture_git",
        files=files,
        editable_paths=[prefix + "/orders.go"],
        workspace_prefix=prefix,
        tree_entries=entries,
        expected_tests=["TestPaidRevenueCents"],
    )
    verify_snapshot(snapshot)
    return snapshot


def setup(engine: "Engine") -> list[dict[str, Any]]:
    existing = engine.commerce.templates()
    if len(existing) == 3:
        return existing
    data = json.loads((ROOT / "fixtures/commerce/data.json").read_text())
    policy = (ROOT / "fixtures/commerce/policy.md").read_bytes()
    job = engine.knowledge.upload(
        Upload(
            title="Commerce revenue policy v1",
            source_uri="kb://commerce/revenue-v1",
            source_version="commerce-revenue-v1",
            media_type="text/markdown",
            content_base64=base64.b64encode(policy).decode(),
        )
    )
    snapshot_id = engine.knowledge.index(job["job_id"])
    knowledge, chunks, _ = engine.knowledge.eligible(snapshot_id, engine.settings.local_actor)
    source = owned_snapshot(engine.settings.repository.lower())
    snapshot_bytes = source.model_dump_json().encode()
    source_hash = engine.repairs.artifact(snapshot_bytes)
    directory = engine.settings.commerce_directory
    for kind, rows in data["datasets"].items():
        path = directory / (kind + "-reader.json")
        reader = provision(engine, path, rows)
        manifest = {
            "kind": kind,
            "execution_mode": "fixture",
            "project_id": engine.settings.project_id,
            "repository": engine.settings.repository.lower(),
            "actor": engine.settings.local_actor,
            "reader_path": str(path.resolve()),
            "data_profile": reader,
            "snapshot_hash": source_hash,
            "source_ref": source.base_sha,
            "source_tree": source.base_tree,
            "source_path": source.editable_paths[0],
            "source_content_hash": hashlib.sha256(
                source.files[source.editable_paths[0]].encode()
            ).hexdigest(),
            "knowledge_snapshot_ids": [str(snapshot_id)],
            "knowledge_snapshot_hash": knowledge["manifest_hash"],
            "knowledge_config_hash": knowledge["config_hash"],
            "knowledge_document_ids": sorted({str(c["document_id"]) for c in chunks}),
            "policy": data["policy"],
            "acceptance_profile_hash": digest({"policy": data["policy"], "task_kind": kind}),
        }
        engine.commerce.register(kind, manifest)
    return engine.commerce.templates()
