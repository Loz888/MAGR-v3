import hashlib
import json
import os
import random
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from mgar_v3.benchmark.domain import EvaluationAuthorization
from mgar_v3.coordination.engine import Engine
from mgar_v3.coordination_eval.candidates import SYSTEM
from mgar_v3.coordination_eval.domain import ROUTER_SYSTEM, WORKER_SYSTEM
from mgar_v3.coordination_eval.provider import Slots, write_json
from mgar_v3.coordination_eval.runtime import Coordinator
from mgar_v3.coordination_eval.store import DATA, Journal
from mgar_v3.domain import DomainError, digest
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.live import LiveProfile
from mgar_v3.settings import ROOT, Settings

PROTECTED = [
    "eval/reports/final-v6-results.json",
    "eval/reports/final-v6-answers.jsonl",
    "eval/reports/final-v6-summary.json",
    "eval/reports/final-v6-failure-analysis.json",
    "docs/repairs/R6-20261103/v6-residual-reproductions.json",
    "docs/repairs/R6-20261103/issue-register.json",
]
PRICE_PATH = ROOT / "docs/repairs/R6-HOST-20261105/v2-current-official-pricing.json"


def known_response_hash(engine: Engine, call: dict[str, Any]) -> str:
    with engine.transaction() as conn:
        response = conn.execute(
            "SELECT safe_payload FROM app.run_events WHERE run_id=%s "
            "AND kind='model_response_recorded' AND safe_payload->>'call_id'=%s",
            (call["run_id"], str(call["call_id"])),
        ).fetchone()
    if not response:
        raise DomainError("MODEL_RESPONSE_MISSING", "Native receipt required")
    content_hash = str(response["safe_payload"]["content_hash"])
    if hashlib.sha256(engine.artifacts.read(content_hash)).hexdigest() != content_hash:
        raise DomainError("MODEL_RESPONSE_CHANGED", "Response CAS hash mismatch")
    return content_hash


def reviewed_price() -> dict[str, Any]:
    value = json.loads(PRICE_PATH.read_text())
    reviewed = datetime.fromisoformat(value["checked_at"])
    age = datetime.now(UTC) - reviewed
    if (
        value.get("status") != "PASS"
        or value.get("api_model") != "deepseek-flash"
        or age < timedelta(0)
        or age > timedelta(days=7)
        or value["usd_peak_per_million"]["cache_miss"] != "0.3"
        or value["usd_peak_per_million"]["output"] != "1.2"
    ):
        raise DomainError("COORD_PRICE_REVIEW_REQUIRED", "Current reviewed upper price required")
    return cast(dict[str, Any], value)


def hashes() -> dict[str, str]:
    paths = [p for p in (ROOT / "src").rglob("*.py")]
    paths += [ROOT / "scripts/coordination_eval.py"]
    paths += [p for p in DATA.iterdir() if p.is_file()]
    return {
        str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(paths)
    }


def configuration(directory: Path, trial: dict[str, Any], *, paid: bool) -> Settings:
    return Settings(
        project_id=trial["project_id"],
        artifact_root=directory.resolve() / "artifacts",
        graph_version="r2-mailbox-v1",
        router_mode="disabled",
        allow_paid=paid,
        allow_external_write=False,
        max_concurrent_runs=2,
    )


def manifest_for(
    name: str, source_ref: str, cfg: Settings, prior: dict[str, int], *, repair: bool = False
) -> dict[str, Any]:
    rng = random.Random(20261105)
    prefix = ("coord-v2-" if repair else "coord-v1-") + digest(name)[:8]
    trials: list[dict[str, Any]] = []
    for repetition in range(2):
        for cache in ("cold", "warm"):
            arms = ["E", "F"] if repair else ["D", "E", "F"]
            rng.shuffle(arms)
            for arm in arms:
                index = len(trials)
                trial_id = f"{prefix}-r{repetition}-{cache}-{arm}"
                trials.append(
                    {
                        "id": trial_id,
                        "arm": arm,
                        "repetition": repetition,
                        "cache": cache,
                        "project_id": "coord-eval-" + digest(trial_id)[:16],
                        "issue_base": (2000000 + index * 100) if repair else 1900000 + index * 20,
                        "fault_after_known": arm == "F" and repetition == 0 and cache == "cold",
                    }
                )
    events = json.loads((DATA / "events.json").read_text())["events"]
    profiles = []
    for trial in trials:
        trial["routing_protocol"] = "candidate-v2" if repair else "free-plan-v1"
        router_slots = [
            f"router:{i}:a{a}" if repair else f"router:{i}"
            for i in range({"D": 0, "E": 8, "F": 3}[trial["arm"]])
            for a in range(2 if repair else 1)
        ]
        slots = ["worker:e01", "worker:e02", *router_slots]
        for slot in slots:
            is_worker = slot.startswith("worker:")
            event: dict[str, Any] = next((e for e in events if "worker:" + e["key"] == slot), {})
            if is_worker:
                assert event is not None
            ordinal = int(slot.split(":")[1]) if not is_worker else int(event["issue"])
            offset = (10 + ordinal * 2 + int(slot[-1])) if repair else 10 + ordinal
            case_id = f"r6-eval:coord:{trial['id']}:{slot}"
            p = LiveProfile(
                case_id=case_id,
                project_id=trial["project_id"],
                repository=cfg.repository.lower(),
                issue_number=trial["issue_base"] + (ordinal if is_worker else offset),
                actor=cfg.local_actor,
                question=event["body"].removeprefix("/mgar investigate ")
                if is_worker
                else "Produce a bounded coordination decision for the frozen event batch.",
                source_ref=source_ref,
                source_path="v3/eval/coordination-v1/source.md",
                expected_source_hash=hashlib.sha256((DATA / "source.md").read_bytes()).hexdigest(),
                model_id="deepseek-flash",
                price_version="DeepSeek-V4.1-Flash-1017-peak",
                input_micros_per_million=300000,
                output_micros_per_million=1200000,
                context_tokens=8192,
                max_output_tokens=512,
                max_model_calls=1,
                max_cost_micros=3072,
            )
            profiles.append(
                {
                    "case_id": case_id,
                    "arm": trial["arm"],
                    "trial_id": trial["id"],
                    "slot": slot,
                    "profile": p.model_dump(mode="json"),
                    "profile_hash": p.profile_hash,
                }
            )
    assert len(profiles) == (104 if repair else 68)
    return {
        "schema": "coordination-comparison-v2" if repair else "coordination-comparison-v1",
        "routing_protocol": "candidate-v2" if repair else "free-plan-v1",
        "arms": ["E", "F"] if repair else ["D", "E", "F"],
        "max_router_attempts": 2 if repair else 1,
        "source_ref": source_ref,
        "repository": cfg.repository.lower(),
        "actor": cfg.local_actor,
        "created_at": datetime.now(UTC).isoformat(),
        "trials": trials,
        "profiles": profiles,
        "max_new_calls": 68,
        "max_new_cost_micros": 68 * 3072,
        "prior_cost_micros": prior["cost_micros"],
        "prior_finance": prior,
        "source_hashes": hashes(),
        "protected_business_hashes": {
            p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in PROTECTED
        },
        "prompts": {"router": SYSTEM if repair else ROUTER_SYSTEM, "worker": WORKER_SYSTEM},
        "pricing_evidence": {
            "path": str(PRICE_PATH.relative_to(ROOT)),
            "sha256": hashlib.sha256(PRICE_PATH.read_bytes()).hexdigest(),
            "observation": reviewed_price(),
            "accounting": (
                "Conservative frozen peak cache-miss upper bound, not exact provider bill"
            ),
        },
        "model_destination": "https://api.deepseek.com/v1/chat/completions",
        "outbound_data": (
            "Frozen synthetic event text, synthetic source, own test Run UUIDs; "
            "no credentials, billing, private code or oracle/operator answers."
        ),
        "external_business_publication": False,
        "worker": "same real LangGraph R2, two concurrent Workers, one model call per task",
        "coordination_adapter": (
            "experiment-only admission router; original Engine/Mailbox/Runner/StructuredModel"
        ),
        "manual_time": "D excludes human decision time; frozen operator table supplies decisions",
        "cache_scope": (
            "local fixed Worker source Redis cache only; PG and provider cache uncontrolled"
        ),
        "random_seed": 20261105,
        "repetitions": 2,
        "continuation": (
            "One planned SIGKILL after F first known Router response, before delivery; "
            "explicit known-only result recovery with zero resend of that request"
        ),
        "acceptance": {
            "minimum_completed_trials": len(trials),
            "maximum_lost_accepted_events": 0,
            "maximum_forbidden_effects": 0,
            "maximum_stale_write_successes": 0,
            "minimum_business_success_rate": None,
            "note": (
                "Complete execution/report is distinct from measured routing and Worker success; "
                "preserve every failure."
            ),
        },
    }


def freeze(directory: Path, source_ref: str, *, repair: bool = False) -> dict[str, Any]:
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    cfg = Settings(allow_paid=False, allow_external_write=False)
    engine = Engine(cfg, ArtifactStore(cfg.artifact_root))
    try:
        with engine.transaction() as conn:
            prior = {k: int(v) for k, v in engine.benchmarks.cost(conn, cfg.repository).items()}
    finally:
        engine.close()
    if prior["unknown"]:
        raise DomainError("USAGE_UNKNOWN", "Reconcile existing repository costs before freezing")
    manifest = manifest_for(directory.name, source_ref, cfg, prior, repair=repair)
    if prior["cost_micros"] + manifest["max_new_cost_micros"] > 2000000:
        raise DomainError("BUDGET_EXHAUSTED", "Original repository USD2 hard cap remains")
    write_json(directory / "manifest.json", manifest)
    write_json(
        directory / "authorization.proposed.json",
        {
            "manifest_hash": digest(manifest),
            "approved_by": cfg.local_actor,
            "expires_at": (datetime.now(UTC) + timedelta(days=2)).isoformat(),
            "max_new_calls": manifest["max_new_calls"],
            "max_new_cost_micros": manifest["max_new_cost_micros"],
            "allow_paid": False,
            "allow_external_write": False,
        },
    )
    return manifest


def verify(directory: Path) -> dict[str, Any]:
    manifest = json.loads((directory / "manifest.json").read_text())
    if manifest["source_hashes"] != hashes():
        raise DomainError("COORD_FROZEN_SOURCE_CHANGED", "Use a new frozen version")
    reviewed_price()
    if (
        manifest["pricing_evidence"]["sha256"]
        != hashlib.sha256(PRICE_PATH.read_bytes()).hexdigest()
    ):
        raise DomainError("COORD_PRICE_CHANGED", "Frozen pricing observation changed")
    for p, h in manifest["protected_business_hashes"].items():
        if hashlib.sha256((ROOT / p).read_bytes()).hexdigest() != h:
            raise DomainError("COORD_OLD_RESULT_CHANGED", "Original business outcomes must stay")
    repair = manifest.get("routing_protocol") == "candidate-v2"
    if len(manifest["profiles"]) != (104 if repair else 68) or len(manifest["trials"]) != (
        8 if repair else 12
    ):
        raise DomainError("COORD_MATRIX_CHANGED", "The frozen paired experiment matrix changed")
    for item in manifest["profiles"]:
        p = LiveProfile.model_validate(item["profile"])
        if p.profile_hash != item["profile_hash"] or p.max_model_calls != 1:
            raise DomainError("COORD_PROFILE_CHANGED", "Profile or call limit changed")
    if (
        manifest["max_new_cost_micros"] != 68 * 3072
        or manifest["max_new_calls"] != 68
        or any(p["profile"]["max_cost_micros"] != 3072 for p in manifest["profiles"])
    ):
        raise DomainError("COORD_BUDGET_CHANGED", "Shared cap differs from frozen single calls")
    return cast(dict[str, Any], manifest)


def child(
    directory: Path, authorization: Path, trial_id: str, *, paid: bool, resume: bool
) -> dict[str, Any]:
    if not paid:
        raise DomainError("EXPLICIT_PAID_REQUIRED", "Use reviewed grant and --execute-paid", 403)
    manifest = verify(directory)
    auth = EvaluationAuthorization.model_validate_json(authorization.read_bytes())
    trial = next(t for t in manifest["trials"] if t["id"] == trial_id)
    output = directory / (trial_id + ".json")
    if output.exists():
        raise DomainError("COORD_TRIAL_ALREADY_EXECUTED", "Never replace a recorded trial")
    cfg = configuration(directory, trial, paid=True)
    engine = Engine(cfg, ArtifactStore(cfg.artifact_root))
    journal = None
    try:
        engine.benchmarks.authorize(manifest, auth)
        journal = Journal(engine, trial, digest(manifest))
        result = Coordinator(journal, Slots(journal, manifest, auth), resume=resume).execute()
        write_json(output, result)
        return result
    except BaseException as exc:
        write_json(
            directory / (trial_id + ("-resume" if resume else "") + "-failure.json"),
            {
                "error_type": type(exc).__name__,
                "reason": str(exc),
                "partial": journal.export() if journal else None,
            },
        )
        raise
    finally:
        engine.close()


def execute(directory: Path, authorization: Path, *, paid: bool) -> dict[str, Any]:
    if not paid:
        raise DomainError("EXPLICIT_PAID_REQUIRED", "No generation without --execute-paid", 403)
    manifest = verify(directory)
    auth = EvaluationAuthorization.model_validate_json(authorization.read_bytes())
    marker = directory / "execution.json"
    if marker.exists():
        raise DomainError(
            "COORD_ALREADY_STARTED", "Review original process logs; never restart all"
        )
    cfg = configuration(directory, manifest["trials"][0], paid=True)
    engine = Engine(cfg, ArtifactStore(cfg.artifact_root))
    try:
        engine.benchmarks.authorize(manifest, auth)
    finally:
        engine.close()
    state: dict[str, Any] = {"manifest_hash": digest(manifest), "processes": []}
    write_json(marker, state)
    for trial in manifest["trials"]:
        argv = [
            sys.executable,
            str(ROOT / "scripts/coordination_eval.py"),
            "child",
            "--directory",
            str(directory.resolve()),
            "--authorization",
            str(authorization.resolve()),
            "--trial",
            trial["id"],
            "--execute-paid",
        ]
        with (directory / (trial["id"] + ".log")).open("xb") as log:
            os.fchmod(log.fileno(), 0o600)
            proc = subprocess.Popen(
                argv, stdout=log, stderr=subprocess.STDOUT, cwd=ROOT, start_new_session=True
            )
            try:
                code = proc.wait(timeout=900)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, 9)
                proc.wait()
                raise
        state["processes"].append({"trial": trial["id"], "pid": proc.pid, "exit": code})
        write_json(marker, state)
        if code == -9 and trial["fault_after_known"]:
            # Source/manifest still frozen; child permits only the existing known typed result.
            verify(directory)
            cfg = configuration(directory, trial, paid=True)
            gate = Engine(cfg, ArtifactStore(cfg.artifact_root))
            try:
                with gate.transaction() as conn:
                    killed = conn.execute(
                        "SELECT details FROM coordination_eval.audit WHERE trial_id=%s "
                        "AND kind='known_router_killpoint'",
                        (trial["id"],),
                    ).fetchone()
                    if not killed:
                        raise DomainError("COORD_UNPLANNED_CRASH", "No planned known-result cut")
                    known = conn.execute(
                        "SELECT * FROM app.usage_calls WHERE run_id=%s AND status='known' "
                        "AND result IS NOT NULL",
                        (killed["details"]["run_id"],),
                    ).fetchone()
                    if not known:
                        raise DomainError("USAGE_UNKNOWN", "No known response; cannot continue")
                response_hash = known_response_hash(gate, known)
                write_json(
                    directory / (trial["id"] + "-continuation-reviewed.json"),
                    {
                        "manifest_hash": digest(manifest),
                        "planned_exit": -9,
                        "original_call_id": str(known["call_id"]),
                        "original_response_hash": response_hash,
                        "original_settled_micros": known["settled_micros"],
                        "replay_only_for_original_request": True,
                        "remaining_frozen_trial_calls_still_count_toward_original_grant": True,
                    },
                )
            finally:
                gate.close()
            with (directory / (trial["id"] + "-known-continuation.log")).open("xb") as log:
                os.fchmod(log.fileno(), 0o600)
                continuation = subprocess.Popen(
                    [*argv, "--resume-known"],
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    cwd=ROOT,
                    start_new_session=True,
                )
                try:
                    code = continuation.wait(timeout=900)
                except subprocess.TimeoutExpired:
                    os.killpg(continuation.pid, 9)
                    continuation.wait()
                    raise
            state["processes"].append(
                {
                    "trial": trial["id"],
                    "pid": continuation.pid,
                    "exit": code,
                    "continuation": "planned_known_result_only",
                }
            )
            write_json(marker, state)
        if code != 0:
            state["status"] = "STOPPED_REVIEW_REQUIRED"
            write_json(marker, state)
            raise DomainError("COORD_CHILD_FAILED", "Retain partial result; no automatic rerun")
        print(json.dumps({"trial": trial["id"], "execution": "RECORDED"}), flush=True)
    state["status"] = "EXECUTED"
    write_json(marker, state)
    return state
