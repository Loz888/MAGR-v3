"""Prepare separate private observer/server configuration; never overwrite existing credentials."""

import argparse
import json
import os
import secrets
from pathlib import Path

from mgar_v3.recovery.watchdog import Configuration


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--directory", type=Path, default=Path(__file__).resolve().parents[1] / ".local/watchdog"
    )
    parser.add_argument("--api-url", default="http://127.0.0.1:18080")
    args = parser.parse_args()
    token = secrets.token_urlsafe(32)
    config = Configuration(api_url=args.api_url, token=token)
    args.directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    client, server = args.directory / "client.json", args.directory / "server.env"
    if client.exists() or server.exists():
        raise SystemExit("Existing watchdog configuration retained; choose another directory")
    contents = [
        (
            client,
            json.dumps(
                {
                    "api_url": config.api_url,
                    "token": token,
                    "interval_seconds": config.interval_seconds,
                },
                indent=2,
            )
            + "\n",
        ),
        (server, "MGAR_V3_WATCHDOG_TOKEN=" + token + "\n"),
    ]
    for path, value in contents:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as output:
            output.write(value)
    print(json.dumps({"client_config": str(client), "server_environment": str(server)}))


if __name__ == "__main__":
    main()
