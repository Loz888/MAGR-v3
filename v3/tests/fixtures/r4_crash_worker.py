import base64
import os
import signal
import sys
from contextlib import contextmanager

from mgar_v3.agents.data import provision
from mgar_v3.coordination.engine import Engine
from mgar_v3.domain import TriggerInput
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.graph.runner import Runner
from mgar_v3.rag.domain import Upload
from mgar_v3.settings import Settings

settings = Settings()
engine = Engine(settings, ArtifactStore(settings.artifact_root))
provision(engine, settings.data_profile_path)
job = engine.knowledge.upload(
    Upload(
        title="orders",
        source_uri="kb://crash/orders",
        source_version="v1",
        media_type="text/plain",
        content_base64=base64.b64encode(
            "业务订单按唯一订单号统计，重复支付事件要去重。".encode()
        ).decode(),
    )
)
engine.knowledge.index(job["job_id"])
receipt = engine.ingest(TriggerInput.model_validate_json(os.environ["MGAR_TEST_EVENT"]))
phase = sys.argv[1]
transaction = engine.transaction
event = engine._event
pending = False


def crash_event(conn, run_id, kind, **payload):
    global pending
    event(conn, run_id, kind, **payload)
    if kind == phase:
        pending = True


@contextmanager
def crash_transaction():
    with transaction() as conn:
        yield conn
    if pending and engine.connection.info.transaction_status.value == 0:
        os.kill(os.getpid(), signal.SIGKILL)


engine._event = crash_event
engine.transaction = crash_transaction
Runner(engine).execute(receipt.run_id)
raise RuntimeError("Expected SIGKILL point was not reached")
