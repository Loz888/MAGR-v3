import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from mgar_v3.coordination.engine import Engine
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.graph.runner import Runner
from mgar_v3.repair.graph import execute_repair
from mgar_v3.repair.live import DeepSeekPatcher, repair_permit


@pytest.mark.parametrize("phase", ["valid-first", "invalid-first"])
def test_sigkill_costed_model_result_is_reused_even_after_a_known_schema_failure(
    settings, tmp_path, phase
):
    environment = {
        **os.environ,
        "MGAR_V3_DATABASE_URL": settings.database_url.get_secret_value(),
        "MGAR_V3_ARTIFACT_ROOT": str(settings.artifact_root),
        "MGAR_V3_REPOSITORY": settings.repository,
        "MGAR_V3_LOCAL_ACTOR": "owner",
        "MGAR_V3_ALLOWED_ACTORS": '["owner"]',
    }
    child = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).parent / "fixtures/r5_model_crash_worker.py"),
            str(tmp_path),
            phase,
        ],
        env=environment,
        capture_output=True,
        timeout=90,
    )
    assert child.returncode == -9, child.stderr.decode()
    settings.allow_paid = True
    settings.deepseek_api_key = SecretStr("fixture-key")
    settings.repair_profile_path = tmp_path / "profile.json"
    settings.repair_authorization_path = tmp_path / "authorization.json"
    settings.repair_snapshot_path = tmp_path / "source.json"
    engine = Engine(settings, ArtifactStore(settings.artifact_root))
    try:
        engine.recover()
        run = engine.list_runs()[0]
        run_id = run["run_id"]
        engine.resume(run_id)
        expected = 2 if phase == "invalid-first" else 1
        assert int((tmp_path / "physical-calls.txt").read_text()) == expected

        def forbidden(_):
            raise AssertionError("A known model result must not be physically requested again")

        client = httpx.Client(transport=httpx.MockTransport(forbidden))
        epoch = engine.get_run(run_id)["execution_epoch"] + 1
        action = execute_repair(
            Runner(engine),
            run_id,
            DeepSeekPatcher(engine, run_id, epoch, repair_permit(settings), client),
        )
        assert action and engine.actions(run_id)[0]["status"] == "proposed"
        with engine.transaction() as conn:
            rows = conn.execute("SELECT status FROM app.usage_calls").fetchall()
        assert len(rows) == expected and all(r["status"] == "known" for r in rows)
        assert len(engine.actions(run_id)) == 1
    finally:
        engine.close()
