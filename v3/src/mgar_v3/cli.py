import argparse
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
from mgar_v3.settings import Settings


def doctor(settings: Settings) -> int:
    result = {"postgres": "NOT_RUN", "redis": "NOT_RUN", "live": "NOT_RUN"}
    try:
        with psycopg.connect(settings.database_url.get_secret_value(), connect_timeout=3) as conn:
            row = conn.execute("SELECT count(*) FROM app.schema_migrations").fetchone()
            result["postgres"] = "PASS" if row and row[0] >= 5 else "FAIL"
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
    live = commands.add_parser("live-preflight")
    live.add_argument("--profile", type=Path, required=True)
    live.add_argument("--authorization", type=Path)
    run = commands.add_parser("run")
    run.add_argument("--project", default="orders-demo")
    run.add_argument("--question", required=True)
    run.add_argument("--issue", required=True, type=int)
    run.add_argument("--client-request-id", default=None)
    run.add_argument("--execution-mode", choices=["fixture", "live"], default="fixture")
    for name in ["status", "report", "watch", "actions", "cancel", "resume", "approve"]:
        command = commands.add_parser(name)
        command.add_argument("run_id")
        if name == "approve":
            command.add_argument("--action", required=True)
            command.add_argument("--payload-hash", required=True)
    args = parser.parse_args(argv)
    settings = Settings()
    if args.command == "doctor":
        return doctor(settings)
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
        timeout=15,
    ) as client:
        try:
            if args.command == "run":
                response = client.post(
                    "/api/v3/runs",
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
                if args.command == "approve":
                    response = client.post(
                        base + "/approvals",
                        json={
                            "action_id": args.action,
                            "payload_hash": args.payload_hash,
                        },
                    )
                elif args.command in {"cancel", "resume"}:
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
