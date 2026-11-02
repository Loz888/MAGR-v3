import os
import subprocess
import sys
from pathlib import Path

import pytest

from mgar_v3.coordination.engine import Engine
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.graph.runner import Runner


@pytest.mark.parametrize(
    "phase", ["evidence_read", "expert_completed", "expert_joined", "approval_requested"]
)
def test_sigkill_expert_handoffs_resume_same_run_without_duplicate_action(
    settings, trigger, phase, tmp_path
):
    settings.graph_version = "r4-experts-v1"
    settings.data_profile_path = tmp_path / "reader.json"
    environment = {
        **os.environ,
        "MGAR_V3_DATABASE_URL": settings.database_url.get_secret_value(),
        "MGAR_V3_ARTIFACT_ROOT": str(settings.artifact_root),
        "MGAR_V3_REPOSITORY": settings.repository,
        "MGAR_V3_GRAPH_VERSION": settings.graph_version,
        "MGAR_V3_LOCAL_ACTOR": "owner",
        "MGAR_V3_ALLOWED_ACTORS": '["owner"]',
        "MGAR_V3_DATA_PROFILE_PATH": str(settings.data_profile_path),
        "MGAR_V3_RAG_MODE": "fixture",
        "MGAR_TEST_EVENT": trigger.model_dump_json(),
    }
    process = subprocess.run(
        [sys.executable, str(Path(__file__).parent / "fixtures/r4_crash_worker.py"), phase],
        env=environment,
        capture_output=True,
        timeout=20,
    )
    assert process.returncode == -9, process.stderr.decode()
    recovered = Engine(settings, ArtifactStore(settings.artifact_root))
    try:
        recovered.recover()
        run = recovered.list_runs()[0]
        with recovered.transaction() as conn:
            old = conn.execute(
                "SELECT task_id,result FROM app.expert_results WHERE run_id=%s", (run["run_id"],)
            ).fetchall()
            assert not conn.execute("SELECT 1 FROM app.tool_grants").fetchone()
        if run["status"] == "paused":
            assert recovered.resume(run["run_id"]) == run["run_id"]
            Runner(recovered).execute(run["run_id"])
        assert recovered.get_run(run["run_id"])["status"] == "waiting_approval"
        assert len(recovered.actions(run["run_id"])) == 1
        with recovered.transaction() as conn:
            for row in old:
                assert (
                    conn.execute(
                        "SELECT result FROM app.expert_results WHERE run_id=%s AND task_id=%s",
                        (run["run_id"], row["task_id"]),
                    ).fetchone()["result"]
                    == row["result"]
                )
            assert conn.execute("SELECT count(*) AS n FROM app.expert_results").fetchone()["n"] == 3
            assert conn.execute("SELECT count(*) AS n FROM app.usage_calls").fetchone()["n"] == 0
    finally:
        recovered.close()
