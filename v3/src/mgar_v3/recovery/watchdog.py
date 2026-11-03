"""Independent limited HTTP observer. Does not import Settings, Engine or model clients."""

import argparse
import json
import stat
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from pydantic import Field, SecretStr, field_validator

from mgar_v3.domain import Model
from mgar_v3.recovery.domain import Snapshot, detect


class SubmissionUnconfirmed(Exception):
    """A request may have committed before its response was received."""


class Configuration(Model):
    api_url: str
    token: SecretStr = Field(min_length=24)
    interval_seconds: int = Field(default=30, ge=5, le=300)

    @field_validator("api_url")
    @classmethod
    def loopback_only(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "localhost"}
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError("Watchdog only connects to the local Engine")
        return value.rstrip("/")


def poll(config: Configuration) -> dict[str, int | bool]:
    with httpx.Client(
        base_url=config.api_url,
        timeout=5,
        follow_redirects=False,
        trust_env=False,
        headers={"X-MGAR-Watchdog-Token": config.token.get_secret_value()},
    ) as client:
        response = client.get("/api/v3/watchdog/snapshot")
        response.raise_for_status()
        snapshot = Snapshot.model_validate(response.json())
        signals = detect(snapshot)
        created = 0
        for item in signals:
            try:
                reply = client.post(
                    "/api/v3/watchdog/recovery-requests", json=item.model_dump(mode="json")
                )
                reply.raise_for_status()
                created += bool(reply.json()["created"])
            except (httpx.HTTPError, ValueError, KeyError) as exc:
                raise SubmissionUnconfirmed from exc
        return {
            "observations": len(snapshot.items),
            "signals": len(signals),
            "created": created,
            "partial": snapshot.truncated,
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    try:
        if stat.S_IMODE(args.config.stat().st_mode) & 0o077:
            raise ValueError("Private config permissions required")
        config = Configuration.model_validate_json(args.config.read_bytes())
    except (OSError, ValueError):
        print(json.dumps({"status": "invalid_private_config"}))
        return 2
    while True:
        try:
            print(json.dumps({"status": "observed", **poll(config)}), flush=True)
        except SubmissionUnconfirmed:
            print(
                json.dumps(
                    {"status": "unavailable", "saved": None, "submission_outcome": "unknown"}
                ),
                flush=True,
            )
            if args.once:
                return 1
        except (httpx.HTTPError, ValueError, KeyError):
            print(json.dumps({"status": "unavailable", "saved": False}), flush=True)
            if args.once:
                return 1
        if args.once:
            return 0
        time.sleep(config.interval_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
