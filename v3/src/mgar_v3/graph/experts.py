from typing import TYPE_CHECKING, Annotated, Any, TypedDict, cast
from uuid import UUID

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send
from psycopg.types.json import Jsonb

from mgar_v3.agents.domain import Assignment, Finding, InvestigationPlan, merge_findings
from mgar_v3.agents.experts import ExpertProvider, FixtureExperts
from mgar_v3.agents.verify import render, verify_findings
from mgar_v3.domain import DomainError, digest
from mgar_v3.graph.checkpoints import checkpoint_saver
from mgar_v3.mcp.r4 import TaskTools

if TYPE_CHECKING:
    from mgar_v3.graph.runner import Runner


class ExpertState(TypedDict, total=False):
    question: str
    snapshot_id: str
    input_revision: int
    plan_revision: int
    plan: dict[str, Any]
    round_no: int
    joined_round: int
    findings: Annotated[dict[str, Any], merge_findings]
    assignment: dict[str, Any]
    verification: dict[str, Any]
    resolution: dict[str, Any]
    verified_round: int


def execute_experts(
    owner: "Runner", run_id: UUID, provider: ExpertProvider | None = None
) -> UUID | None:
    engine = owner.engine
    run = engine.begin_run(run_id)
    epoch = run["execution_epoch"]
    owner.execution_epoch = epoch
    selected: ExpertProvider = provider or FixtureExperts()
    if run["execution_mode"] == "live" and provider is None:
        from mgar_v3.agents.experts import DeepSeekExperts
        from mgar_v3.live import load_permit

        settings = engine.settings
        if not settings.live_profile_path or not settings.live_authorization_path:
            raise DomainError(
                "R4_LIVE_PROFILE_REQUIRED",
                "A reviewed R4 source and budget profile is required",
                403,
            )
        permit = load_permit(settings.live_profile_path, settings.live_authorization_path, settings)
        selected = DeepSeekExperts(engine, permit)
        context = engine.tools.bind_context(run_id, epoch, run["input_revision"])
        profile = permit.profile
        if (
            context["knowledge_snapshot_ids"] != profile.knowledge_snapshot_ids
            or digest(engine.knowledge.config()) != profile.rag_config_hash
            or engine.settings.rag_mode != "local"
            or engine.tools.reader(context["profile"]["actor"]).profile["fixture_hash"]
            != profile.data_fixture_hash
        ):
            raise DomainError(
                "R4_SOURCE_PROFILE_CHANGED",
                "R4 source/model versions differ from the frozen profile",
                403,
            )
    if selected.mode != run["execution_mode"]:
        raise DomainError(
            "R4_LIVE_PROFILE_REQUIRED",
            "R4 requires its own expert provider and reviewed call budget",
        )

    def intake(state: ExpertState) -> ExpertState:
        snapshot = engine.prepare_intake(run_id, epoch)
        return {
            "question": snapshot["question"],
            "snapshot_id": str(snapshot["snapshot_id"]),
            "input_revision": snapshot["input_revision"],
        }

    def acknowledge(state: ExpertState) -> ExpertState:
        snapshot = engine.acknowledge_intake(run_id, epoch, UUID(state["snapshot_id"]))
        return {"question": snapshot["question"], "input_revision": snapshot["input_revision"]}

    def route(state: ExpertState) -> str:
        if state.get("plan_revision") != state["input_revision"]:
            return "plan"
        if state.get("joined_round", -1) < state["round_no"]:
            return "dispatch"
        if state.get("verified_round", -1) < state["round_no"]:
            return "verify"
        return "finish"

    def plan(state: ExpertState) -> ExpertState:
        context = engine.tools.bind_context(run_id, epoch, state["input_revision"])
        tasks = [
            Assignment(
                task_id=f"r{state['input_revision']}:0:{role}",
                role=role,
                question=state["question"],
                round_no=0,
                input_revision=state["input_revision"],
                snapshot_ids=[UUID(s) for s in context["knowledge_snapshot_ids"]],
            )
            for role in ("knowledge", "repo", "data")
        ]
        value = InvestigationPlan(
            question=state["question"], tasks=tasks, required_roles=["knowledge", "repo", "data"]
        )
        with engine.transaction() as conn:
            engine._run(conn, run_id, epoch)
            engine._event(
                conn,
                run_id,
                "expert_plan",
                input_revision=state["input_revision"],
                tasks=[t.task_id for t in tasks],
                plan_hash=digest(value.model_dump(mode="json")),
            )
        return {
            "plan": value.model_dump(mode="json"),
            "plan_revision": state["input_revision"],
            "round_no": 0,
            "joined_round": -1,
            "verified_round": -1,
        }

    def dispatch(state: ExpertState) -> list[Send]:
        return [Send("expert", {"assignment": task}) for task in state["plan"]["tasks"]]

    def expert(state: ExpertState) -> ExpertState:
        task = Assignment.model_validate(state["assignment"])
        with engine.transaction() as conn:
            engine._run(conn, run_id, epoch)
            existing = conn.execute(
                "SELECT result FROM app.expert_results WHERE run_id=%s AND task_id=%s",
                (run_id, task.task_id),
            ).fetchone()
        if existing:
            return {"findings": {task.task_id: existing["result"]}}
        try:
            token = engine.tools.issue(run_id, epoch, task.task_id, task.role, task.input_revision)
            finding = selected.investigate(task, TaskTools(engine, token, task.role))
            if finding.task_id != task.task_id or finding.role != task.role:
                raise DomainError("EXPERT_IDENTITY_MISMATCH", "Expert cannot change its assignment")
        except DomainError as exc:
            if exc.code == "FENCED":
                raise
            finding = Finding(
                task_id=task.task_id,
                role=task.role,
                status="missing",
                error_code=exc.code,
                open_questions=[task.role + ": " + exc.code],
            )
        except Exception:
            finding = Finding(
                task_id=task.task_id,
                role=task.role,
                status="failed",
                error_code="EXPERT_FAILED",
                open_questions=[task.role + ": expert read did not complete"],
            )
        value = finding.model_dump(mode="json")
        with engine.transaction() as conn:
            current = engine._run(conn, run_id, epoch)
            if current["status"] != "running":
                raise DomainError("FENCED", "Late expert result was rejected")
            conn.execute(
                "INSERT INTO app.expert_results(run_id,task_id,input_revision,role,result) "
                "VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (run_id, task.task_id, task.input_revision, task.role, Jsonb(value)),
            )
            engine._event(
                conn,
                run_id,
                "expert_completed",
                task_id=task.task_id,
                role=task.role,
                status=finding.status,
            )
        return {"findings": {task.task_id: value}}

    def collect(state: ExpertState) -> ExpertState:
        if not all(t["task_id"] in state["findings"] for t in state["plan"]["tasks"]):
            raise DomainError(
                "EXPERT_JOIN_INCOMPLETE", "Every selected branch must terminate before verification"
            )
        with engine.transaction() as conn:
            engine._run(conn, run_id, epoch)
            engine._event(conn, run_id, "expert_joined", round_no=state["round_no"])
        return {"joined_round": state["round_no"]}

    def current_findings(state: ExpertState) -> list[Finding]:
        # Latest observation per role, never a result from a previous input revision.
        values = [
            Finding.model_validate(v)
            for k, v in state["findings"].items()
            if k.startswith(f"r{state['input_revision']}:")
        ]
        latest = {v.role: v for v in sorted(values, key=lambda v: int(v.task_id.split(":")[1]))}
        return list(latest.values())

    def verify(state: ExpertState) -> ExpertState:
        resolution, verdict = verify_findings(
            engine, run_id, current_findings(state), state["plan"]["required_roles"]
        )
        return {
            "resolution": resolution.model_dump(mode="json"),
            "verification": verdict.model_dump(mode="json"),
            "verified_round": state["round_no"],
        }

    def repair_route(state: ExpertState) -> str:
        return (
            "supplement"
            if (
                state["verification"]["passed"]
                and not state["resolution"]["complete"]
                and state["round_no"] < 2
            )
            else "intake"
        )

    def supplement(state: ExpertState) -> ExpertState:
        value = InvestigationPlan.model_validate(state["plan"])
        round_no = state["round_no"] + 1
        completed = set(state["resolution"]["completed_roles"])
        tasks = [
            task.model_copy(
                update={
                    "task_id": f"r{state['input_revision']}:{round_no}:{task.role}",
                    "round_no": round_no,
                }
            )
            for task in value.tasks
            if task.role not in completed
        ]
        # Conflicting versions need reviewed input, not repeated reads.
        if not tasks:
            return {"verified_round": round_no, "joined_round": round_no, "round_no": round_no}
        return {
            "plan": value.model_copy(update={"tasks": tasks}).model_dump(mode="json"),
            "round_no": round_no,
        }

    builder = StateGraph(ExpertState)
    for name, fn in [
        ("intake", intake),
        ("ack", acknowledge),
        ("plan", plan),
        ("expert", expert),
        ("collect", collect),
        ("verify", verify),
        ("supplement", supplement),
    ]:
        builder.add_node(name, fn)
    builder.add_node("dispatch", lambda state: {})
    builder.add_edge(START, "intake")
    builder.add_edge("intake", "ack")
    builder.add_conditional_edges(
        "ack", route, {"plan": "plan", "dispatch": "dispatch", "verify": "verify", "finish": END}
    )
    builder.add_edge("plan", "dispatch")
    builder.add_conditional_edges("dispatch", dispatch, ["expert"])
    builder.add_edge("expert", "collect")
    builder.add_edge("collect", "intake")
    builder.add_conditional_edges(
        "verify", repair_route, {"supplement": "supplement", "intake": "intake"}
    )
    builder.add_edge("supplement", "intake")
    with checkpoint_saver(engine.settings.database_url.get_secret_value()) as saver:
        graph = builder.compile(checkpointer=saver)
        config: RunnableConfig = {
            "configurable": {"thread_id": str(run_id)},
            "recursion_limit": 96,
            "max_concurrency": 3,
        }
        old = graph.get_state(config)
        initial = None if old.next else old.values or {"question": run["question"], "findings": {}}
        state = cast(
            ExpertState, graph.invoke(cast(ExpertState | None, initial), config, durability="sync")
        )
    # Recheck every source after recovery and immediately before the business commit.
    resolution, verdict = verify_findings(
        engine, run_id, current_findings(state), state["plan"]["required_roles"]
    )
    context = engine.tools.bind_context(run_id, epoch, state["input_revision"])
    report = render(
        resolution,
        verdict,
        state["input_revision"],
        {
            "experts": selected.mode,
            "knowledge": context["profile"]["knowledge_modes"],
            "tools": "mcp_http" if engine.settings.r4_tool_base_url else "direct",
            "data": "local_readonly_postgresql",
        },
    )
    content_hash = engine.artifacts.put(report)
    try:
        with engine.transaction() as conn:
            current = engine._run(conn, run_id, epoch)
            if current["status"] != "running":
                raise DomainError("FENCED", "Execution stopped before report commit")
            if current["thread_revision"] != state["input_revision"]:
                raise DomainError("STALE_INPUT", "New input requires replanning")
            conn.execute(
                (
                    "INSERT INTO app.artifacts(content_hash,byte_count) VALUES (%s,%s) ON "
                    "CONFLICT DO NOTHING"
                ),
                (content_hash, len(report)),
            )
            conn.execute(
                (
                    "INSERT INTO "
                    "app.resolutions(run_id,input_revision,report_hash,payload,verdict"
                    ") VALUES (%s,%s,%s,%s,%s) ON CONFLICT(run_id,input_revision) DO "
                    "UPDATE SET "
                    "report_hash=excluded.report_hash,payload=excluded.payload,verdict"
                    "=excluded.verdict"
                ),
                (
                    run_id,
                    state["input_revision"],
                    content_hash,
                    Jsonb(resolution.model_dump(mode="json")),
                    Jsonb(verdict.model_dump(mode="json")),
                ),
            )
            if not verdict.passed or resolution.complete:
                return engine.commit_report(
                    run_id, epoch, report, verdict, stored_report_hash=content_hash
                )
            conn.execute(
                (
                    "UPDATE app.runs SET "
                    "status='waiting_input',report_hash=%s,reason_code='R4_EVIDENCE_IN"
                    "COMPLETE' WHERE run_id=%s"
                ),
                (content_hash, run_id),
            )
            engine._event(conn, run_id, "expert_waiting_input", rounds=state["round_no"])
            return None
    except DomainError as exc:
        if exc.code != "STALE_INPUT":
            raise
        engine.requeue_input(run_id, epoch)
        return None
