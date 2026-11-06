"""Kill the actual Engine between result preparation and the joint report commit."""

import os
import signal
import sys
from pathlib import Path
from uuid import UUID, uuid4

from mgar_v3.commerce.domain import CaseInput
from mgar_v3.commerce.setup import setup
from mgar_v3.coordination.engine import Engine
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.graph.runner import Runner
from mgar_v3.settings import Settings

settings = Settings()
settings.commerce_directory = Path(sys.argv[1])
engine = Engine(settings, ArtifactStore(settings.artifact_root))
setup(engine)
template = engine.commerce.template("duplicate_paid")
receipt = engine.commerce.create(
    CaseInput(
        template_id="duplicate_paid",
        issue_number=9501,
        window=template["manifest"]["policy"]["window"],
        goal="report",
        client_request_id=str(uuid4()),
    )
)


def crash(*args, **kwargs):
    os.kill(os.getpid(), signal.SIGKILL)


engine.commit_prepared_report = crash
Runner(engine).execute(UUID(receipt["run_id"]))
raise RuntimeError("The expected crash point was not reached")
