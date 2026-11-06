"""Freeze and execute bounded P0 acceptance on owned public code; no automatic publication."""

import argparse
import base64
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

from mgar_v3.agents.data import provision
from mgar_v3.commerce.budget import Authorization as GrantAuthorization
from mgar_v3.commerce.budget import authorize
from mgar_v3.commerce.domain import CaseInput, RepairRequest
from mgar_v3.commerce.setup import owned_snapshot
from mgar_v3.coordination.engine import Engine
from mgar_v3.domain import DomainError, digest
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.graph.runner import Runner
from mgar_v3.live import Authorization, LiveProfile
from mgar_v3.rag.domain import ACLUpdate, Upload
from mgar_v3.repair.domain import RepairScope
from mgar_v3.repair.source import github_commit, verify_snapshot
from mgar_v3.settings import ROOT, Settings

VERSION = "p0-commerce-live-v3"
BASE_BRANCH_PREFIX = "p0-commerce-live-v2"
DIRECTORY = ROOT / ".local" / VERSION
PUBLIC = ROOT / "eval" / VERSION


def write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, default=str) + "\n")
    path.chmod(0o600)


def context(base: Settings, project: str) -> Settings:
    # A fresh 30-minute observation window is frozen before any call, never extended afterward.
    return base.model_copy(
        update={
            "project_id": project,
            "r4_sql_ttl_seconds": 1800,
            "commerce_directory": DIRECTORY / project,
        }
    )


def prepare(e: Engine, base: Settings, issue: int) -> dict[str, Any]:
    if (DIRECTORY / "prepared.json").exists():
        return json.loads((DIRECTORY / "prepared.json").read_text())
    source = owned_snapshot(base.repository.lower())
    remote, entries = github_commit(source.repository, source.base_sha)
    if remote["commit"]["tree"]["sha"] != source.base_tree or entries != source.tree_entries:
        raise DomainError(
            "SOURCE_CHANGED", "Owned local commit differs from fetched GitHub objects"
        )
    source = source.model_copy(update={"origin": "github_commit"})
    verify_snapshot(source)
    base_source = source
    fixtures = json.loads((ROOT / "fixtures/commerce/data.json").read_text())
    scopes = [
        ("N01", "duplicate_paid", None),
        ("N02", "status_filter", None),
        ("N03", "refunded_status", None),
        ("N04", "status_filter", "missing_sql"),
        ("N05", "duplicate_paid", "revoked_knowledge"),
    ]
    profiles = []
    cases = []
    expires = datetime.now(UTC) + timedelta(hours=4)
    for label, kind, negative in scopes:
        project = VERSION + "-" + label.lower()
        e.settings = context(base, project)
        directory = DIRECTORY / project
        source = base_source.model_copy(
            update={
                "base_branch": "main"
                if negative
                else BASE_BRANCH_PREFIX + "-" + label.lower() + "-base"
            }
        )
        source_path = directory / "source.json"
        write(source_path, source.model_dump(mode="json"))
        raw = source_path.read_bytes()
        source_hash = e.repairs.artifact(source.model_dump_json().encode())
        job = e.knowledge.upload(
            Upload(
                title="Commerce revenue policy v1",
                source_uri="kb://commerce/revenue-v1",
                source_version="commerce-revenue-v1",
                media_type="text/markdown",
                content_base64=base64.b64encode(
                    (ROOT / "fixtures/commerce/policy.md").read_bytes()
                ).decode(),
            )
        )
        snapshot_id = e.knowledge.index(job["job_id"])
        knowledge, chunks, _ = e.knowledge.eligible(snapshot_id, base.local_actor)
        reader_path = directory / "reader.json"
        reader = provision(e, reader_path, fixtures["datasets"][kind])
        common = dict(
            project_id=project,
            repository=source.repository,
            issue_number=issue,
            actor=base.local_actor,
            source_kind="file",
            source_ref=source.base_sha,
            source_path=source.editable_paths[0],
            expected_source_hash=hashlib.sha256(
                source.files[source.editable_paths[0]].encode()
            ).hexdigest(),
            model_id="deepseek-flash",
            price_version="p0-20261106-official-20261105-peak",
            input_micros_per_million=300000,
            output_micros_per_million=1200000,
            context_tokens=8192,
            max_output_tokens=512,
            max_source_bytes=16384,
            max_model_calls=3,
            max_cost_micros=9216,
        )
        investigation = LiveProfile(
            **common,
            investigation_version="r4-experts-v1",
            case_id=VERSION + ":" + label + ":investigation",
            question="Investigate "
            + kind
            + ": verify paid-only unique-order revenue in the registered reporting window "
            "against policy and code.",
            knowledge_snapshot_ids=[str(snapshot_id)],
            rag_config_hash=knowledge["config_hash"],
            data_fixture_hash=reader["fixture_hash"],
        )
        stages = [("investigation", investigation)]
        if not negative:
            stages.append(
                (
                    "repair",
                    LiveProfile(
                        **common,
                        investigation_version="r5-repair-v1",
                        case_id=VERSION + ":" + label + ":repair",
                        action_type="draft_pr",
                        question="Fix PaidRevenueCents: count each paid order ID once; "
                        "ignore all other statuses; preserve exact integer cents.",
                        repair=RepairScope(
                            snapshot_sha256=hashlib.sha256(raw).hexdigest(),
                            base_tree=source.base_tree,
                            base_branch=source.base_branch,
                            workspace_prefix=source.workspace_prefix,
                            predecessor_budget_hashes=[investigation.profile_hash],
                        ),
                    ),
                )
            )
        refs = {}
        for stage, profile in stages:
            path = directory / (stage + "-profile.json")
            authorization = directory / (stage + "-authorization.json")
            write(path, profile.model_dump(mode="json"))
            # Native accurate-payload Action approval remains required before any external write.
            write(
                authorization,
                Authorization(
                    profile_hash=profile.profile_hash,
                    approved_by=base.local_actor,
                    expires_at=expires,
                    allow_paid=True,
                    allow_external_write=True,
                ).model_dump(mode="json"),
            )
            refs[stage] = {
                "profile": str(path),
                "authorization": str(authorization),
                "snapshot": str(source_path),
            }
            profiles.append(
                {
                    "project_id": project,
                    "issue_number": issue,
                    "template_id": kind,
                    "stage": stage,
                    "profile_hash": profile.profile_hash,
                }
            )
        manifest = {
            "kind": kind,
            "execution_mode": "live",
            "project_id": project,
            "repository": source.repository,
            "actor": base.local_actor.lower(),
            "reader_path": str(reader_path),
            "data_profile": reader,
            "snapshot_hash": source_hash,
            "source_ref": source.base_sha,
            "source_tree": source.base_tree,
            "source_path": source.editable_paths[0],
            "source_content_hash": common["expected_source_hash"],
            "knowledge_snapshot_ids": [str(snapshot_id)],
            "knowledge_snapshot_hash": knowledge["manifest_hash"],
            "knowledge_config_hash": knowledge["config_hash"],
            "knowledge_document_ids": sorted({str(c["document_id"]) for c in chunks}),
            "policy": fixtures["policy"],
            "acceptance_profile_hash": digest({"policy": fixtures["policy"], "task_kind": kind}),
            "live": refs,
        }
        cases.append({"id": label, "negative": negative, "manifest": manifest})
    with e.transaction() as conn:
        prior = e.benchmarks.cost(conn, source.repository)
    from verify import source_fingerprint

    manifest = {
        "schema": VERSION,
        "repository": source.repository,
        "source_ref": source.base_sha,
        "source_tree": source.base_tree,
        "source_fingerprint": source_fingerprint(ROOT),
        "prior_cost_micros": int(prior["cost_micros"]),
        "prior_calls": prior["calls"],
        "source_branches": [BASE_BRANCH_PREFIX + "-n0" + str(i) + "-base" for i in range(1, 4)],
        "price_evidence_hash": hashlib.sha256(
            (ROOT / "docs/repairs/R6-HOST-20261105/v2-current-official-pricing.json").read_bytes()
        ).hexdigest(),
        "max_new_calls": 18,
        "max_new_cost_micros": 55296,
        "profiles": profiles,
        "sql_ttl_seconds": 1800,
        "issue_anchor": issue,
        "external_writes": "ACCURATE_ACTION_APPROVAL_REQUIRED",
        "oracle": "docs/design/p0-commerce-v1/acceptance-plan.json",
        "outbound": "https://api.deepseek.com/v1/chat/completions",
        "source_acquisition": "native_git_fetch_full_tree_verified_against_owned_local_commit",
    }
    prepared = {"manifest": manifest, "cases": cases}
    write(DIRECTORY / "prepared.json", prepared)
    write(PUBLIC / "manifest.json", manifest)
    return prepared


def run(e: Engine, base: Settings, authorization: Path) -> dict[str, Any]:
    prepared = json.loads((DIRECTORY / "prepared.json").read_text())
    manifest = prepared["manifest"]
    from verify import source_fingerprint

    if source_fingerprint(ROOT) != manifest["source_fingerprint"]:
        raise DomainError("SOURCE_CHANGED", "Freeze a new version before paid execution")
    auth = GrantAuthorization.model_validate_json(authorization.read_bytes())
    grant = authorize(e.commerce, manifest, auth)
    oracle = json.loads((ROOT / "docs/design/p0-commerce-v1/acceptance-plan.json").read_text())
    outputs = []
    for case in prepared["cases"]:
        m = case["manifest"]
        e.settings = context(base, m["project_id"])
        m["live"]["grant_hash"] = grant
        e.commerce.register(m["kind"], m)
        body = CaseInput(
            template_id=m["kind"],
            issue_number=manifest["issue_anchor"],
            window=m["policy"]["window"],
            goal="report" if case["negative"] else "draft_pr",
            execution_mode="live",
            client_request_id=VERSION + ":" + case["id"],
        )
        receipt = e.commerce.create(body)
        case_id, run_id = UUID(receipt["case_id"]), UUID(receipt["run_id"])
        if case["negative"] == "missing_sql":
            path = Path(m["reader_path"])
            if path.exists():
                path.rename(path.with_suffix(".withheld.json"))
        elif case["negative"] == "revoked_knowledge":
            e.knowledge.acl(
                UUID(m["knowledge_document_ids"][0]), ACLUpdate(readers=[], revoked=True)
            )
        if e.get_run(run_id)["status"] == "queued":
            Runner(e).execute(run_id)
        result = e.commerce.current_result(case_id, "investigation")
        if not result:
            raise DomainError("P0_RESULT_MISSING", "No investigation business result")
        with e.transaction() as conn:
            calls = conn.execute(
                "SELECT count(*) AS n FROM app.usage_calls WHERE run_id=%s", (run_id,)
            ).fetchone()["n"]
            cost = e.benchmarks.cost(conn, base.repository.lower())
            actions_count = conn.execute(
                "SELECT count(*) AS n FROM app.actions WHERE run_id=%s", (run_id,)
            ).fetchone()["n"]
        outcome = {
            "id": case["id"],
            "project_id": m["project_id"],
            "case_id": str(case_id),
            "run_id": str(run_id),
            "business": result["payload"],
            "result_hash": result["result_hash"],
            "calls": calls,
        }
        if case["negative"]:
            expected = "needs_input" if case["negative"] == "missing_sql" else "denied"
            outcome["oracle_passed"] = (
                result["payload"]["business_status"] == expected
                and calls == 0
                and actions_count == 0
            )
        else:
            expected = next(c for c in oracle["normal_cases"] if c["kind"] == m["kind"])[
                "expected_report"
            ]
            outcome["oracle_passed"] = (
                result["payload"]["business_status"] == "passed"
                and result["payload"]["facts"] == expected
            )
        write(DIRECTORY / (case["id"] + "-investigation.json"), outcome)
        outputs.append(outcome)
        if cost["unknown"]:
            raise DomainError("USAGE_UNKNOWN", "Stop the complete frozen P0 experiment")
    for case, outcome in zip(prepared["cases"], outputs, strict=True):
        if case["negative"] or not outcome["oracle_passed"]:
            continue
        m = case["manifest"]
        e.settings = context(base, m["project_id"])
        receipt = e.commerce.start_repair(
            UUID(outcome["case_id"]),
            RepairRequest(
                expected_case_revision=1,
                investigation_result_hash=outcome["result_hash"],
                client_request_id=VERSION + ":" + case["id"] + ":repair",
            ),
        )
        repair_id = UUID(receipt["run_id"])
        if e.get_run(repair_id)["status"] == "queued":
            Runner(e).execute(repair_id)
        result = e.commerce.current_result(UUID(outcome["case_id"]), "repair_proposal")
        actions = e.actions(repair_id)
        outcome["repair_run_id"] = str(repair_id)
        outcome["repair_result"] = result["payload"] if result else None
        outcome["repair_passed"] = bool(
            result and result["payload"]["business_status"] == "passed" and len(actions) == 1
        )
        outcome["actions"] = actions
        write(DIRECTORY / (case["id"] + "-repair.json"), outcome)
        with e.transaction() as conn:
            cost = e.benchmarks.cost(conn, base.repository.lower())
        if cost["unknown"]:
            raise DomainError("USAGE_UNKNOWN", "Stop the complete frozen P0 experiment")
    with e.transaction() as conn:
        cost = e.benchmarks.cost(conn, base.repository.lower())
    report = {
        "schema": VERSION + "-result",
        "manifest_hash": grant,
        "cases": outputs,
        "repository_cost": cost,
        "real_delivery": "NOT_RUN_ACCURATE_ACTION_APPROVAL_REQUIRED",
    }
    write(DIRECTORY / "result.json", report)
    return report


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("command", choices=["prepare", "run"])
    p.add_argument("--issue", type=int, default=1)
    p.add_argument("--authorization", type=Path)
    args = p.parse_args()
    base = Settings()
    e = Engine(base, ArtifactStore(base.artifact_root))
    try:
        e.recover()
        if args.command == "prepare":
            result = prepare(e, base, args.issue)
            print(
                json.dumps(
                    {
                        "manifest_hash": digest(result["manifest"]),
                        "cases": len(result["cases"]),
                        "max_calls": 18,
                        "max_cost_micros": 55296,
                    }
                )
            )
        else:
            if args.authorization is None:
                raise ValueError("Exact frozen authorization file required")
            result = run(e, base, args.authorization)
            print(
                json.dumps(
                    {
                        "manifest_hash": result["manifest_hash"],
                        "repository_cost": result["repository_cost"],
                        "result_path": str(DIRECTORY / "result.json"),
                    },
                    default=str,
                )
            )
    finally:
        e.close()


if __name__ == "__main__":
    main()
