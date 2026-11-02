"""Frozen five-case real R4 trial; prepare is free, execution needs a new exact grant."""

import argparse
import asyncio
import base64
import hashlib
import json
import secrets
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from mgar_v3.agents.data import provision
from mgar_v3.coordination.engine import COMMAND, Engine
from mgar_v3.domain import DomainError, TriggerInput, digest
from mgar_v3.evaluation.domain import TrialAuthorization
from mgar_v3.evaluation.frozen import implementation_hash
from mgar_v3.evaluation.oracle import expected, grade
from mgar_v3.evaluation.provider import TrialExperts
from mgar_v3.evaluation.runtime import mcp_authority
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.graph.experts import execute_experts
from mgar_v3.graph.runner import Runner
from mgar_v3.live import Authorization, LivePermit, LiveProfile
from mgar_v3.mcp.factory import github_client
from mgar_v3.rag.domain import ACLUpdate, Upload
from mgar_v3.settings import ROOT, Settings

PREDECESSOR = "5502fc0230f61f8658fef64adb891729cf668da83c1fcdc2ef440c9b31a740e1"
REF = "cf512bd1e5bedd7d562a513caba326588024b88d"
SOURCE = "v3/src/mgar_v3/agents/experts.py"


def configuration(directory: Path, case: dict[str, Any], paid: bool = False) -> Settings:
    return Settings(
        project_id=case["project_id"],
        artifact_root=directory / "artifacts",
        graph_version="r4-experts-v1",
        rag_mode="local",
        data_profile_path=directory / case["id"] / "reader.json",
        allow_paid=paid,
        allow_external_write=False,
        router_mode="disabled",
        github_use_gh_token=True,
        github_mcp_binary=ROOT / ".local/tools/github-mcp-server/v2.0.3/github-mcp-server",
        live_profile_path=None,
        live_authorization_path=None,
    )


def write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n")
    path.chmod(0o600)


def prepare(directory: Path) -> dict[str, Any]:
    if (directory / "manifest.json").exists():
        raise RuntimeError("Frozen trial already exists; do not overwrite it")
    directory.mkdir(parents=True, exist_ok=True)
    cases = json.loads((ROOT / "eval/r4-live/cases.json").read_text())
    token = uuid4().hex[:10]
    manifest = {
        "schema": "r4-paid-trial-v1",
        "trial_token": token,
        "predecessor_budget_hash": PREDECESSOR,
        "additional_call_limit": 24,
        "cumulative_cost_limit_micros": 2000000,
        "arms": ["multi", "single"],
        "design": (
            "fixed identical read tool plan; multi has Knowledge/Repo model "
            "extraction and shared synthesis; single reads serially and uses "
            "one synthesis model"
        ),
        "external_writes": False,
        "cases": [],
        "profiles": [],
        "case_input_hash": digest(cases),
        "implementation_sha256": implementation_hash(ROOT),
        "provider_code_sha256": hashlib.sha256(
            (ROOT / "src/mgar_v3/evaluation/provider.py").read_bytes()
        ).hexdigest(),
        "oracle_code_sha256": hashlib.sha256(
            (ROOT / "src/mgar_v3/evaluation/oracle.py").read_bytes()
        ).hexdigest(),
    }
    oracles = {}
    canary = "R4_REVOKED_" + secrets.token_hex(12)
    for index, case in enumerate(cases):
        case = {**case, "project_id": "r4-" + token + "-" + str(index)}
        folder = directory / case["id"]
        folder.mkdir(parents=True, exist_ok=True)
        cfg = configuration(directory, case)
        engine = Engine(cfg, ArtifactStore(cfg.artifact_root))
        try:
            with engine.transaction() as conn:
                prior = conn.execute(
                    "SELECT * FROM app.authorization_budgets WHERE profile_hash=%s", (PREDECESSOR,)
                ).fetchone()
                if (
                    not prior
                    or prior["used_calls"] != 3
                    or prior["call_limit"] != 3
                    or prior["reserved_micros"]
                ):
                    raise RuntimeError("Expected original settled three-call budget")
            manifest["prior_cost_micros"] = prior["settled_micros"]
            if "repository_source" not in manifest:
                source = asyncio.run(
                    github_client(cfg, cfg.repository, max_bytes=16384).read_file(SOURCE, REF)
                )
                manifest["repository_source"] = {
                    "commit": REF,
                    "path": SOURCE,
                    "sha256": hashlib.sha256(source.content).hexdigest(),
                    "version": source.version,
                    "uri": source.uri,
                    "transport": "official_GitHub_MCP_stdio_read_only",
                }
                (directory / "repository-source.py").write_bytes(source.content)
            policy = (ROOT / "eval/r4-live/policy.md").read_text()
            if case["kind"] == "revoked":
                policy += "\nInternal revocation canary: " + canary + "\n"
            job = engine.knowledge.upload(
                Upload(
                    title="支付订单统计口径",
                    source_uri="kb://trial/" + case["id"],
                    source_version="r4-live-v1",
                    media_type="text/markdown",
                    content_base64=base64.b64encode(policy.encode()).decode(),
                )
            )
            snapshot = engine.knowledge.index(job["job_id"])
            _, chunks, _ = engine.knowledge.eligible(snapshot, cfg.local_actor)
            case["snapshot_id"] = str(snapshot)
            case["rag_config_hash"] = digest(engine.knowledge.config())
            if case["kind"] == "revoked":
                case["denied_chunk_id"] = str(chunks[0]["chunk_id"])
                engine.knowledge.acl(chunks[0]["document_id"], ACLUpdate(revoked=True))
            rows = case["rows"]
            if case["kind"] == "revoked":
                rows = cases[0]["rows"]
            if rows is not None:
                data = provision(engine, cfg.data_profile_path, rows)
                case["data_fixture_hash"] = data["fixture_hash"]
            else:
                case["data_fixture_hash"] = digest({"intentionally_missing": True})
            oracles[case["id"]] = expected(rows, case["kind"])
            if case["kind"] == "revoked":
                oracles[case["id"]]["canary"] = canary
            manifest["cases"].append(case)
            for arm in manifest["arms"]:
                profile = LiveProfile(
                    investigation_version="r4-experts-v1",
                    knowledge_snapshot_ids=[str(snapshot)],
                    rag_config_hash=case["rag_config_hash"],
                    data_fixture_hash=case["data_fixture_hash"],
                    case_id="r4:" + token + ":" + case["id"] + ":" + arm,
                    project_id=cfg.project_id,
                    repository=cfg.repository,
                    issue_number=1,
                    actor=cfg.local_actor,
                    question=case["question"],
                    source_kind="file",
                    source_path=SOURCE,
                    source_ref=REF,
                    expected_source_hash=manifest["repository_source"]["sha256"],
                    model_id="deepseek-flash",
                    price_version="deepseek-2026-11-01-peak-upper-bound",
                    input_micros_per_million=300000,
                    output_micros_per_million=1200000,
                    context_tokens=16384,
                    max_output_tokens=2048,
                    max_source_bytes=16384,
                    max_cost_micros=22119,
                    max_model_calls=3,
                )
                manifest["profiles"].append(
                    {"case_id": case["id"], "arm": arm, "profile": profile.model_dump(mode="json")}
                )
        finally:
            engine.close()
    write(directory / "oracle.json", oracles)
    (directory / "oracle.json").chmod(0o400)
    manifest["oracle_sha256"] = hashlib.sha256((directory / "oracle.json").read_bytes()).hexdigest()
    manifest["planned_calls"] = 19
    manifest["per_call_upper_bound_micros"] = 7373
    manifest["additional_cost_upper_bound_micros"] = 24 * 7373
    manifest["frozen_at"] = datetime.now(UTC).isoformat()
    write(directory / "manifest.json", manifest)
    write(
        directory / "prepared.json",
        {
            "manifest_hash": digest(manifest),
            "status": "PREPARED_NOT_AUTHORIZED",
            "new_paid_calls": 0,
            "additional_call_limit": 24,
            "planned_calls": 19,
            "additional_upper_bound_usd": 0.176952,
        },
    )
    return manifest


def execute(directory: Path, authorization_path: Path) -> dict[str, Any]:
    manifest = json.loads((directory / "manifest.json").read_text())
    auth = TrialAuthorization.model_validate_json(authorization_path.read_bytes())
    if implementation_hash(ROOT) != manifest["implementation_sha256"]:
        raise RuntimeError("Frozen application or experiment implementation changed")
    if (
        hashlib.sha256((directory / "oracle.json").read_bytes()).hexdigest()
        != manifest["oracle_sha256"]
    ):
        raise RuntimeError("Frozen business oracle changed")
    for name in ("provider", "oracle"):
        actual = hashlib.sha256(
            (ROOT / f"src/mgar_v3/evaluation/{name}.py").read_bytes()
        ).hexdigest()
        if actual != manifest[name + "_code_sha256"]:
            raise RuntimeError("Frozen evaluation implementation changed")
    oracle = json.loads((directory / "oracle.json").read_text())
    results = []
    trial_id = None
    state_path = directory / "execution.json"
    state = (
        json.loads(state_path.read_text()) if state_path.exists() else {"runs": {}, "results": []}
    )
    for case in manifest["cases"]:
        cfg = configuration(directory, case, paid=True)
        engine = Engine(cfg, ArtifactStore(cfg.artifact_root))
        try:
            if digest(engine.knowledge.config()) != case["rag_config_hash"]:
                raise RuntimeError("Frozen retrieval model configuration changed")
            if case["kind"] == "missing" and cfg.data_profile_path.exists():
                raise RuntimeError("The missing-data test unexpectedly acquired a data source")
            if (
                case["kind"] != "missing"
                and engine.tools.reader(cfg.local_actor).profile["fixture_hash"]
                != case["data_fixture_hash"]
            ):
                raise RuntimeError("Frozen data profile changed")
            trial = engine.trials.authorize(manifest, auth)
            trial_id = trial["trial_id"]
            for arm in manifest["arms"]:
                key = case["id"] + ":" + arm
                if key in state["runs"]:
                    raise RuntimeError(
                        "A recorded trial arm must be inspected, never silently resent"
                    )
                frozen = next(
                    v
                    for v in manifest["profiles"]
                    if v["case_id"] == case["id"] and v["arm"] == arm
                )
                profile = LiveProfile.model_validate(frozen["profile"])
                engine.trials.bind(trial_id, profile, case["id"], arm)
                permit = LivePermit(
                    profile,
                    Authorization(
                        profile_hash=profile.profile_hash,
                        approved_by=auth.approved_by,
                        expires_at=auth.expires_at,
                        allow_paid=True,
                        allow_external_write=False,
                    ),
                    cfg,
                )
                profile_path = directory / case["id"] / (arm + "-profile.json")
                auth_path = directory / case["id"] / (arm + "-authorization.json")
                write(profile_path, profile.model_dump(mode="json"))
                write(auth_path, permit.authorization.model_dump(mode="json"))
                cfg.live_profile_path = profile_path
                cfg.live_authorization_path = auth_path
                event = TriggerInput(
                    source="cli",
                    external_event_id="r4-trial:" + manifest["trial_token"] + ":" + key,
                    project_id=cfg.project_id,
                    repository=cfg.repository,
                    issue_number=1,
                    actor=cfg.local_actor,
                    body=COMMAND + profile.question,
                    execution_mode="live",
                )
                with engine.transaction():
                    receipt = engine.ingest(event, permit)
                    assert receipt.run_id
                    engine.tools.requested_snapshots(receipt.run_id, [UUID(case["snapshot_id"])])
                state["runs"][key] = str(receipt.run_id)
                write(state_path, state)
                started = time.monotonic()
                error = None
                try:
                    with mcp_authority(engine):
                        execute_experts(
                            Runner(engine), receipt.run_id, TrialExperts(engine, permit, case, arm)
                        )
                except DomainError as exc:
                    error = exc.code
                    engine.fail_run(receipt.run_id, exc.code)
                with engine.transaction() as conn:
                    analysis = conn.execute(
                        "SELECT * FROM app.r4_analyses WHERE run_id=%s", (receipt.run_id,)
                    ).fetchone()
                    reads = conn.execute(
                        (
                            "SELECT e.*,r.role,r.task_id,r.origin FROM app.evidence e JOIN "
                            "app.r4_reads r USING(evidence_id) WHERE e.run_id=%s"
                        ),
                        (receipt.run_id,),
                    ).fetchall()
                    calls = conn.execute(
                        "SELECT * FROM app.usage_calls WHERE run_id=%s", (receipt.run_id,)
                    ).fetchall()
                    experts = conn.execute(
                        "SELECT * FROM app.expert_results WHERE run_id=%s", (receipt.run_id,)
                    ).fetchall()
                events = engine.events(receipt.run_id)
                run = engine.get_run(receipt.run_id)
                resolution = engine.tools.resolution(receipt.run_id)
                forbidden = False
                canary = oracle[case["id"]].get("canary")
                if canary:
                    for eventrow in events:
                        if eventrow["kind"] in {
                            "model_response_recorded",
                            "model_input_recorded",
                            "trial_synthesis_input",
                        }:
                            content = engine.artifacts.read(
                                eventrow["safe_payload"]["content_hash"]
                            ).decode(errors="replace")
                            forbidden = forbidden or canary in content
                adjudication = grade(
                    analysis["result"] if analysis else {},
                    oracle[case["id"]],
                    {str(r["evidence_id"]): r["role"] for r in reads},
                    mechanical=bool(resolution and resolution["verdict"]["passed"]),
                    forbidden_seen=forbidden,
                )
                if case["kind"] != "normal":
                    required = (
                        {"NOT_FOUND", "KNOWLEDGE_REVOKED"}
                        if case["kind"] == "revoked"
                        else {"DATA_SOURCE_NOT_CONFIGURED"}
                    )
                    exercised = any(
                        e["kind"] == "readonly_tool_denied"
                        and e["safe_payload"].get("reason_code") in required
                        for e in events
                    )
                    if not exercised:
                        adjudication["passed"] = False
                        adjudication["reasons"].append("negative_boundary_not_exercised")
                result = {
                    "case_id": case["id"],
                    "kind": case["kind"],
                    "arm": arm,
                    "run_id": str(receipt.run_id),
                    "status": "PASS" if adjudication["passed"] and error is None else "FAIL",
                    "error_code": error,
                    "adjudication": adjudication,
                    "duration_seconds": round(time.monotonic() - started, 3),
                    "physical_calls": len(calls),
                    "cost_micros": sum(c["settled_micros"] or 0 for c in calls),
                    "prompt_tokens": sum((c["usage"] or {}).get("prompt_tokens", 0) for c in calls),
                    "completion_tokens": sum(
                        (c["usage"] or {}).get("completion_tokens", 0) for c in calls
                    ),
                    "unresolved_calls": sum(c["status"] != "known" for c in calls),
                    "report_hash": run["report_hash"],
                    "assessment": analysis["result"] if analysis else None,
                }
                audit = {
                    "result": result,
                    "reads": reads,
                    "calls": calls,
                    "experts": experts,
                    "events": events,
                    "resolution": resolution,
                }
                write(directory / case["id"] / (arm + "-execution.json"), audit)
                if run["report_hash"]:
                    (directory / case["id"] / (arm + "-report.md")).write_bytes(
                        engine.artifact_bytes(run["report_hash"])
                    )
                results.append(result)
                state["results"].append(result)
                write(state_path, state)
                # Trials have no dispatcher; even complete interpretations are never published.
                engine.cancel(receipt.run_id)
                if result["unresolved_calls"]:
                    raise RuntimeError("Unknown model request: stop the trial without retry")
        finally:
            engine.close()
    result = {
        "schema": "r4-business-comparison-v1",
        "status": "PASS" if all(r["status"] == "PASS" for r in results) else "FAIL",
        "manifest_hash": digest(manifest),
        "trial_id": str(trial_id),
        "results": results,
        "new_github_comments": 0,
        "scope": (
            "five frozen owned synthetic business cases; actual model, GitHub "
            "code, local RAG and PostgreSQL/MCP; fixed tool-plan development "
            "comparison"
        ),
    }
    write(directory / "result.json", result)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("phase", choices=["prepare", "execute", "report"])
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--authorization", type=Path)
    parser.add_argument("--execute-paid", action="store_true")
    args = parser.parse_args()
    if args.phase == "prepare":
        result = prepare(args.directory.resolve())
        print(
            json.dumps(
                {
                    "status": "PREPARED_NOT_AUTHORIZED",
                    "manifest_hash": digest(result),
                    "paid_calls": 0,
                }
            )
        )
        return
    if args.phase == "report":
        print((args.directory / "result.json").read_text())
        return
    if not args.execute_paid or args.authorization is None:
        raise SystemExit("Exact new trial authorization is required")
    result = execute(args.directory.resolve(), args.authorization)
    print(
        json.dumps(
            {
                "status": result["status"],
                "cases": len(result["results"]),
                "physical_calls": sum(r["physical_calls"] for r in result["results"]),
                "cost_micros": sum(r["cost_micros"] for r in result["results"]),
            }
        )
    )
    raise SystemExit(0 if result["status"] == "PASS" else 1)


if __name__ == "__main__":
    main()
