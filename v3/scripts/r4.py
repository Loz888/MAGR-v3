"""Explicit R4 local setup and isolated real-model retrieval acceptance."""

import argparse
import base64
import json
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

from mgar_v3.agents.data import provision
from mgar_v3.coordination.engine import Engine
from mgar_v3.domain import digest
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.rag.domain import Search, Upload
from mgar_v3.rag.embedding import SpacyEmbedding
from mgar_v3.rag.rank import train
from mgar_v3.settings import ROOT, Settings
from mgar_v3.store.migrate import migrate


def seed(engine: Engine) -> None:
    for item in json.loads((ROOT / "config/r4-corpus.json").read_text()):
        path = ROOT / item["path"]
        if not path.resolve().is_relative_to((ROOT / "fixtures/knowledge").resolve()):
            raise ValueError("Corpus path is outside its explicit scope")
        body = Upload(
            title=item["title"],
            source_uri=item["source_uri"],
            source_version=item["source_version"],
            media_type="text/markdown",
            content_base64=base64.b64encode(path.read_bytes()).decode(),
        )
        job = engine.knowledge.upload(body)
        engine.knowledge.index(job["job_id"])


def evaluate(settings: Settings, output: Path) -> dict:
    name = "mgar_r4_eval_" + uuid4().hex
    base = settings.database_url.get_secret_value()
    with psycopg.connect(base, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    local = settings.model_copy(
        update={
            "database_url": make_conninfo(base, dbname=name),
            "rag_mode": "local",
            "artifact_root": ROOT / ".local/r4-eval" / name,
        }
    )
    # SecretStr must stay a secret selector, including on a copied configuration.
    from pydantic import SecretStr

    local.database_url = SecretStr(make_conninfo(base, dbname=name))
    try:
        migrate(local.database_url.get_secret_value())
        engine = Engine(local, ArtifactStore(local.artifact_root))
        try:
            seed(engine)
            frozen = json.loads((ROOT / "config/r4-frozen-queries.json").read_text())
            training = json.loads((ROOT / "config/r4-rank-training.json").read_text())
            assert not {q["query"] for q in frozen["queries"]} & {t["query"] for t in training}
            cases = []
            _, indexed, _ = engine.knowledge.eligible(
                engine.knowledge.active(), engine.knowledge.actor()
            )
            sources = {str(row["chunk_id"]): row["source_uri"] for row in indexed}
            for query in frozen["queries"]:
                result = engine.knowledge.search(Search(query=query["query"], top_k=8))
                uris = [hit["source_uri"] for hit in result["hits"]]
                rank = (
                    uris.index(query["expected_source_uri"]) + 1
                    if query["expected_source_uri"] in uris
                    else None
                )
                raw_ranks = {}
                for method in ("vector", "bm25"):
                    ranked = [sources[item[0]] for item in result[method + "_ranking"]]
                    expected = query["expected_source_uri"]
                    raw_ranks[method + "_rank"] = (
                        ranked.index(expected) + 1 if expected in ranked else None
                    )
                cases.append(
                    {
                        "id": query["id"],
                        "rank": rank,
                        "hits": uris,
                        "trace_id": result["trace_id"],
                        **raw_ranks,
                    }
                )
            metrics = {
                f"recall_at_{k}": sum(c["rank"] is not None and c["rank"] <= k for c in cases)
                / len(cases)
                for k in (1, 5, 8)
            }
            config = engine.knowledge.config()
            result = {
                "schema": "r4-real-retrieval-v1",
                "gate": "PASS" if metrics["recall_at_5"] >= 0.9 else "FAIL",
                "cases": cases,
                "metrics": metrics,
                "config": config,
                "corpus_hash": digest(json.loads((ROOT / "config/r4-corpus.json").read_text())),
                "frozen_queries_hash": digest(frozen),
                "training_hash": digest(training),
                "paid_api_calls": 0,
                "scope": (
                    "12 synthetic owned Chinese documents; frozen paraphrases; local CPU "
                    "models; real PostgreSQL/pgvector"
                ),
            }
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
            return result
        finally:
            engine.close()
    finally:
        assert name.startswith("mgar_r4_eval_")
        with psycopg.connect(base, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["train", "seed", "data", "eval"])
    parser.add_argument("--output", type=Path, default=ROOT / ".local/r4-eval/result.json")
    args = parser.parse_args()
    if args.action == "train":
        pairs = json.loads((ROOT / "config/r4-rank-training.json").read_text())
        value = train(pairs, SpacyEmbedding(), ROOT / ".local/models/reranker-linear.json")
        print(json.dumps({k: v for k, v in value.items() if k != "weights"}))
        return
    settings = Settings()
    if args.action == "eval":
        result = evaluate(settings, args.output)
        print(
            json.dumps(
                {"gate": result["gate"], "metrics": result["metrics"], "output": str(args.output)}
            )
        )
        raise SystemExit(0 if result["gate"] == "PASS" else 1)
    engine = Engine(settings, ArtifactStore(settings.artifact_root))
    try:
        if args.action == "seed":
            seed(engine)
            print(
                json.dumps(
                    {"snapshot_id": str(engine.knowledge.active()), "mode": settings.rag_mode}
                )
            )
        else:
            print(json.dumps(provision(engine, settings.data_profile_path)))
    finally:
        engine.close()


if __name__ == "__main__":
    main()
