import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mgar_v3.quality_eval import record  # noqa: E402

HASH_A = "a" * 64
HASH_B = "b" * 64


def base_mapping(**overrides):
    mapping = {
        "experiment_id": "exp-001",
        "baseline_commit": "1" * 40,
        "candidate_commit": "2" * 40,
        "dataset_hash": HASH_A,
        "config_hash": HASH_B,
        "case_id": "case-1",
        "arm": "baseline",
        "mode": "offline_replay",
        "status": "PASS",
        "failure_kind": None,
        "latency_ms": 12.5,
        "model_calls": 1,
        "usage_source": "actual_provider_usage",
        "evidence_refs": ["eval/reports/example.jsonl#L1"],
    }
    mapping.update(overrides)
    return mapping


def test_canonical_digest_matches_domain_serialization():
    expected = hashlib.sha256('{"a":1,"b":2}'.encode()).hexdigest()
    assert record.canonical_digest({"b": 2, "a": 1}) == expected
    assert record.canonical_json({"b": 2, "a": 1}) == '{"a":1,"b":2}'


def test_valid_pass_record_round_trips():
    mapping = base_mapping()
    assert record.validate_mapping(mapping) == []
    parsed = record.CaseRecord.from_mapping(mapping)
    dumped = parsed.to_dict()
    assert dumped["evidence_refs"] == ["eval/reports/example.jsonl#L1"]
    assert record.validate_mapping(dumped) == []
    assert parsed.key() == ("exp-001", "case-1", "baseline", 0)


def test_pass_record_rejects_failure_kind():
    problems = record.validate_mapping(base_mapping(failure_kind="business"))
    assert any("PASS records must have failure_kind=null" in item for item in problems)


def test_fail_record_requires_failure_kind():
    problems = record.validate_mapping(base_mapping(status="FAIL", failure_kind=None))
    assert any("FAIL records require failure_kind" in item for item in problems)
    assert record.validate_mapping(base_mapping(status="FAIL", failure_kind="business")) == []
    assert (
        record.validate_mapping(base_mapping(status="FAIL", failure_kind="infrastructure")) == []
    )
    assert (
        record.validate_mapping(base_mapping(status="FAIL", failure_kind="validation_rejected"))
        == []
    )


def test_unknown_requires_unknown_failure_kind():
    problems = record.validate_mapping(base_mapping(status="UNKNOWN", failure_kind=None))
    assert any("UNKNOWN records require failure_kind=unknown" in item for item in problems)
    assert record.validate_mapping(base_mapping(status="UNKNOWN", failure_kind="unknown")) == []


def test_not_run_requires_reason_and_no_failure_kind():
    problems = record.validate_mapping(
        base_mapping(status="NOT_RUN", failure_kind=None, not_run_reason=None, model_calls=0,
                     usage_source="not_applicable")
    )
    assert any("NOT_RUN records require not_run_reason" in item for item in problems)
    assert (
        record.validate_mapping(
            base_mapping(
                status="NOT_RUN",
                failure_kind=None,
                not_run_reason="not authorized",
                model_calls=0,
                usage_source="not_applicable",
            )
        )
        == []
    )


def test_not_applicable_usage_requires_zero_calls():
    problems = record.validate_mapping(base_mapping(usage_source="not_applicable", model_calls=3))
    assert any("usage_source=not_applicable requires model_calls=0" in item for item in problems)


def test_unknown_fields_and_missing_fields_are_rejected():
    problems = record.validate_mapping(base_mapping(extra_field=1))
    assert any("unknown fields" in item for item in problems)
    incomplete = base_mapping()
    incomplete.pop("dataset_hash")
    problems = record.validate_mapping(incomplete)
    assert any("missing required fields" in item for item in problems)


def test_hash_and_enum_formats_are_enforced():
    assert any("dataset_hash" in item for item in record.validate_mapping(base_mapping(dataset_hash="xyz")))
    assert record.validate_mapping(base_mapping(dataset_hash=HASH_A.upper())) != []
    assert any("mode" in item for item in record.validate_mapping(base_mapping(mode="live")))
    assert any(
        "cache_state" in item for item in record.validate_mapping(base_mapping(cache_state="hot"))
    )
    assert any(
        "latency_ms" in item for item in record.validate_mapping(base_mapping(latency_ms=-1))
    )


def test_serialize_record_is_deterministic():
    mapping = base_mapping()
    first = record.serialize_record(mapping)
    second = record.serialize_record(dict(reversed(list(mapping.items()))))
    assert first == second
    assert "\ufeff" not in first
    assert json.loads(first)["case_id"] == "case-1"


def test_record_error_exposes_problems():
    with pytest.raises(record.RecordError) as caught:
        record.CaseRecord.from_mapping(base_mapping(status="PASS", failure_kind="business"))
    assert caught.value.problems
