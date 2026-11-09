import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mgar_v3.quality_eval import manifest, record, validate  # noqa: E402

DATASET_HASH = "c" * 64
CONFIG_HASH = "d" * 64


def make(arm="baseline", case_id="case-1", **overrides):
    mapping = {
        "experiment_id": "exp-001",
        "baseline_commit": "1" * 40,
        "candidate_commit": "2" * 40,
        "dataset_hash": DATASET_HASH,
        "config_hash": CONFIG_HASH,
        "case_id": case_id,
        "arm": arm,
        "mode": "offline_replay",
        "status": "PASS",
        "failure_kind": None,
        "latency_ms": 10.0,
        "model_calls": 1,
        "usage_source": "actual_provider_usage",
        "evidence_refs": [],
    }
    mapping.update(overrides)
    return mapping


def test_duplicate_keys_are_rejected():
    records = [make(case_id="case-1"), make(case_id="case-1")]
    problems = validate.validate_records(records)
    assert any("duplicate key" in item for item in problems)
    with pytest.raises(validate.ValidationError):
        validate.require_valid(records)


def test_repeat_index_makes_duplicates_legal():
    records = [make(repeat_index=0), make(repeat_index=1)]
    assert validate.validate_records(records) == []


def test_case_missing_from_candidate_is_rejected():
    records = [make(), make(case_id="case-2"), make(arm="candidate", case_id="case-1")]
    with pytest.raises(validate.ValidationError) as caught:
        validate.require_comparable(records, "baseline", "candidate")
    assert any("case sets differ" in item for item in caught.value.problems)


def test_missing_arm_is_rejected():
    records = [make()]
    with pytest.raises(validate.ValidationError) as caught:
        validate.require_comparable(records, "baseline", "candidate")
    assert any("arm candidate has no records" in item for item in caught.value.problems)


def test_dataset_hash_mismatch_is_rejected():
    records = [
        make(),
        make(arm="candidate", dataset_hash="e" * 64),
    ]
    with pytest.raises(validate.ValidationError) as caught:
        validate.require_comparable(records, "baseline", "candidate")
    assert any("records disagree on dataset_hash" in item for item in caught.value.problems)


def test_config_hash_mismatch_is_rejected():
    records = [
        make(),
        make(arm="candidate", config_hash="f" * 64),
    ]
    with pytest.raises(validate.ValidationError) as caught:
        validate.require_comparable(records, "baseline", "candidate")
    assert any("records disagree on config_hash" in item for item in caught.value.problems)


def test_paired_keys_must_match_exactly():
    records = [
        make(),
        make(arm="candidate", repeat_index=1),
    ]
    with pytest.raises(validate.ValidationError) as caught:
        validate.require_comparable(records, "baseline", "candidate")
    assert any("paired keys differ" in item for item in caught.value.problems)


def test_load_jsonl_rejects_non_object_and_invalid_json(tmp_path):
    path = tmp_path / "bad.jsonl"
    path.write_text('{"a": 1}\n\n[1, 2]\n', encoding="utf-8")
    with pytest.raises(validate.ValidationError):
        validate.load_jsonl(path)
    path.write_text("not json\n", encoding="utf-8")
    with pytest.raises(validate.ValidationError):
        validate.load_jsonl(path)


def build_project(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "config").mkdir()
    dataset = tmp_path / "data" / "cases.json"
    config_file = tmp_path / "config" / "config.json"
    dataset.write_text(json.dumps([{"id": "case-1"}]), encoding="utf-8")
    config_file.write_text('{"k": 1}', encoding="utf-8")
    manifest_obj = manifest.build_manifest(
        experiment_id="exp-001",
        baseline_commit="1" * 40,
        candidate_commit="2" * 40,
        dataset_file="data/cases.json",
        config_file="config/config.json",
        case_ids=["case-1"],
        arms=["baseline", "candidate"],
        root=tmp_path,
        mode="offline_replay",
        created_at="2026-11-09T00:00:00+00:00",
    )
    return manifest_obj, dataset, config_file


def test_manifest_self_hash_and_file_checks(tmp_path):
    manifest_obj, dataset, _ = build_project(tmp_path)
    result = manifest.verify_manifest(manifest_obj, root=tmp_path, check_files=True)
    assert result["errors"] == []
    assert result["file_checks"] == {"dataset": "MATCH", "config": "MATCH"}

    tampered = dict(manifest_obj)
    tampered["experiment_id"] = "other"
    assert manifest.verify_manifest(tampered)["errors"]

    dataset.write_text(json.dumps([{"id": "case-2"}]), encoding="utf-8")
    result = manifest.verify_manifest(manifest_obj, root=tmp_path, check_files=True)
    assert result["file_checks"]["dataset"] == "MISMATCH"
    assert any("dataset file hash mismatch" in item for item in result["errors"])


def test_manifest_consistency_with_records(tmp_path):
    manifest_obj, dataset, _ = build_project(tmp_path)
    dataset_hash = record.sha256_file(dataset)
    config_hash = record.sha256_file(tmp_path / "config" / "config.json")
    records = [
        make(dataset_hash=dataset_hash, config_hash=config_hash),
        make(arm="candidate", dataset_hash=dataset_hash, config_hash=config_hash),
    ]
    assert validate.check_manifest_consistency(manifest_obj, records) == []

    records[1]["case_id"] = "case-2"
    problems = validate.check_manifest_consistency(manifest_obj, records)
    assert any("record case_ids differ from manifest" in item for item in problems)


def test_manifest_requires_declared_arms():
    manifest_obj = {
        "schema": "wrong",
        "experiment_id": "exp-001",
        "baseline_commit": "1",
        "candidate_commit": "2",
        "dataset": {},
        "config": {},
        "arms": [],
        "pairing_key": ["case_id"],
        "authorization": {},
        "environment": {},
        "manifest_sha256": "0" * 64,
    }
    errors = manifest.verify_manifest(manifest_obj)["errors"]
    assert any("schema" in item for item in errors)
    assert any("pairing_key" in item for item in errors)
    assert any("arms" in item for item in errors)
    assert any("manifest_sha256" in item for item in errors)
