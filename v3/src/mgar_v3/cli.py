import argparse
import hashlib
import json
import sys
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
import psycopg
from redis import Redis
from redis.exceptions import RedisError

from mgar_v3.preflight import preflight
from mgar_v3.settings import ROOT, Settings


def doctor(settings: Settings) -> int:
    result = {"postgres": "NOT_RUN", "redis": "NOT_RUN", "live": "NOT_RUN"}
    try:
        with psycopg.connect(settings.database_url.get_secret_value(), connect_timeout=3) as conn:
            applied: dict[str, str] = dict(
                conn.execute("SELECT name,sha256 FROM app.schema_migrations").fetchall()
            )
            required = list((ROOT / "migrations").glob("*.sql"))
            result["postgres"] = (
                "PASS"
                if required
                and all(
                    applied.get(p.name) == hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in required
                )
                else "FAIL"
            )
    except psycopg.Error:
        result["postgres"] = "FAIL"
    try:
        with Redis.from_url(
            settings.redis_url.get_secret_value(), socket_connect_timeout=3
        ) as cache:
            result["redis"] = "PASS" if cache.ping() else "FAIL"
    except (OSError, RedisError):
        result["redis"] = "FAIL"
    print(json.dumps(result))
    return 0 if result["postgres"] == result["redis"] == "PASS" else 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mgar")
    parser.add_argument("--api", default="http://127.0.0.1:18080")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor")
    commands.add_parser("commerce-setup")
    commands.add_parser("commerce-list")
    commerce = commands.add_parser("commerce-create")
    commerce.add_argument(
        "--template", choices=["duplicate_paid", "status_filter", "refunded_status"], required=True
    )
    commerce.add_argument("--issue", type=int, required=True)
    commerce.add_argument("--start", required=True)
    commerce.add_argument("--end", required=True)
    commerce.add_argument("--goal", choices=["report", "draft_pr"], default="report")
    commerce.add_argument("--execution-mode", choices=["fixture", "live"], default="fixture")
    commerce.add_argument("--client-request-id")
    for operation in ["commerce-result", "commerce-repair", "commerce-message"]:
        sub = commands.add_parser(operation)
        sub.add_argument("case_id")
        if operation != "commerce-result":
            sub.add_argument("--revision", type=int, required=True)
            sub.add_argument("--client-request-id")
        if operation == "commerce-repair":
            sub.add_argument("--result-hash", required=True)
        if operation == "commerce-message":
            sub.add_argument("--body", required=True)
    commands.add_parser("tickets")
    commands.add_parser("routing")
    commands.add_parser("recovery-requests")
    recovery = commands.add_parser("recovery-decision")
    recovery.add_argument("request_id")
    recovery.add_argument(
        "--decision", choices=["wait", "escalate", "pause", "cancel"], required=True
    )
    ticket = commands.add_parser("ticket")
    ticket.add_argument("ticket_id")
    event = commands.add_parser("event")
    event.add_argument("--issue", type=int, required=True)
    event.add_argument("--body", required=True)
    event.add_argument("--client-request-id")
    event.add_argument("--execution-mode", choices=["fixture", "live"], default="fixture")
    live = commands.add_parser("live-preflight")
    live.add_argument("--profile", type=Path, required=True)
    live.add_argument("--authorization", type=Path)
    run = commands.add_parser("run")
    run.add_argument("--project", default="orders-demo")
    run.add_argument("--question", required=True)
    run.add_argument("--issue", required=True, type=int)
    run.add_argument("--client-request-id", default=None)
    run.add_argument("--execution-mode", choices=["fixture", "live"], default="fixture")
    repair = commands.add_parser("repair")
    repair.add_argument("--project", default="orders-demo")
    repair.add_argument("--issue", type=int, required=True)
    repair.add_argument(
        "--question",
        default="Fix paid revenue: count the first paid order per ID, ignoring other statuses.",
    )
    repair.add_argument("--client-request-id")
    repair.add_argument("--execution-mode", choices=["fixture", "live"], default="fixture")
    for name in [
        "status",
        "report",
        "watch",
        "actions",
        "pause",
        "cancel",
        "resume",
        "approve",
        "message",
        "repair-detail",
        "reject",
        "revise",
        "reconcile",
        "continue-draft",
    ]:
        command = commands.add_parser(name)
        command.add_argument("run_id")
        if name == "message":
            command.add_argument("--body", required=True)
            command.add_argument("--client-request-id", default=None)
        if name == "approve":
            command.add_argument("--action", required=True)
            command.add_argument("--payload-hash", required=True)
        if name in {"reject", "revise", "reconcile", "continue-draft"}:
            command.add_argument("--action", required=True)
        if name in {"reject", "revise", "continue-draft"}:
            command.add_argument("--payload-hash", required=True)
            command.add_argument("--reason", default="")
        if name == "revise":
            command.add_argument("--diff-file", type=Path, required=True)
    args = parser.parse_args(argv)
    settings = Settings()
    if args.command == "doctor":
        return doctor(settings)
    if args.command == "commerce-setup":
        from mgar_v3.commerce.setup import setup
        from mgar_v3.coordination.engine import Engine
        from mgar_v3.evidence.artifacts import ArtifactStore

        engine = Engine(settings, ArtifactStore(settings.artifact_root))
        try:
            print(json.dumps(setup(engine), indent=2))
        finally:
            engine.close()
        return 0
    if args.command == "live-preflight":
        try:
            result = preflight(args.profile, settings, args.authorization)
        except (OSError, ValueError):
            print('{"status":"NOT_RUN","code":"PROFILE_INVALID"}')
            return 2
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0 if result["ready_for_execution"] else 2
    if urlsplit(args.api).hostname not in {"127.0.0.1", "localhost", "::1"}:
        print('{"code":"LOCAL_API_REQUIRED"}', file=sys.stderr)
        return 2
    # The user-facing CLI uses the same API as any future Web client.
    with httpx.Client(
        base_url=args.api,
        headers={"X-MGAR-Token": settings.local_token.get_secret_value()},
        timeout=60,
    ) as client:
        try:
            if args.command == "commerce-list":
                response = client.get("/api/v3/commerce/cases")
            elif args.command == "commerce-create":
                response = client.post(
                    "/api/v3/commerce/cases",
                    json={
                        "template_id": args.template,
                        "issue_number": args.issue,
                        "window": {"start": args.start, "end": args.end},
                        "goal": args.goal,
                        "execution_mode": args.execution_mode,
                        "client_request_id": args.client_request_id or str(uuid4()),
                    },
                )
            elif args.command == "commerce-result":
                response = client.get(f"/api/v3/commerce/cases/{args.case_id}/result")
            elif args.command in {"commerce-repair", "commerce-message"}:
                payload = {
                    "expected_case_revision": args.revision,
                    "client_request_id": args.client_request_id or str(uuid4()),
                }
                payload.update(
                    {"investigation_result_hash": args.result_hash}
                    if args.command == "commerce-repair"
                    else {"body": args.body}
                )
                response = client.post(
                    f"/api/v3/commerce/cases/{args.case_id}/"
                    + ("repair" if args.command == "commerce-repair" else "messages"),
                    json=payload,
                )
            elif args.command == "recovery-requests":
                response = client.get("/api/v3/recovery-requests")
            elif args.command == "recovery-decision":
                response = client.post(
                    "/api/v3/recovery-requests/" + args.request_id + "/decision",
                    json={"decision": args.decision},
                )
            elif args.command in {"tickets", "routing"}:
                response = client.get(
                    "/api/v3/tickets" if args.command == "tickets" else "/api/v3/router/status"
                )
            elif args.command == "ticket":
                response = client.get("/api/v3/tickets/" + args.ticket_id)
            elif args.command == "event":
                response = client.post(
                    "/api/v3/events",
                    json={
                        "source": "cli",
                        "issue_number": args.issue,
                        "body": args.body,
                        "client_request_id": args.client_request_id or str(uuid4()),
                        "execution_mode": args.execution_mode,
                    },
                )
            elif args.command in {"run", "repair"}:
                response = client.post(
                    "/api/v3/repairs" if args.command == "repair" else "/api/v3/runs",
                    json={
                        "project_id": args.project,
                        "question": args.question,
                        "issue_number": args.issue,
                        "client_request_id": args.client_request_id or str(uuid4()),
                        "execution_mode": args.execution_mode,
                    },
                )
            else:
                base = f"/api/v3/runs/{args.run_id}"
                if args.command == "repair-detail":
                    response = client.get(base + "/repair")
                elif args.command in {"reject", "revise", "reconcile", "continue-draft"}:
                    decision = (
                        {}
                        if args.command == "reconcile"
                        else {"payload_hash": args.payload_hash, "reason": args.reason}
                    )
                    if args.command == "revise":
                        decision["diff"] = args.diff_file.read_text()
                    response = client.post(
                        base
                        + "/actions/"
                        + args.action
                        + "/"
                        + ("continue" if args.command == "continue-draft" else args.command),
                        json=decision,
                    )
                elif args.command == "approve":
                    response = client.post(
                        base + "/approvals",
                        json={
                            "action_id": args.action,
                            "payload_hash": args.payload_hash,
                        },
                    )
                elif args.command == "message":
                    response = client.post(
                        base + "/messages",
                        json={
                            "client_request_id": args.client_request_id or str(uuid4()),
                            "body": args.body,
                        },
                    )
                elif args.command in {"pause", "cancel", "resume"}:
                    response = client.post(base + "/" + args.command)
                else:
                    suffix = {
                        "status": "",
                        "report": "/report",
                        "watch": "/events",
                        "actions": "/actions",
                    }[args.command]
                    response = client.get(base + suffix)
            print(response.text)
            return 0 if response.is_success else 2
        except httpx.HTTPError:
            print('{"code":"API_UNAVAILABLE"}', file=sys.stderr)
            return 2


if __name__ == "__main__":
    raise SystemExit(main())
