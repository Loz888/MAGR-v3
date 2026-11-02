"""Local CPU RAG + actual HTTP MCP + readonly SQL, explicitly fixture experts/repo."""

import hashlib
import json
import socket
import threading
import time
from datetime import UTC, datetime
from uuid import uuid4

import psycopg
import uvicorn
from psycopg import sql
from psycopg.conninfo import make_conninfo
from pydantic import SecretStr
from r4 import seed

from mgar_v3.agents.data import provision
from mgar_v3.api import create_app
from mgar_v3.coordination.engine import Engine
from mgar_v3.domain import TriggerInput, digest
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.graph.runner import Runner
from mgar_v3.settings import ROOT, Settings
from mgar_v3.store.migrate import migrate


def main() -> None:
    base = Settings()
    name = "mgar_r4_local_" + uuid4().hex
    directory = ROOT / ".local/r4-local" / name
    directory.mkdir(parents=True)
    dsn = make_conninfo(base.database_url.get_secret_value(), dbname=name)
    with psycopg.connect(base.database_url.get_secret_value(), autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    config = base.model_copy(
        update={
            "database_url": SecretStr(dsn),
            "artifact_root": directory / "artifacts",
            "data_profile_path": directory / "reader.json",
            "graph_version": "r4-experts-v1",
            "rag_mode": "local",
            "allow_paid": False,
            "allow_external_write": False,
            "live_profile_path": None,
            "live_authorization_path": None,
            "router_mode": "disabled",
        }
    )
    reader_role = None
    engine = None
    server = None
    thread = None
    sock = None
    try:
        migrate(dsn)
        engine = Engine(config, ArtifactStore(config.artifact_root))
        seed(engine)
        reader_role = provision(engine, config.data_profile_path)["reader_role"]
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        sock.listen(16)
        config.r4_tool_base_url = "http://127.0.0.1:" + str(sock.getsockname()[1])
        server = uvicorn.Server(
            uvicorn.Config(create_app(config, engine, run_background=False), log_level="error")
        )
        thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        thread.start()
        deadline = time.monotonic() + 5
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        if not server.started:
            raise RuntimeError("Local MCP authority failed to start")
        started = time.monotonic()
        receipt = engine.ingest(
            TriggerInput(
                source="cli",
                external_event_id="r4-local-combination",
                project_id=config.project_id,
                repository=config.repository,
                issue_number=1,
                actor=config.local_actor,
                body=(
                    "/mgar investigate 核对支付事件与去重订单的统计口径，"
                    "结合知识资料、只读 SQL 和代码核查。"
                ),
            )
        )
        assert receipt.run_id
        action_id = Runner(engine).execute(receipt.run_id)
        resolution = engine.tools.resolution(receipt.run_id)
        assert resolution and resolution["verdict"]["passed"] and resolution["payload"]["complete"]
        run = engine.get_run(receipt.run_id)
        with engine.transaction() as conn:
            reads = conn.execute(
                (
                    "SELECT role,count(*) AS n FROM app.r4_reads r JOIN app.evidence "
                    "e USING(evidence_id) WHERE run_id=%s GROUP BY role"
                ),
                (receipt.run_id,),
            ).fetchall()
            usage = conn.execute("SELECT count(*) AS n FROM app.usage_calls").fetchone()
        assert usage and usage["n"] == 0 and action_id and run["status"] == "waiting_approval"
        assert len(engine.actions(receipt.run_id)) == 1
        cells = [f["cell"]["value"] for f in resolution["payload"]["verified_facts"] if f["cell"]]
        assert cells == [3, 2, "32.00"]
        with engine.transaction() as conn:
            audit = {
                "evidence": conn.execute(
                    "SELECT * FROM app.evidence WHERE run_id=%s", (receipt.run_id,)
                ).fetchall(),
                "reads": conn.execute(
                    (
                        "SELECT r.* FROM app.r4_reads r JOIN app.evidence e "
                        "USING(evidence_id) WHERE run_id=%s"
                    ),
                    (receipt.run_id,),
                ).fetchall(),
                "expert_results": conn.execute(
                    "SELECT * FROM app.expert_results WHERE run_id=%s", (receipt.run_id,)
                ).fetchall(),
                "search_traces": conn.execute(
                    "SELECT * FROM knowledge.search_traces WHERE run_id=%s", (receipt.run_id,)
                ).fetchall(),
                "events": engine.events(receipt.run_id),
                "resolution": resolution,
                "knowledge_config": engine.knowledge.config(),
            }
        (directory / "execution.json").write_text(
            json.dumps(audit, ensure_ascii=False, indent=2, default=str) + "\n"
        )
        report = engine.artifact_bytes(run["report_hash"])
        (directory / "report.md").write_bytes(report)
        result = {
            "schema": "r4-local-combined-v1",
            "status": "PASS",
            "observed_at": datetime.now(UTC).isoformat(),
            "run_id": str(receipt.run_id),
            "action_count": 1,
            "action_status": "proposed_not_approved",
            "report_sha256": hashlib.sha256(report).hexdigest(),
            "resolution_sha256": digest(resolution["payload"]),
            "roles": [r["role"] for r in reads],
            "actual_reads": sum(r["n"] for r in reads),
            "sql_values": cells,
            "mechanical_verification": "PASS",
            "duration_seconds": round(time.monotonic() - started, 3),
            "components": {
                "embedding": "real_local_pretrained_zh_core_web_md_3.8.0",
                "retrieval": "real_pgvector_chinese_BM25_RRF",
                "reranker": "real_local_trained_pairwise_linear",
                "mcp": "actual_HTTP_SDK_knowledge_and_data",
                "sql": "actual_independent_readonly_PostgreSQL_role",
                "experts": "fixture_deterministic",
                "repository": "owned_synthetic_fixture",
                "langgraph": "official_PostgresSaver_parallel_Send",
            },
            "new_paid_calls": 0,
            "new_github_comments": 0,
            "full_paid_business_live": "NOT_RUN",
        }
        (directory / "result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n"
        )
        print(
            json.dumps(
                {"status": "PASS", "result": str(directory / "result.json"), "new_paid_calls": 0}
            )
        )
    finally:
        if server:
            server.should_exit = True
        if thread:
            thread.join(timeout=10)
        if sock:
            sock.close()
        if engine:
            engine.close()
        role = reader_role or "r4_reader_" + hashlib.sha256(name.encode()).hexdigest()[:12]
        with psycopg.connect(dsn, autocommit=True) as conn:
            if conn.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (role,)).fetchone():
                conn.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
                conn.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))
        assert name.startswith("mgar_r4_local_")
        with psycopg.connect(base.database_url.get_secret_value(), autocommit=True) as conn:
            conn.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


if __name__ == "__main__":
    main()
