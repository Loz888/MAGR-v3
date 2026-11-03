import json
from uuid import uuid4

import pytest

from mgar_v3.benchmark.run import resume_state, retire_interrupted
from mgar_v3.domain import DomainError, digest


def test_completed_results_are_sealed_and_unknown_never_resumes(tmp_path):
    rows = [{"case_id": "c1", "arm": "A", "category": "qa", "status": "PASS"}]
    manifest = {"assignments": rows}
    state = {
        "manifest_hash": digest(manifest),
        "runs": {},
        "sealed_results": {"c1:A": digest(rows[0])},
    }
    (tmp_path / "results.json").write_text(json.dumps(rows))
    (tmp_path / "execution.json").write_text(json.dumps(state))
    assert resume_state(tmp_path, manifest)[0] == rows
    for status in ["FAIL", "UNKNOWN"]:
        rows[0]["status"] = status
        (tmp_path / "results.json").write_text(json.dumps(rows))
        with pytest.raises(DomainError):
            resume_state(tmp_path, manifest)


def test_interrupted_assignment_is_failed_without_rerun_or_paid_call(engine, trigger):
    receipt = engine.ingest(trigger)
    engine.begin_run(receipt.run_id)
    record = {"case_id": "c1", "arm": "C", "status": "NOT_RUN"}
    retire_interrupted(engine, record, receipt.run_id)
    assert record["status"] == "FAIL" and record["reason"] == "PROCESS_INTERRUPTED"
    assert record["model_calls"] == 0 and record["latency_unknown"]
    assert engine.get_run(receipt.run_id)["status"] == "cancelled"
    with engine.transaction() as conn:
        assert not conn.execute("SELECT 1 FROM app.usage_calls").fetchone()
        assert not conn.execute("SELECT 1 FROM app.outbox").fetchone()


def test_unknown_interrupted_call_retains_reservation_and_blocks_continuation(engine, trigger):
    receipt = engine.ingest(trigger)
    engine.begin_run(receipt.run_id)
    with engine.transaction() as conn:
        conn.execute(
            "INSERT INTO app.usage_calls(call_id,run_id,step_key,status,reserved_micros) "
            "VALUES (%s,%s,'pending','unknown',100)",
            (uuid4(), receipt.run_id),
        )
    with pytest.raises(DomainError) as error:
        retire_interrupted(engine, {}, receipt.run_id)
    assert error.value.code == "USAGE_UNKNOWN"
    with engine.transaction() as conn:
        assert conn.execute("SELECT status,reserved_micros FROM app.usage_calls").fetchone() == {
            "status": "unknown",
            "reserved_micros": 100,
        }


def test_continuation_cannot_change_model_strategy_even_with_a_new_receipt(tmp_path, monkeypatch):
    import hashlib
    import zipfile

    from mgar_v3.benchmark import run

    old = {
        "src/mgar_v3/benchmark/run.py": b"original supervisor",
        "src/mgar_v3/benchmark/executor.py": b"frozen model strategy",
    }
    h = hashlib.sha256()
    for name, data in sorted(old.items()):
        h.update(name.encode() + b"\0" + data + b"\0")
    manifest = {"implementation_sha256": h.hexdigest()}
    archive = tmp_path / "baseline-source.zip"
    with zipfile.ZipFile(archive, "w") as z:
        for name, data in old.items():
            z.writestr(name, data)
    for name, data in old.items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    (tmp_path / "src/mgar_v3/benchmark/run.py").write_bytes(b"new isolated supervisor")
    (tmp_path / "results.json").write_text("[]")
    monkeypatch.setattr(run, "ROOT", tmp_path)
    monkeypatch.setattr(run, "fingerprint", lambda: "current")
    receipt = {
        "manifest_hash": digest(manifest),
        "new_implementation_sha256": "current",
        "baseline_archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        "scope": "supervision_only_no_reruns_no_new_grant",
        "changed_paths": ["src/mgar_v3/benchmark/run.py"],
    }
    p = tmp_path / "continuation.json"
    p.write_text(json.dumps(receipt))
    run.verify_continuation(tmp_path, manifest, p)
    (tmp_path / "src/mgar_v3/benchmark/executor.py").write_bytes(b"changed model algorithm")
    receipt["changed_paths"].append("src/mgar_v3/benchmark/executor.py")
    receipt["changed_paths"].sort()
    p.write_text(json.dumps(receipt))
    with pytest.raises(DomainError):
        run.verify_continuation(tmp_path, manifest, p)


def test_io_setup_change_preserves_actual_frozen_model_request_and_rejects_protocol_edits():
    import zipfile

    from mgar_v3.benchmark.run import same_model_protocol
    from mgar_v3.settings import ROOT

    name = "src/mgar_v3/models/structured.py"
    with zipfile.ZipFile(ROOT / "eval/reports/final-v2-baseline-source.zip") as archive:
        original = archive.read(name)
    current = (ROOT / name).read_bytes()
    assert same_model_protocol(original, current)
    for before, after in [
        (b'"max_tokens": p.max_output_tokens', b'"max_tokens": 8192'),
        (b"https://api.deepseek.com/v1/chat/completions", b"https://other.invalid/send"),
        (b'usage["prompt_tokens"] * p.input_micros_per_million', b'usage["prompt_tokens"] * 0'),
    ]:
        assert before in current
        assert not same_model_protocol(original, current.replace(before, after))
