"""Bounded operator experiment: real Router, frozen synthetic events, no publication.

Prepare freezes the oracle before a model call. The separately supplied authorization
must bind that exact context and the shared, already-used R1 budget. model-crash
intentionally exits with SIGKILL after durable model results/proposals. recover never
calls a model and cancels the fixture test Runs after checking the frozen oracle.
"""

import argparse
import hashlib
import json
import os
import signal
from pathlib import Path
from uuid import UUID

from mgar_v3.coordination.engine import Engine
from mgar_v3.domain import TriggerInput
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.routing.live import RouterProfile, load_router_permit
from mgar_v3.routing.model import DeepSeekRouter
from mgar_v3.routing.worker import RouterWorker
from mgar_v3.settings import ROOT, Settings


def main() -> None:
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("phase", choices=["prepare", "model-crash", "recover"])
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--authorization", type=Path)
    parser.add_argument("--profile", type=Path, default=ROOT / "config/router-live-proposed.json")
    parser.add_argument("--execute-paid", action="store_true")
    args = parser.parse_args()
    args.directory.mkdir(parents=True, exist_ok=True)
    manifest = args.directory / "experiment.json"
    data = json.loads(manifest.read_text()) if manifest.exists() else {}
    profile = RouterProfile.model_validate_json(args.profile.read_bytes())
    settings = Settings(
        allow_paid=args.phase == "model-crash" and args.execute_paid,
        allow_external_write=False,
        graph_version="r2-mailbox-v1",
    )
    engine = Engine(settings, ArtifactStore(settings.artifact_root))

    def save() -> None:
        manifest.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str) + "\n")

    try:
        if args.phase == "prepare":
            if manifest.exists():
                raise RuntimeError(
                    "An existing experiment must be recovered, never silently repeated"
                )
            with engine.transaction() as conn:
                budget = conn.execute(
                    "SELECT * FROM app.authorization_budgets WHERE profile_hash=%s",
                    (profile.budget_profile_hash,),
                ).fetchone()
                assert (
                    budget is not None
                    and budget["call_limit"] == 3
                    and budget["limit_micros"] == 2000000
                )
                assert budget["used_calls"] == 2 and budget["reserved_micros"] == 0
            prefix = "r3-20261102:" + profile.profile_hash[:12]
            source = TriggerInput(
                source="cli",
                external_event_id=prefix + ":source",
                project_id=profile.project_id,
                repository=profile.repository,
                issue_number=1000301,
                actor=profile.actor,
                body="/mgar investigate [R3 synthetic] 订单服务重复计数问题：核对原文证据。",
            )
            original = engine.ingest(source)
            assert original.route == "created" and not original.duplicate
            data.update(
                profile_hash=profile.profile_hash,
                budget_profile_hash=profile.budget_profile_hash,
                source_run_id=str(original.run_id),
                source_ticket_id=str(original.ticket_id),
                prior_used_calls=budget["used_calls"],
                prior_cost_micros=budget["settled_micros"],
            )
            save()
            events = [
                source.model_copy(
                    update={
                        "external_event_id": prefix + ":related",
                        "issue_number": 1000302,
                        "body": (
                            "@mgar [R3 synthetic] 请创建独立调查工单，"
                            "核对订单服务重复计数的原文证据。"
                            f"仅把工单 {original.ticket_id} 作为关联背景，不要修改或暂停该工单。"
                        ),
                    }
                ),
                source.model_copy(
                    update={
                        "external_event_id": prefix + ":clarify",
                        "issue_number": 1000303,
                        "body": "@mgar 帮我处理一下",
                    }
                ),
            ]
            receipts = [engine.ingest(event) for event in events]
            assert all(r.route == "unrouted" and not r.duplicate for r in receipts)
            batch = engine.routing.prepare("live")
            assert batch is not None
            assert {str(r.thread_id) for r in receipts} == {
                g["thread_id"] for g in batch["snapshot"]["threads"]
            }
            oracle = {
                "context_hash": batch["context_hash"],
                "expected": {
                    str(receipts[0].thread_id): {
                        "action": "create_ticket",
                        "related_ticket_ids": [str(original.ticket_id)],
                        "message_code": None,
                    },
                    str(receipts[1].thread_id): {
                        "action": "request_clarification",
                        "related_ticket_ids": [],
                        "message_code": "clarification",
                    },
                },
            }
            oracle_path = args.directory / "oracle.json"
            oracle_path.write_text(json.dumps(oracle, ensure_ascii=False, indent=2) + "\n")
            data.update(
                batch_id=str(batch["batch_id"]),
                context_hash=batch["context_hash"],
                oracle_sha256=hashlib.sha256(oracle_path.read_bytes()).hexdigest(),
                events=[e.model_dump(mode="json") for e in events],
                components={
                    "postgres": "real",
                    "router_model": "real_deepseek",
                    "worker_events": "synthetic",
                    "workers": "fixture_not_executed",
                    "external_writes": "none",
                },
            )
            save()
        elif args.phase == "model-crash":
            if not args.execute_paid or args.authorization is None:
                raise RuntimeError("Exact reviewed authorization and --execute-paid are required")
            permit = load_router_permit(args.profile, args.authorization, settings)
            permit.validate(data["context_hash"])
            original_save = engine.routing.save

            def crash_after_save(batch_id, decisions):
                assert str(batch_id) == data["batch_id"]
                original_save(batch_id, decisions)
                data["crash_point"] = (
                    "known_model_result_and_decisions_durable_before_business_commit"
                )
                save()
                os.kill(os.getpid(), signal.SIGKILL)

            engine.routing.save = crash_after_save
            RouterWorker(engine, DeepSeekRouter(engine, permit)).tick()
            raise RuntimeError(
                "Model phase did not reach its crash point; inspect the preserved ledger"
            )
        else:
            engine.recover()

            class NoModel:
                mode = "live"

                def propose(self, batch):
                    raise RuntimeError("Recovery is forbidden to make a new model call")

            worker = RouterWorker(engine, NoModel())
            resumed = worker.tick()
            assert str(resumed) == data["batch_id"], "Expected the saved batch to resume"
            oracle_path = args.directory / "oracle.json"
            assert hashlib.sha256(oracle_path.read_bytes()).hexdigest() == data["oracle_sha256"]
            oracle = json.loads(oracle_path.read_text())
            with engine.transaction() as conn:
                batch = conn.execute(
                    "SELECT * FROM app.router_batches WHERE batch_id=%s", (resumed,)
                ).fetchone()
                decisions = conn.execute(
                    "SELECT * FROM app.router_decisions WHERE batch_id=%s", (resumed,)
                ).fetchall()
                call = conn.execute(
                    "SELECT * FROM app.router_calls WHERE batch_id=%s", (resumed,)
                ).fetchone()
                budget = conn.execute(
                    "SELECT * FROM app.authorization_budgets WHERE profile_hash=%s",
                    (profile.budget_profile_hash,),
                ).fetchone()
                assert (
                    batch["status"] == "applied"
                    and len(decisions) == 2
                    and call["status"] == "known"
                )
                assert (
                    budget["used_calls"] == 3
                    and budget["reserved_micros"] == 0
                    and budget["settled_micros"] <= 2000000
                )
            for decision in decisions:
                payload = decision["payload"]
                expected = oracle["expected"][payload["thread_id"]]
                assert {k: payload[k] for k in expected} == expected
                assert payload["target_id"] is None
            before = engine.routing.metrics()["physical_router_calls"]
            assert all(
                engine.ingest(TriggerInput.model_validate(e)).duplicate for e in data["events"]
            )
            assert engine.routing.metrics()["physical_router_calls"] == before
            for run_id in [
                UUID(data["source_run_id"]),
                *[UUID(d["result"]["run_id"]) for d in decisions],
            ]:
                assert engine.get_run(run_id)["execution_mode"] == "fixture"
                assert all(a["status"] == "proposed" for a in engine.actions(run_id))
                engine.cancel(run_id)
            response = json.loads(engine.artifacts.read(call["response_hash"]))
            data.update(
                status="PASS",
                oracle_cases_passed=2,
                router_physical_calls=1,
                total_physical_calls=budget["used_calls"],
                total_conservative_cost_micros=budget["settled_micros"],
                router_conservative_cost_micros=call["settled_micros"],
                usage=call["usage"],
                provider_receipt={"id": response.get("id"), "model": response.get("model")},
                response_hash=call["response_hash"],
                duplicate_delivery_passed=True,
                recovery_without_additional_call=True,
                external_writes=0,
                fixture_test_runs_cancelled_after_verification=True,
            )
            save()
        print(json.dumps(data, ensure_ascii=False, default=str))
    finally:
        engine.close()


if __name__ == "__main__":
    main()
