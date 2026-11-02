"""Record a fixture Run before dependency restart; verify it afterwards without external writes."""

import argparse
import json
from uuid import UUID, uuid4

import psycopg
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg.rows import dict_row

from mgar_v3.coordination.engine import Engine
from mgar_v3.domain import TriggerInput
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.graph.runner import Runner
from mgar_v3.settings import ROOT, Settings

parser = argparse.ArgumentParser()
parser.add_argument("phase", choices=["write", "read"])
args = parser.parse_args()
settings = Settings()
path = ROOT / ".local/restart-probe.json"
engine = Engine(settings, ArtifactStore(settings.artifact_root))
try:
    if args.phase == "write":
        trigger = TriggerInput(
            source="cli",
            external_event_id="restart-probe:" + uuid4().hex,
            project_id=settings.project_id,
            repository=settings.repository,
            issue_number=999999,
            actor=settings.local_actor,
            body="/mgar investigate Documented fixture observations",
        )
        receipt = engine.ingest(trigger)
        Runner(engine).execute(receipt.run_id)
        run = engine.get_run(receipt.run_id)
        record = {
            "trigger": trigger.model_dump(mode="json"),
            "run_id": str(receipt.run_id),
            "report_hash": run["report_hash"],
            "status": run["status"],
        }
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(record, indent=2) + "\n")
        print(json.dumps({"phase": "write", "run_id": record["run_id"], "status": run["status"]}))
    else:
        record = json.loads(path.read_text())
        run_id = UUID(record["run_id"])
        assert engine.ingest(TriggerInput.model_validate(record["trigger"])).duplicate
        run = engine.get_run(run_id)
        assert run["status"] == record["status"] == "waiting_approval"
        assert run["report_hash"] == record["report_hash"]
        assert engine.artifact_bytes(run["report_hash"])
        with psycopg.connect(
            settings.database_url.get_secret_value(),
            autocommit=True,
            row_factory=dict_row,
            options="-c search_path=checkpoints,public",
        ) as conn:
            assert PostgresSaver(conn).get_tuple({"configurable": {"thread_id": str(run_id)}})
        print(
            json.dumps(
                {
                    "phase": "read",
                    "run_id": str(run_id),
                    "status": "PASS",
                    "events_mailbox_report_checkpoint": "persisted",
                }
            )
        )
finally:
    engine.close()
