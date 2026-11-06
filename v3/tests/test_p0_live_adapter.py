"""Native live adapters with controlled HTTP only; never a paid acceptance claim."""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import SecretStr
from test_p0_commerce import commerce as commerce

from mgar_v3.commerce.domain import RepairRequest
from mgar_v3.domain import DomainError
from mgar_v3.graph.runner import Runner
from mgar_v3.live import Authorization, LiveProfile
from mgar_v3.repair.domain import RepairScope, Snapshot


def configure(commerce, tmp_path, max_calls=6):
    e = commerce.engine
    m = dict(commerce.template("duplicate_paid")["manifest"])
    snapshot = Snapshot.model_validate_json(e.artifacts.read(m["snapshot_hash"]))
    # This disposable controlled scope is not remote-source acceptance evidence.
    snapshot = snapshot.model_copy(update={"origin": "github_commit"})
    raw = snapshot.model_dump_json().encode()
    source_path = tmp_path / "controlled-source.json"
    source_path.write_bytes(raw)
    m["snapshot_hash"] = e.repairs.artifact(raw)
    e.settings.allow_paid = True
    e.settings.allow_external_write = True
    e.settings.deepseek_api_key = SecretStr("controlled-http-key")
    common = dict(
        project_id=e.settings.project_id,
        repository=e.settings.repository,
        actor="owner",
        issue_number=9401,
        source_kind="file",
        source_ref=snapshot.base_sha,
        source_path=m["source_path"],
        expected_source_hash=m["source_content_hash"],
        model_id="deepseek-flash",
        price_version="controlled-no-paid-call",
        input_micros_per_million=300000,
        output_micros_per_million=1200000,
        context_tokens=8192,
        max_output_tokens=512,
        max_cost_micros=9216,
        max_model_calls=3,
    )
    investigation = LiveProfile(
        **common,
        investigation_version="r4-experts-v1",
        case_id="p0-controlled-investigation",
        question="Investigate paid order revenue and source policy.",
        knowledge_snapshot_ids=m["knowledge_snapshot_ids"],
        rag_config_hash=m["knowledge_config_hash"],
        data_fixture_hash=m["data_profile"]["fixture_hash"],
    )
    repair = LiveProfile(
        **common,
        investigation_version="r5-repair-v1",
        case_id="p0-controlled-repair",
        question="Fix the paid-only unique-order revenue function.",
        action_type="draft_pr",
        repair=RepairScope(
            snapshot_sha256=hashlib.sha256(raw).hexdigest(),
            base_tree=snapshot.base_tree,
            workspace_prefix=snapshot.workspace_prefix,
            predecessor_budget_hashes=[investigation.profile_hash],
        ),
    )
    refs = {}
    for stage, profile in [("investigation", investigation), ("repair", repair)]:
        profile_path = tmp_path / (stage + "-profile.json")
        authorization_path = tmp_path / (stage + "-authorization.json")
        auth = Authorization(
            profile_hash=profile.profile_hash,
            approved_by="owner",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            allow_paid=True,
            allow_external_write=True,
        )
        profile_path.write_text(profile.model_dump_json())
        authorization_path.write_text(auth.model_dump_json())
        refs[stage] = {
            "profile": str(profile_path),
            "authorization": str(authorization_path),
            "snapshot": str(source_path),
        }
    # Start a fresh registration in this disposable DB; no original result is altered.
    with e.transaction() as conn:
        conn.execute("TRUNCATE app.commerce_templates CASCADE")
    from mgar_v3.commerce.budget import Authorization as GrantAuthorization
    from mgar_v3.commerce.budget import authorize
    from mgar_v3.domain import digest

    manifest = {
        "repository": e.settings.repository,
        "prior_cost_micros": 0,
        "max_new_calls": max_calls,
        "max_new_cost_micros": 18432,
        "profiles": [
            {
                "project_id": e.settings.project_id,
                "issue_number": 9401,
                "template_id": "duplicate_paid",
                "stage": stage,
                "profile_hash": profile.profile_hash,
            }
            for stage, profile in [("investigation", investigation), ("repair", repair)]
        ],
    }
    grant_hash = authorize(
        commerce,
        manifest,
        GrantAuthorization(
            manifest_hash=digest(manifest),
            approved_by="owner",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            allow_paid=True,
            max_new_calls=max_calls,
            max_new_cost_micros=18432,
        ),
    )
    m.update(execution_mode="live", live={**refs, "grant_hash": grant_hash})
    commerce.register("duplicate_paid", m)
    return m, investigation, repair


def install_http(monkeypatch, unknown=False):
    requests = []
    from mgar_v3.models.structured import StructuredModel

    def answer(request):
        body = json.loads(request.content)
        requests.append(body)
        if unknown:
            return httpx.Response(400, json={"error": {"message": "Controlled unknown usage"}})
        content = json.loads(body["messages"][1]["content"])
        if "quote_catalog" in content:
            reply = {
                "quote_ids": [next(iter(content["quote_catalog"]))],
                "explanation": "Source unit selected",
            }
        else:
            reply = {
                "function_body": (
                    "total := 0\nseen := make(map[string]bool)\n"
                    "for _, order := range orders {\n"
                    'if order.Status == "paid" && !seen[order.ID] {\n'
                    "seen[order.ID] = true\ntotal += order.Amount\n}\n}\nreturn total"
                ),
                "explanation": "Controlled body follows the public policy",
            }
        return httpx.Response(
            200,
            json={
                "model": "deepseek-flash",
                "choices": [{"message": {"content": json.dumps(reply)}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 100},
            },
        )

    class ControlledModel(StructuredModel):
        def complete(self, *args, **kwargs):
            with httpx.Client(transport=httpx.MockTransport(answer)) as client:
                self.client = client
                return super().complete(*args, **kwargs)

    monkeypatch.setattr("mgar_v3.commerce.provider.StructuredModel", ControlledModel)
    return requests


def test_native_live_stage_permits_body_provenance_and_no_repeat(commerce, tmp_path, monkeypatch):
    _, investigation, repair = configure(commerce, tmp_path)
    requests = install_http(monkeypatch)
    body = {
        "template_id": "duplicate_paid",
        "issue_number": 9401,
        "window": commerce.template("duplicate_paid")["manifest"]["policy"]["window"],
        "goal": "draft_pr",
        "execution_mode": "live",
        "client_request_id": str(uuid4()),
    }
    from mgar_v3.commerce.domain import CaseInput

    case = commerce.create(CaseInput.model_validate(body))
    case_id = UUID(case["case_id"])
    Runner(commerce.engine).execute(UUID(case["run_id"]))
    result = commerce.result_view(case_id)
    assert result["result"]["business_status"] == "passed", result
    receipt = commerce.start_repair(
        case_id,
        RepairRequest(
            expected_case_revision=1,
            investigation_result_hash=result["result_hash"],
            client_request_id=str(uuid4()),
        ),
    )
    run_id = UUID(receipt["run_id"])
    action_id = Runner(commerce.engine).execute(run_id)
    assert action_id
    action = commerce.engine.actions(run_id)[0]
    assert action["payload"]["model_receipts"][0]["transformation"] == "function_body_v1"
    assert len(requests) == 3
    with commerce.engine.transaction() as conn:
        rows = conn.execute(
            "SELECT profile_hash,status,result FROM app.usage_calls ORDER BY step_key"
        ).fetchall()
    assert {r["profile_hash"] for r in rows} == {investigation.profile_hash, repair.profile_hash}
    assert all(r["status"] == "known" for r in rows)
    assert any("function_body" in r["result"] and "diff" not in r["result"] for r in rows)
    commerce.engine.approve(action_id, action["payload_hash"], "owner")
    assert len(requests) == 3


def test_unknown_native_model_blocks_next_case_without_resend(commerce, tmp_path, monkeypatch):
    configure(commerce, tmp_path)
    requests = install_http(monkeypatch, unknown=True)
    from mgar_v3.commerce.domain import CaseInput

    body = CaseInput(
        template_id="duplicate_paid",
        issue_number=9401,
        window=commerce.template("duplicate_paid")["manifest"]["policy"]["window"],
        execution_mode="live",
        goal="draft_pr",
        client_request_id=str(uuid4()),
    )
    receipt = commerce.create(body)
    run_id = UUID(receipt["run_id"])
    Runner(commerce.engine).execute(run_id)
    result = commerce.current_result(UUID(receipt["case_id"]), "investigation")
    assert result["payload"]["business_status"] == "unknown"
    with pytest.raises(DomainError) as error:
        commerce.create(body.model_copy(update={"client_request_id": str(uuid4())}))
    assert error.value.code == "OUTCOME_UNKNOWN"
    count = len(requests)
    with pytest.raises(DomainError):
        Runner(commerce.engine).execute(run_id)
    assert len(requests) == count
    with commerce.engine.transaction() as conn:
        row = conn.execute(
            "SELECT count(*) AS n,sum(reserved_micros) AS held FROM app.usage_calls "
            "WHERE status='unknown'"
        ).fetchone()
    assert row["n"] >= 1 and row["held"] > 0


def test_shared_frozen_p0_grant_blocks_remaining_profile_allowance(commerce, tmp_path, monkeypatch):
    configure(commerce, tmp_path, max_calls=1)
    requests = install_http(monkeypatch)
    from mgar_v3.commerce.domain import CaseInput

    body = CaseInput(
        template_id="duplicate_paid",
        issue_number=9401,
        window=commerce.template("duplicate_paid")["manifest"]["policy"]["window"],
        execution_mode="live",
        goal="draft_pr",
        client_request_id=str(uuid4()),
    )
    receipt = commerce.create(body)
    Runner(commerce.engine).execute(UUID(receipt["run_id"]))
    assert len(requests) == 1
    with commerce.engine.transaction() as conn:
        count = conn.execute("SELECT count(*) AS n FROM app.usage_calls").fetchone()["n"]
    assert count == 1
