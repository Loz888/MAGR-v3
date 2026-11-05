"""Real evidence connector/context checks with zero model calls and no output grading."""

import gc
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from mgar_v3.benchmark.domain import Case
from mgar_v3.benchmark.executor import SYSTEM, SYSTEM_V5, bounded
from mgar_v3.benchmark.prepare import configuration, write
from mgar_v3.benchmark.run import verify
from mgar_v3.benchmark.tools import collect
from mgar_v3.benchmark.wire import quote_sources, response_schema
from mgar_v3.coordination.engine import Engine
from mgar_v3.domain import DomainError, digest
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.proactivity.domain import Activation, Policy, ProactiveEvent


def preflight(directory: Path) -> dict[str, Any]:
    manifest = verify(directory)
    inputs = json.loads((directory / "inputs.json").read_text())
    rows = []
    for raw in manifest["cases"]:
        case = Case.model_validate(raw)
        profile = next(p["profile"] for p in manifest["profiles"] if p["case_id"] == case.id)
        cfg = configuration(directory, profile["project_id"])
        engine = Engine(cfg, ArtifactStore(cfg.artifact_root))
        run = None
        created = False
        policy = Policy(
            repository=cfg.repository,
            kinds=["pr_comment"],
            allowed_actors=[cfg.local_actor],
            source_ref=manifest["source_ref"],
            question=case.question,
            expires_at=datetime.now(UTC) + timedelta(minutes=20),
            max_reads=16,
        )
        try:
            engine.proactivity.create(policy, cfg.local_actor)
            created = True
            engine.proactivity.activate(
                Activation(policy_hash=policy.policy_hash, mode="auto"), cfg.local_actor
            )
            run = engine.proactivity.observe(
                ProactiveEvent(
                    external_event_id="preflight:" + digest([digest(manifest), case.id]),
                    repository=cfg.repository,
                    actor=cfg.local_actor,
                    kind="pr_comment",
                    issue_number=1,
                    source_ref=manifest["source_ref"],
                )
            )[0]["run_id"]
            epoch = engine.begin_run(run)["execution_epoch"]
            sources, errors, stats = collect(engine, run, epoch, case, inputs, ablation="none")
            if case.category != "negative" and not set(case.required_roles) <= {
                s.role for s in sources
            }:
                raise DomainError(
                    "BENCHMARK_CONNECTOR_FAILED", "Required normal-case evidence missing"
                )
            payload = {
                "question": case.question,
                "required_roles": case.required_roles,
                "sources": (
                    quote_sources(bounded(sources))[0] if case.output_contract else bounded(sources)
                ),
                "source_errors": errors,
                "expert_notes": [
                    {"role": r, "required": r in case.required_roles, "notes": "x" * 600}
                    for r in ["knowledge", "repo"]
                ],
                "answer_schema": response_schema(case).model_json_schema(),
                "output_contract": (
                    case.output_contract.model_dump(mode="json") if case.output_contract else None
                ),
                "required_contract_version": (
                    case.required_revision if case.output_contract else "2026-final-v1"
                ),
                "capabilities": ["read", "local_patch_proposal"],
            }
            if case.category == "repair":
                payload["files"] = inputs["repair_snapshots"][case.id]["files"]
                payload["editable_paths"] = inputs["repair_snapshots"][case.id]["editable_paths"]
            upper = (
                len(
                    (
                        (SYSTEM_V5 if case.output_contract else SYSTEM)
                        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
                    ).encode()
                )
                + 2048
                + 1024
            )
            if upper > 12288:
                raise DomainError("MODEL_INPUT_LIMIT", "Conservative prompt bound does not fit")
            rows.append(
                {"case_id": case.id, "context_upper_bound": upper, **stats, "source_errors": errors}
            )
        finally:
            if run:
                engine.cancel(run)
            if created:
                engine.proactivity.activate(
                    Activation(policy_hash=policy.policy_hash, mode="revoked"), cfg.local_actor
                )
            engine.close()
            engine.knowledge.embedder = None
            engine.knowledge.ranker = None
            gc.collect()
    result = {
        "status": "PASS",
        "scope": "real evidence connectors and conservative prompt bounds only",
        "model_quality_gate": "NOT_RUN",
        "new_paid_calls": 0,
        "cases": rows,
    }
    write(directory / "preflight.json", result)
    return result
