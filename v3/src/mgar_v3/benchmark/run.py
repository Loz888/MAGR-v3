import ast
import copy
import hashlib
import json
import time
import zipfile
from pathlib import Path
from typing import Any

from mgar_v3.benchmark.domain import Case, EvaluationAuthorization
from mgar_v3.benchmark.executor import Executor
from mgar_v3.benchmark.metrics import summarize
from mgar_v3.benchmark.oracle import cross_expected, grade, hidden_suite
from mgar_v3.benchmark.prepare import DATA, DATASETS, configuration, fingerprint, write
from mgar_v3.coordination.engine import Engine
from mgar_v3.domain import DomainError, Verification, digest
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.live import Authorization, LivePermit, LiveProfile
from mgar_v3.proactivity.domain import Activation, Policy, ProactiveEvent
from mgar_v3.recovery.heartbeat import worker_heartbeat
from mgar_v3.repair.sandbox import Sandbox
from mgar_v3.repair.source import working_files
from mgar_v3.settings import ROOT

SUPERVISION_CHANGES = {
    "src/mgar_v3/benchmark/run.py",
    "src/mgar_v3/benchmark/metrics.py",
    "scripts/final_eval.py",
    "scripts/final_eval_supervisor.py",
    "config/verification.json",
    "src/mgar_v3/benchmark/reconcile.py",
    "scripts/reconcile_final_usage.py",
}
FROZEN_FOLDERS = ("src", "migrations", "scripts", "config", "eval/final-v1", "eval/final-v3")


def same_model_protocol(original: bytes, revised: bytes) -> bool:
    """Allow only local client setup and I/O accounting changes."""
    old, new = ast.parse(original), ast.parse(revised)

    def assignment(tree: ast.Module, name: str) -> ast.Assign | None:
        found = [
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == name for t in n.targets)
        ]
        return found[0] if len(found) == 1 else None

    old_client, new_client = assignment(old, "client"), assignment(new, "client")
    old_reserve, new_reserve = assignment(old, "call_id"), assignment(new, "call_id")
    if any(n is None for n in (old_client, new_client, old_reserve, new_reserve)):
        return False
    assert old_client and new_client and old_reserve and new_reserve
    if (
        ast.dump(old_client) != ast.dump(new_client)
        or ast.dump(old_reserve) != ast.dump(new_reserve)
        or new_client.lineno >= new_reserve.lineno
    ):
        return False
    template = ast.parse(
        "try:\n pass\nexcept (OSError, httpx.HTTPError):\n "
        'raise DomainError("MODEL_CLIENT_UNAVAILABLE", '
        '"Model client setup failed before paid reservation") from None'
    ).body[0]
    assert isinstance(template, ast.Try)
    setup = [
        n
        for n in ast.walk(new)
        if isinstance(n, ast.Try)
        and n.body == [new_client]
        and not n.orelse
        and not n.finalbody
        and len(n.handlers) == 1
        and ast.dump(n.handlers[0]) == ast.dump(template.handlers[0])
    ]
    if len(setup) != 1 or not any(
        isinstance(n, ast.Try) and n.body[0] is new_reserve for n in ast.walk(new)
    ):
        return False
    setup_dump = ast.dump(setup[0])

    class Normalize(ast.NodeTransformer):
        def visit_Assign(self, node: ast.Assign) -> ast.AST | None:
            if any(isinstance(t, ast.Name) and t.id in {"client", "call_id"} for t in node.targets):
                return None
            return self.generic_visit(node)

        def visit_Try(self, node: ast.Try) -> ast.AST | None:
            if ast.dump(node) == setup_dump:
                return None
            return self.generic_visit(node)

        def visit_ExceptHandler(self, node: ast.ExceptHandler) -> ast.AST:
            if isinstance(node.type, ast.Tuple) and [ast.unparse(e) for e in node.type.elts] == [
                "httpx.HTTPError",
                "ValueError",
                "OSError",
            ]:
                node.type.elts = node.type.elts[:-1]
            return self.generic_visit(node)

    return ast.dump(Normalize().visit(copy.deepcopy(old))) == ast.dump(
        Normalize().visit(copy.deepcopy(new))
    )


def verify_continuation(directory: Path, manifest: dict[str, Any], receipt: Path) -> None:
    approval = json.loads(receipt.read_text())
    archive = directory / "baseline-source.zip"
    if (
        approval["manifest_hash"] != digest(manifest)
        or approval["new_implementation_sha256"] != fingerprint()
        or approval["baseline_archive_sha256"] != hashlib.sha256(archive.read_bytes()).hexdigest()
        or approval["scope"]
        not in {
            "supervision_only_no_reruns_no_new_grant",
            "supervision_and_billing_only_no_reruns_no_new_grant",
        }
    ):
        raise DomainError("BENCHMARK_CHANGED", "Continuation receipt does not match")
    with zipfile.ZipFile(archive) as source:
        old = {n: source.read(n) for n in source.namelist()}
        if len(old) != len(source.namelist()):
            raise DomainError("BENCHMARK_CHANGED", "Duplicate archived source paths")
    original = hashlib.sha256()
    current = {}
    for folder in FROZEN_FOLDERS:
        for name in sorted(n for n in old if n.startswith(folder + "/")):
            original.update(name.encode() + b"\0" + old[name] + b"\0")
        for path in sorted((ROOT / folder).rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts:
                current[str(path.relative_to(ROOT))] = path.read_bytes()
    changed = {n for n in set(old) | set(current) if old.get(n) != current.get(n)}
    model_path = "src/mgar_v3/models/structured.py"
    allowed = set(SUPERVISION_CHANGES)
    if model_path in changed:
        if approval[
            "scope"
        ] != "supervision_and_billing_only_no_reruns_no_new_grant" or not same_model_protocol(
            old[model_path], current[model_path]
        ):
            raise DomainError("BENCHMARK_CHANGED", "Frozen generation protocol changed")
        allowed.add(model_path)
    if (
        original.hexdigest() != manifest["implementation_sha256"]
        or not changed <= allowed
        or sorted(changed) != approval["changed_paths"]
    ):
        raise DomainError("BENCHMARK_CHANGED", "Model, evidence, oracle or task source changed")
    records = {
        r["case_id"] + ":" + r["arm"]: r
        for r in json.loads((directory / "results.json").read_text())
    }
    if any(
        key not in records or digest(records[key]) != value
        for key, value in approval.get("retained_results", {}).items()
    ):
        raise DomainError("BENCHMARK_STATE_CHANGED", "Previously completed result changed")


def resume_state(
    directory: Path, manifest: dict[str, Any]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    records = json.loads((directory / "results.json").read_text())
    state = json.loads((directory / "execution.json").read_text())

    def keys(rows: list[dict[str, Any]]) -> list[tuple[str, str, str]]:
        return [(r["case_id"], r["arm"], r["category"]) for r in rows]

    if (
        keys(records) != keys(manifest["assignments"])
        or state["manifest_hash"] != digest(manifest)
        or any(r["status"] not in {"PASS", "FAIL", "NOT_RUN"} for r in records)
    ):
        raise DomainError("BENCHMARK_STATE_CHANGED", "Unknown or changed assignments cannot resume")
    sealed = state.get("sealed_results", {})
    for record in records:
        key = record["case_id"] + ":" + record["arm"]
        if key in sealed and digest(record) != sealed[key]:
            raise DomainError("BENCHMARK_STATE_CHANGED", "Completed result changed")
    return records, state


def retire_interrupted(engine: Engine, record: dict[str, Any], run_id: Any) -> None:
    """Fail a previously started assignment; never rerun its paid or model steps."""
    with engine.transaction() as conn:
        engine._run(conn, run_id)
        usage = conn.execute(
            "SELECT count(*) AS model_calls,coalesce(sum(settled_micros),0) AS cost_micros,"
            "count(*) FILTER(WHERE status!='known') AS unknown "
            "FROM app.usage_calls WHERE run_id=%s",
            (run_id,),
        ).fetchone()
        policy = conn.execute(
            "SELECT policy_hash FROM app.speculative_runs WHERE run_id=%s", (run_id,)
        ).fetchone()
        writes = conn.execute(
            "SELECT count(*) AS n FROM app.actions WHERE run_id=%s", (run_id,)
        ).fetchone()
        assert usage is not None and writes is not None
    if usage["unknown"]:
        engine.recover()
        raise DomainError("USAGE_UNKNOWN", "Interrupted paid call needs reconciliation")
    engine.cancel(run_id)
    if policy:
        engine.proactivity.activate(
            Activation(policy_hash=policy["policy_hash"], mode="revoked"),
            engine.settings.local_actor,
        )
    record.update(
        status="FAIL",
        reason="PROCESS_INTERRUPTED",
        infrastructure_failure=True,
        model_calls=int(usage["model_calls"]),
        cost_micros=int(usage["cost_micros"]),
        reserved_micros=0,
        seconds=0,
        latency_unknown=True,
        forbidden_actions=int(writes["n"]),
        write_invariant_checked=True,
    )


def verify(directory: Path, continuation: Path | None = None) -> dict[str, Any]:
    manifest: dict[str, Any] = json.loads((directory / "manifest.json").read_text())
    if manifest["implementation_sha256"] != fingerprint():
        if continuation is None:
            raise DomainError("BENCHMARK_CHANGED", "Frozen implementation changed")
        verify_continuation(directory, manifest, continuation)
    cases = [Case.model_validate(c) for c in manifest["cases"]]
    dataset = DATASETS.get(manifest.get("dataset", "final-v1"))
    if dataset is None:
        raise DomainError("BENCHMARK_CHANGED", "Unknown public dataset revision")
    if (
        manifest.get("dataset_cases_sha256")
        and hashlib.sha256((dataset / "cases.json").read_bytes()).hexdigest()
        != manifest["dataset_cases_sha256"]
    ):
        raise DomainError("BENCHMARK_CHANGED", "Public request contracts changed")
    public_cases = [
        Case.model_validate(c).model_dump(mode="json")
        for c in json.loads((dataset / "cases.json").read_text())
    ]
    if manifest["cases"] != public_cases or manifest["quality_thresholds"] != {
        "C_total_min": 24,
        "normal_min": 20,
        "negative_required": 5,
        "citation_validity": 1,
    }:
        raise DomainError("BENCHMARK_CHANGED", "Frozen cases or acceptance thresholds changed")
    expected = {(c.id, a) for c in cases for a in "ABC"}
    if (
        len(cases) != 30
        or len(expected) != 90
        or len(manifest["profiles"]) != 90
        or (
            {(r["case_id"], r["arm"]) for r in manifest["assignments"]} != expected
            or len(manifest["assignments"]) != 90
        )
    ):
        raise DomainError("BENCHMARK_CHANGED", "The frozen 30-case/90-assignment matrix changed")
    identities = set()
    if {(p["case_id"], p["arm"]) for p in manifest["profiles"]} != expected:
        raise DomainError("BENCHMARK_CHANGED", "Every case requires exactly one profile per arm")
    for item in manifest["profiles"]:
        profile = LiveProfile.model_validate(item["profile"])
        if profile.profile_hash != item["profile_hash"] or profile.max_model_calls != 3:
            raise DomainError("BENCHMARK_CHANGED", "Frozen profile or per-task cap changed")
        if profile.max_cost_micros != 3 * profile.reservation_micros:
            raise DomainError("BENCHMARK_CHANGED", "Per-task reservation cap changed")
        identities.add(
            (
                profile.model_id,
                profile.price_version,
                profile.context_tokens,
                profile.max_output_tokens,
                profile.input_micros_per_million,
                profile.output_micros_per_million,
            )
        )
    if len(identities) != 1 or manifest["max_new_calls"] != 270:
        raise DomainError(
            "BENCHMARK_CHANGED", "Arms must share one frozen model/price/context bound"
        )
    if (
        sum(p["profile"]["max_cost_micros"] for p in manifest["profiles"])
        != (manifest["max_new_cost_micros"])
    ):
        raise DomainError("BENCHMARK_CHANGED", "Shared cost cap differs from task reservations")
    for path, key in [
        (directory / "inputs.json", "inputs_sha256"),
        (DATA / "oracles.json", "oracle_sha256"),
    ]:
        if hashlib.sha256(path.read_bytes()).hexdigest() != manifest[key]:
            raise DomainError("BENCHMARK_CHANGED", "Frozen inputs or oracle changed")
    return manifest


def execute(
    directory: Path,
    authorization: Path,
    *,
    execute_paid: bool = False,
    resume: bool = False,
    one_assignment: bool = False,
    continuation: Path | None = None,
) -> dict[str, Any]:
    if not execute_paid:
        raise DomainError("EXPLICIT_PAID_REQUIRED", "Final evaluation requires --execute-paid", 403)
    manifest = verify(directory, continuation)
    auth = EvaluationAuthorization.model_validate_json(authorization.read_bytes())
    if (directory / "execution.json").exists() and not resume:
        raise DomainError(
            "BENCHMARK_ALREADY_STARTED",
            "Inspect existing attempts; never silently rerun a paid assignment",
        )
    inputs = json.loads((directory / "inputs.json").read_text())
    criteria = json.loads((DATA / "oracles.json").read_text())
    results = [dict(r) for r in manifest["assignments"]]
    state: dict[str, Any] = {"manifest_hash": digest(manifest), "runs": {}, "started": True}
    if resume:
        if continuation is None:
            raise DomainError("CONTINUATION_REQUIRED", "Explicit reviewed continuation required")
        results, state = resume_state(directory, manifest)
    first = LiveProfile.model_validate(manifest["profiles"][0]["profile"])
    cfg = configuration(directory, first.project_id, paid=True)
    gate = Engine(cfg, ArtifactStore(cfg.artifact_root))
    try:
        budget = gate.benchmarks.authorize(manifest, auth)
        with gate.transaction() as conn:
            uncertain = conn.execute(
                "SELECT 1 FROM app.usage_calls WHERE final_budget_hash=%s "
                "AND status!='known' LIMIT 1",
                (budget,),
            ).fetchone()
        if uncertain:
            gate.recover()
            raise DomainError("USAGE_UNKNOWN", "Existing final-evaluation budget is uncertain")
    finally:
        gate.close()
    write(directory / "execution.json", state)
    for record in results:
        if record["status"] != "NOT_RUN":
            continue
        case = Case.model_validate(
            next(c for c in manifest["cases"] if c["id"] == record["case_id"])
        )
        frozen = next(
            p for p in manifest["profiles"] if p["case_id"] == case.id and p["arm"] == record["arm"]
        )
        profile = LiveProfile.model_validate(frozen["profile"])
        cfg = configuration(directory, profile.project_id, paid=True)
        engine = Engine(cfg, ArtifactStore(cfg.artifact_root))
        policy = None
        run_id = None
        started = time.monotonic()
        try:
            prior = state["runs"].get(case.id + ":" + record["arm"])
            if prior:
                from uuid import UUID

                retire_interrupted(engine, record, UUID(prior))
                continue
            engine.recover()
            engine.benchmarks.authorize(manifest, auth)
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
            policy = Policy(
                repository=cfg.repository,
                allowed_actors=[cfg.local_actor],
                kinds=["pr_comment"],
                source_ref=str(profile.source_ref),
                question=profile.question,
                expires_at=auth.expires_at,
                execution_mode="live",
                model_profile_hash=profile.profile_hash,
                max_model_calls=3,
                max_cost_micros=profile.max_cost_micros,
                max_reads=16,
            )
            engine.proactivity.create(policy, cfg.local_actor, permit)
            engine.proactivity.activate(
                Activation(policy_hash=policy.policy_hash, mode="auto"), cfg.local_actor, permit
            )
            request = ProactiveEvent(
                external_event_id=digest([digest(manifest), case.id, record["arm"]]),
                repository=cfg.repository,
                actor=cfg.local_actor,
                kind="pr_comment",
                issue_number=1,
                source_ref=str(profile.source_ref),
            )
            run_id = engine.proactivity.observe(request, permit)[0]["run_id"]
            engine.benchmarks.bind(run_id, digest(manifest), case.id, record["arm"])
            state["runs"][case.id + ":" + record["arm"]] = str(run_id)
            write(directory / "execution.json", state)
            run = engine.begin_run(run_id)
            epoch = run["execution_epoch"]
            # Warm local models consistently before timed inference/tool execution.
            engine.knowledge.backend()
            started = time.monotonic()
            executor = Executor(
                engine, permit, run_id, epoch, case, record["arm"], inputs, manifest["ablation"]
            )

            def epoch_value(value: int = epoch) -> int:
                return value

            with worker_heartbeat(engine.recovery, run_id, epoch_value):
                answer, sources, stats = executor.execute()
                expected = criteria.get(case.id, {"status": "supported", "values": {}})
                if case.category == "cross":
                    expected = {"status": "supported", "values": cross_expected(case.rows or [])}
                if case.category == "repair" and executor.final_files is not None:
                    assert executor.final_snapshot is not None and case.repair_variant is not None
                    files = working_files(executor.final_snapshot, executor.final_files)
                    proof, raw = Sandbox(str(run_id)).run(
                        {**files, "zz_r6_hidden_test.go": hidden_suite(case.repair_variant)},
                        [*executor.final_snapshot.expected_tests, "TestR6Hidden"],
                    )
                    stats["repair_passed"] = proof["passed"]
                    executor.repair_receipts.append(
                        {
                            "stage": "independent_hidden",
                            "proof": proof,
                            "log_hash": engine.artifacts.put(raw),
                        }
                    )
                assessment = grade(
                    answer.model_dump(mode="json"),
                    expected,
                    [s.model_dump(mode="json") for s in sources],
                    case.required_roles,
                    stats["repair_passed"],
                )
                if case.negative_condition == "revoked" and any(
                    s.role == "knowledge" for s in sources
                ):
                    assessment["passed"] = False
                    assessment["reasons"].append("revoked_source_leaked")
                report = {
                    "answer": answer.model_dump(mode="json"),
                    "assessment": assessment,
                    "stats": stats,
                    "sources": [s.model_dump(mode="json") for s in sources],
                    "repair": executor.repair_receipts,
                }
                write(directory / "outputs" / (case.id + "-" + record["arm"] + ".json"), report)
                engine.commit_report(
                    run_id,
                    epoch,
                    json.dumps(report, ensure_ascii=False).encode(),
                    Verification(
                        passed=assessment["passed"],
                        checks=["independent frozen oracle", "exact quotations"],
                        reasons=assessment["reasons"],
                    ),
                )
                record.update(
                    status="PASS" if assessment["passed"] else "FAIL",
                    **assessment,
                    tool_calls=stats["tool_calls"],
                    recall_at5=stats.get("recall_at5"),
                )
        except DomainError as exc:
            record.update(
                status="UNKNOWN" if exc.code == "USAGE_UNKNOWN" else "FAIL", reason=exc.code
            )
            if run_id:
                engine.fail_run(run_id, exc.code)
            if exc.code in {"BENCHMARK_INPUT_CHANGED", "BENCHMARK_CHANGED"}:
                record["stop_trial"] = True
        except (OSError, ValueError, KeyError) as exc:
            record.update(status="FAIL", reason=type(exc).__name__, stop_trial=True)
            if run_id:
                engine.fail_run(run_id, "BENCHMARK_INFRASTRUCTURE_FAILED")
        finally:
            record["seconds"] = round(time.monotonic() - started, 4)
            if run_id:
                with engine.transaction() as conn:
                    usage = conn.execute(
                        "SELECT coalesce(sum(settled_micros),0) AS cost_micros,"
                        "coalesce(sum(reserved_micros) FILTER(WHERE status!='known'),"
                        "0) AS reserved_micros,"
                        "count(*) AS model_calls,"
                        "coalesce(sum((usage->>'prompt_tokens')::bigint),0) AS prompt_tokens,"
                        "coalesce(sum((usage->>'completion_tokens')::bigint),0) "
                        "AS completion_tokens "
                        "FROM app.usage_calls WHERE run_id=%s",
                        (run_id,),
                    ).fetchone()
                    assert usage is not None
                    record.update({k: int(v) for k, v in usage.items()})
                    writes = conn.execute(
                        "SELECT count(*) AS n FROM app.actions WHERE run_id=%s", (run_id,)
                    ).fetchone()
                    assert writes is not None
                    record.update(forbidden_actions=int(writes["n"]), write_invariant_checked=True)
                    if record["reserved_micros"]:
                        record["status"] = "UNKNOWN"
            if policy is not None:
                engine.proactivity.activate(
                    Activation(policy_hash=policy.policy_hash, mode="revoked"), cfg.local_actor
                )
            engine.close()
            state["sealed_results"] = {
                r["case_id"] + ":" + r["arm"]: digest(r)
                for r in results
                if r["status"] != "NOT_RUN"
            }
            write(directory / "execution.json", state)
            write(directory / "results.json", results)
        if one_assignment:
            break
        if record["status"] == "UNKNOWN" or record.get("stop_trial"):
            break
    summary = summarize(results)
    write(directory / "summary.json", summary)
    return summary
