import asyncio
import base64
import hashlib
import json
import random
import re
from pathlib import Path
from typing import Any

from mgar_v3.agents.data import provision
from mgar_v3.benchmark.domain import Case
from mgar_v3.coordination.engine import Engine
from mgar_v3.domain import DomainError, digest
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.live import LiveProfile
from mgar_v3.mcp.factory import github_client
from mgar_v3.rag.domain import ACLUpdate, Upload
from mgar_v3.repair.source import freeze_github
from mgar_v3.settings import ROOT, Settings

DATA = ROOT / "eval/final-v1"
DATASETS = {"final-v1": DATA, "final-v3": ROOT / "eval/final-v3"}


def fingerprint() -> str:
    h = hashlib.sha256()
    for folder in ("src", "migrations", "scripts", "config", "eval/final-v1", "eval/final-v3"):
        for path in sorted((ROOT / folder).rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts:
                h.update(str(path.relative_to(ROOT)).encode() + b"\0" + path.read_bytes() + b"\0")
    return h.hexdigest()


def write(path: Path, value: Any) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n")
    path.chmod(0o600)


def configuration(
    directory: Path, project: str, paid: bool = False, local_rag: bool = True
) -> Settings:
    return Settings(
        project_id=project,
        artifact_root=directory / "artifacts",
        allow_paid=paid,
        allow_external_write=False,
        router_mode="disabled",
        rag_mode="local" if local_rag else "fixture",
        github_use_gh_token=True,
        github_mcp_binary=ROOT / ".local/tools/github-mcp-server/v2.0.3/github-mcp-server",
    )


def prepare(
    directory: Path,
    source_ref: str,
    ablation: str = "none",
    dataset: str = "final-v1",
    max_new_calls: int = 270,
) -> dict[str, Any]:
    if not re.fullmatch(r"[a-f0-9]{40}", source_ref):
        raise ValueError("Source ref must be a full immutable Git commit")
    if type(max_new_calls) is not int or not 1 <= max_new_calls <= 270:
        raise ValueError("Shared call envelope must be between 1 and 270")
    if ablation not in {"none", "no_reranker", "no_evidence_repair", "no_verifier"}:
        raise ValueError("Unknown ablation")
    if (directory / "manifest.json").exists():
        raise DomainError("FROZEN_EXISTS", "Frozen evaluation cannot be overwritten")
    if dataset not in DATASETS:
        raise ValueError("Unknown public dataset revision")
    cases = [
        Case.model_validate(c) for c in json.loads((DATASETS[dataset] / "cases.json").read_text())
    ]
    if len(cases) != 30 or {
        c.category: sum(x.category == c.category for x in cases) for c in cases
    } != {"qa": 10, "cross": 10, "repair": 5, "negative": 5}:
        raise ValueError("Frozen 10/10/5/5 case split required")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    project = (
        "r6-final-"
        + digest(
            [
                source_ref,
                ablation,
                str(directory.resolve()),
                [c.model_dump(mode="json") for c in cases],
            ]
        )[:12]
    )
    projects = {
        project: json.loads((DATA / "corpus.json").read_text()),
        project + "-legacy": [
            {
                "id": "legacy-contract",
                "text": (
                    "# 收入口径 legacy-2024\n旧版本按所有事件累计金额，"
                    "不能证明 2026-final-v1 行为。"
                ),
                "version": "legacy-2024",
            }
        ],
        project + "-revoked": [
            {
                "id": "private-incident",
                "text": "# 私有事件\nThis revoked source must never reach a model.",
                "version": "private-v1",
            }
        ],
    }
    frozen_projects = {}
    readers = {}
    profiles: list[dict[str, Any]] = []
    source_files = {}
    snapshots = {}
    cfg = configuration(directory, project)
    engine = Engine(cfg, ArtifactStore(cfg.artifact_root))
    try:
        with engine.transaction() as conn:
            prior = {k: int(v) for k, v in engine.benchmarks.cost(conn, cfg.repository).items()}
        if prior["unknown"]:
            raise DomainError("USAGE_UNKNOWN", "Reconcile prior usage before freezing evaluation")
        for name, docs in projects.items():
            cfg.project_id = name
            ids = {}
            for doc in docs:
                uploaded = engine.knowledge.upload(
                    Upload(
                        title=doc["id"],
                        source_uri="kb://final-v1/" + doc["id"],
                        source_version=doc["version"],
                        media_type="text/markdown",
                        content_base64=base64.b64encode(doc["text"].encode()).decode(),
                    )
                )
                engine.knowledge.index(uploaded["job_id"])
                with engine.transaction() as conn:
                    document = conn.execute(
                        "SELECT v.document_id FROM knowledge.ingestion_jobs j "
                        "JOIN knowledge.document_versions v USING(version_id) WHERE j.job_id=%s",
                        (uploaded["job_id"],),
                    ).fetchone()
                    assert document is not None
                    ids[doc["id"]] = str(document["document_id"])
            snapshot = engine.knowledge.active()
            frozen_projects[name] = {
                "snapshot_id": str(snapshot),
                "rag_config_hash": digest(engine.knowledge.config()),
                "documents": ids,
            }
            if name.endswith("-revoked"):
                from uuid import UUID

                engine.knowledge.acl(UUID(ids["private-incident"]), ACLUpdate(revoked=True))
        cfg.project_id = project
        for case in cases:
            if case.rows is not None:
                reader_path = directory / case.id / "reader.json"
                reader_profile = provision(engine, reader_path, case.rows)
                readers[case.id] = {
                    "path": str(reader_path.resolve()),
                    "fixture_hash": reader_profile["fixture_hash"],
                }
        client = github_client(cfg, cfg.repository)
        for path in dict.fromkeys(c.source_path for c in cases):
            if not path.startswith("v3/eval/final-v1/public/") or ".." in Path(path).parts:
                raise ValueError("Source scope escaped public benchmark inputs")
            source = asyncio.run(client.read_file(path, source_ref))
            source_files[path] = {
                "content": source.content.decode(),
                "blob": source.version,
                "sha256": hashlib.sha256(source.content).hexdigest(),
            }
        for case in cases:
            if case.category == "repair":
                prefix = str(Path(case.source_path).parent)
                snapshots[case.id] = freeze_github(
                    cfg.repository, source_ref, prefix, "main", client
                ).model_dump(mode="json")
        price = json.loads((ROOT / "config/live-r1-proposed.json").read_text())
        for case in cases:
            case_project = project + (
                "-legacy"
                if case.negative_condition == "version_conflict"
                else "-revoked"
                if case.negative_condition == "revoked"
                else ""
            )
            for arm in "ABC":
                profile = LiveProfile(
                    case_id=f"r6-eval:{dataset}:{source_ref[:12]}:{ablation}:{case.id}:{arm}",
                    project_id=case_project,
                    repository=cfg.repository,
                    issue_number=1,
                    actor=cfg.local_actor,
                    question=case.question,
                    source_path=case.source_path,
                    source_ref=source_ref,
                    expected_source_hash=source_files[case.source_path]["sha256"],
                    model_id=price["model_id"],
                    price_version=price["price_version"],
                    input_micros_per_million=price["input_micros_per_million"],
                    output_micros_per_million=price["output_micros_per_million"],
                    context_tokens=12288,
                    max_output_tokens=2048,
                    max_source_bytes=8192,
                    max_model_calls=3,
                    max_cost_micros=20000,
                )
                profile = profile.model_copy(
                    update={"max_cost_micros": 3 * profile.reservation_micros}
                )
                profiles.append(
                    {
                        "case_id": case.id,
                        "arm": arm,
                        "profile": profile.model_dump(mode="json"),
                        "profile_hash": profile.profile_hash,
                    }
                )
    finally:
        engine.close()
    order = [
        {"case_id": c.id, "category": c.category, "arm": a, "status": "NOT_RUN"}
        for c in cases
        for a in "ABC"
    ]
    random.Random(20261103).shuffle(order)
    per_call = LiveProfile.model_validate(profiles[0]["profile"]).reservation_micros
    maximum = min(sum(p["profile"]["max_cost_micros"] for p in profiles), max_new_calls * per_call)
    if maximum + prior["cost_micros"] > 2000000:
        raise DomainError("BUDGET_EXHAUSTED", "Final plan exceeds cumulative USD 2")
    write(
        directory / "inputs.json",
        {
            "projects": frozen_projects,
            "readers": readers,
            "sources": source_files,
            "repair_snapshots": snapshots,
        },
    )
    oracle_bytes = (DATA / "oracles.json").read_bytes()
    manifest = {
        "schema": "r6-final-evaluation-v1",
        "dataset": dataset,
        "dataset_cases_sha256": hashlib.sha256(
            (DATASETS[dataset] / "cases.json").read_bytes()
        ).hexdigest(),
        "repository": cfg.repository,
        "source_ref": source_ref,
        "implementation_sha256": fingerprint(),
        "cases": [c.model_dump(mode="json") for c in cases],
        "profiles": profiles,
        "assignments": order,
        "seed": 20261103,
        "max_new_calls": max_new_calls,
        "max_new_cost_micros": maximum,
        "prior_cost_micros": prior["cost_micros"],
        "cumulative_limit_micros": 2000000,
        "inputs_sha256": hashlib.sha256((directory / "inputs.json").read_bytes()).hexdigest(),
        "oracle_sha256": hashlib.sha256(oracle_bytes).hexdigest(),
        "budget_per_task": {
            "calls": 3,
            "context_tokens": 12288,
            "max_output_tokens": 2048,
            "logical_tool_calls": 16,
            "deadline_seconds": 180,
        },
        "quality_thresholds": {
            "C_total_min": 24,
            "normal_min": 20,
            "negative_required": 5,
            "citation_validity": 1,
        },
        "external_writes": "disabled_readonly_Engine; offline_deduplicated_result_sink",
        "cache_conditions": "same frozen GitHub sources; shared warm pretrained embedding"
        "/ranker; fresh per-task evidence",
        "comparison_scope": "common bounded evidence acquisition; A single analyst; B fix"
        "ed knowledge/repo summaries; C required-role summaries",
        "model_revision": "provider alias pinned; server revision unavailable, verify r"
        "eturned model and randomize order",
        "exposure": (
            "developer-visible repair regression reusing the first-round business inputs and "
            "oracle; the same model alias previously answered these tasks; no blind holdout; "
            "no oracle or archived answers supplied to runtime"
            if dataset == "final-v3"
            else "new revision/data variants; developer-visible; runtime model"
            " has not seen oracle or these task outputs"
        ),
        "ablations": ["no_reranker", "no_evidence_repair", "no_verifier"],
        "ablation": ablation,
    }
    write(directory / "manifest.json", manifest)
    write(directory / "results.json", order)
    return manifest
