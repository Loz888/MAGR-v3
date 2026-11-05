"""Version 3 evidence acquisition using public scope and actual reader results."""

import hashlib
import json
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid4

from mgar_v3.agents.data import DataReader
from mgar_v3.agents.numeric import revenue_metrics
from mgar_v3.benchmark.domain import Case, Source
from mgar_v3.coordination.engine import Engine
from mgar_v3.domain import DomainError, Evidence, digest
from mgar_v3.rag.domain import Search


def collect_scoped(
    engine: Engine,
    run_id: UUID,
    epoch: int,
    case: Case,
    inputs: dict[str, Any],
    *,
    supplement: bool = False,
    ablation: str = "none",
) -> tuple[list[Source], list[str], dict[str, Any]]:
    sources: list[Source] = []
    errors = []
    calls = 0
    retrieved = []
    project = inputs["projects"][engine.settings.project_id]
    if digest(engine.knowledge.config()) != project["rag_config_hash"]:
        raise DomainError("BENCHMARK_INPUT_CHANGED", "Retrieval model or ranker changed")

    def record(
        role: Literal["knowledge", "repo", "data"],
        text: str,
        version: str,
        uri: str,
        *,
        document_id: str | None = None,
        revision: str | None = None,
        numeric_facts: dict[str, str] | None = None,
    ) -> None:
        if len(text.encode()) > 8192:
            raise DomainError("EVIDENCE_TOO_LARGE", "Do not compute or cite truncated evidence")
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
        sources.append(
            Source(
                id=str(eid),
                role=role,
                text=text,
                version=version,
                document_id=document_id,
                revision=revision,
                numeric_facts=numeric_facts or {},
            )
        )

    target = project["documents"].get(case.document_id or "")
    if "knowledge" in case.required_roles:
        try:
            # This is scoped authorization metadata, not document contents or an oracle.
            calls += 1
            documents = engine.knowledge.documents()
            document = next((d for d in documents if str(d["document_id"]) == target), None)
            if document is None:
                raise DomainError("KNOWLEDGE_DOCUMENT_NOT_FOUND", "Requested document not found")
            if document["status"] != "active":
                raise DomainError("KNOWLEDGE_REVOKED", "Requested document was revoked", 403)
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
            # A named-document task cannot inherit an unrelated contract from another hit.
            for hit in [h for h in result["hits"] if str(h["document_id"]) == target][:2]:
                calls += 1
                value = engine.knowledge.read(UUID(hit["chunk_id"]), UUID(project["snapshot_id"]))
                record(
                    "knowledge",
                    value["text"],
                    str(value["version_id"]),
                    "kb://final/" + str(hit["chunk_id"]),
                    document_id=case.document_id,
                    revision=value["source_version"],
                )
        except DomainError as exc:
            if exc.code in {
                "BENCHMARK_INPUT_CHANGED",
                "KNOWLEDGE_SOURCE_MISMATCH",
                "ABLATION_SCOPE_DENIED",
            }:
                raise
            errors.append("knowledge:" + exc.code)
    if "repo" in case.required_roles:
        source = inputs["sources"][case.source_path]
        if hashlib.sha256(source["content"].encode()).hexdigest() != source["sha256"]:
            raise DomainError("BENCHMARK_INPUT_CHANGED", "Frozen GitHub bytes changed")
        calls += 1
        record("repo", source["content"], source["blob"], "github://" + case.source_path)
    if "data" in case.required_roles:
        try:
            config = inputs["readers"].get(case.id)
            if config is None:
                raise DomainError("DATA_SOURCE_NOT_CONFIGURED", "No SQL reader profile")
            reader = DataReader(json.loads(Path(config["path"]).read_text()))
            if reader.profile["fixture_hash"] != config["fixture_hash"]:
                raise DomainError("BENCHMARK_INPUT_CHANGED", "SQL fixture changed")
            table = reader.profile["schema"] + ".orders"
            calls += 1
            raw = reader.query(
                "SELECT event_id,order_id,status,amount FROM " + table + " ORDER BY event_id", []
            )
            rows = [
                dict(zip([c["name"] for c in raw["columns"]], r, strict=True)) for r in raw["rows"]
            ]
            if digest(rows) != config["fixture_hash"]:
                raise DomainError("BENCHMARK_INPUT_CHANGED", "Observed SQL rows changed")
            facts = revenue_metrics(rows)
            calls += 1
            aggregate = reader.query(
                "SELECT count(*) AS paid_events,count(DISTINCT order_id) AS distinct_orders,"
                "sum(amount) AS event_amount FROM " + table + " WHERE status='paid'",
                [],
            )
            observed = dict(
                zip([c["name"] for c in aggregate["columns"]], aggregate["rows"][0], strict=True)
            )
            if any(
                Decimal(str(observed[k] if observed[k] is not None else 0)) != Decimal(facts[k])
                for k in observed
            ):
                raise DomainError("NUMERIC_EVIDENCE_CONFLICT", "SQL aggregate disagrees with rows")
            calls += 1
            first = reader.query(
                "SELECT order_id,min(event_id) AS first_paid_event FROM "
                + table
                + " WHERE status='paid' GROUP BY order_id ORDER BY order_id",
                [],
            )
            selected = {r[0]: r[1] for r in first["rows"]}
            earliest: dict[str, int] = {}
            for row in rows:
                if row["status"] == "paid":
                    earliest[row["order_id"]] = min(
                        earliest.get(row["order_id"], row["event_id"]), row["event_id"]
                    )
            if selected != earliest:
                raise DomainError("NUMERIC_EVIDENCE_CONFLICT", "SQL first-event IDs disagree")
            text = json.dumps(
                {
                    "exact_decimal_metrics": facts,
                    "raw_rows": rows,
                    "selection": (
                        "earliest paid event_id per order; include zero and negative amounts"
                    ),
                    "first_paid_events": selected,
                    "sql_queries": [raw["statement"], aggregate["statement"], first["statement"]],
                    "sql_result_hashes": [
                        raw["result_hash"],
                        aggregate["result_hash"],
                        first["result_hash"],
                    ],
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
            record("data", text, config["fixture_hash"], "sql://" + table, numeric_facts=facts)
        except DomainError as exc:
            if exc.code == "BENCHMARK_INPUT_CHANGED":
                raise
            errors.append("data:" + exc.code)
    return (
        sources,
        errors,
        {
            "tool_calls": calls,
            "recall_at5": int(target in retrieved) if target else None,
            "knowledge_target_present": target is not None,
            "supplement": supplement,
            "evidence_protocol": "r6-scoped-v3",
        },
    )
