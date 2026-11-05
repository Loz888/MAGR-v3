import hashlib
import json
from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid4

from mgar_v3.agents.data import DataReader
from mgar_v3.benchmark.domain import Case, Source
from mgar_v3.coordination.engine import Engine
from mgar_v3.domain import DomainError, Evidence, digest
from mgar_v3.rag.domain import Search


def collect(
    engine: Engine,
    run_id: UUID,
    epoch: int,
    case: Case,
    inputs: dict[str, Any],
    *,
    supplement: bool = False,
    ablation: str = "none",
) -> tuple[list[Source], list[str], dict[str, Any]]:
    if case.output_contract is not None:
        from mgar_v3.benchmark.scoped_tools import collect_scoped

        return collect_scoped(
            engine, run_id, epoch, case, inputs, supplement=supplement, ablation=ablation
        )
    sources = []
    errors = []
    calls = 0
    retrieved = []
    project = inputs["projects"][engine.settings.project_id]
    if digest(engine.knowledge.config()) != project["rag_config_hash"]:
        raise DomainError("BENCHMARK_INPUT_CHANGED", "Retrieval model or ranker changed")

    def record(
        role: Literal["knowledge", "repo", "data"], text: str, version: str, uri: str
    ) -> None:
        raw = text.encode()[:2000]
        text = raw.decode(errors="ignore")
        eid = uuid4()
        engine.record_read(
            Evidence(
                evidence_id=eid,
                run_id=run_id,
                project_id=engine.settings.project_id,
                source_uri=uri,
                source_version=version,
                content_hash=hashlib.sha256(text.encode()).hexdigest(),
                start_line=1,
                end_line=max(1, len(text.splitlines())),
                tool_call_id=uuid4(),
            ),
            epoch,
            text.encode(),
        )
        sources.append(Source(id=str(eid), role=role, text=text, version=version))

    try:
        calls += 1
        result = engine.knowledge.search(
            Search(
                query=case.question[:1800] + (" 订单 服务 契约" if supplement else ""),
                snapshot_id=UUID(project["snapshot_id"]),
                top_k=5,
                max_context_bytes=4096,
            ),
            run_id=run_id,
            benchmark_no_reranker=ablation == "no_reranker",
        )
        retrieved = [str(h["document_id"]) for h in result["hits"]]
        for hit in result["hits"][:2]:
            calls += 1
            value = engine.knowledge.read(UUID(hit["chunk_id"]), UUID(project["snapshot_id"]))
            record(
                "knowledge",
                str(value.get("text", value.get("content", ""))),
                str(value.get("version_id", project["snapshot_id"])),
                "kb://final/" + str(hit["chunk_id"]),
            )
    except DomainError as exc:
        if exc.code in {
            "BENCHMARK_INPUT_CHANGED",
            "KNOWLEDGE_SOURCE_MISMATCH",
            "ABLATION_SCOPE_DENIED",
        }:
            raise
        errors.append("knowledge:" + exc.code)
    source = inputs["sources"][case.source_path]
    if hashlib.sha256(source["content"].encode()).hexdigest() != source["sha256"]:
        raise DomainError("BENCHMARK_INPUT_CHANGED", "Frozen GitHub bytes changed")
    calls += 1
    record("repo", source["content"], source["blob"], "github://" + case.source_path)
    try:
        calls += 1
        config = inputs["readers"].get(case.id)
        if config is None:
            raise DomainError("DATA_SOURCE_NOT_CONFIGURED", "No SQL reader profile")
        reader = DataReader(json.loads(Path(config["path"]).read_text()))
        if reader.profile["fixture_hash"] != config["fixture_hash"]:
            raise DomainError("BENCHMARK_INPUT_CHANGED", "SQL fixture changed")
        value = reader.query(
            "SELECT event_id,order_id,status,amount FROM "
            + reader.profile["schema"]
            + ".orders ORDER BY event_id",
            [],
        )
        actual_rows = [
            dict(zip([c["name"] for c in value["columns"]], r, strict=True)) for r in value["rows"]
        ]
        if digest(actual_rows) != config["fixture_hash"]:
            raise DomainError("BENCHMARK_INPUT_CHANGED", "SQL data changed after freezing")
        record(
            "data",
            json.dumps({"columns": value["columns"], "rows": value["rows"]}, ensure_ascii=False),
            config["fixture_hash"],
            "sql://" + reader.profile["schema"] + "/orders",
        )
    except DomainError as exc:
        if exc.code == "BENCHMARK_INPUT_CHANGED":
            raise
        errors.append("data:" + exc.code)
    target = project["documents"].get(case.document_id or "")
    return (
        sources,
        errors,
        {
            "tool_calls": calls,
            "recall_at5": int(target in retrieved) if target else None,
            "knowledge_target_present": target is not None,
            "supplement": supplement,
        },
    )
