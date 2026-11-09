"""Trusted R03 sandbox bootstrap: stdin tar -> /workspace, then exec a fixed argv.

The host submits only regular files with validated repository-relative paths.
This launcher is baked into the pinned sandbox image; it never evaluates shell
text and never reads anything from the host except its stdin archive.
"""

from __future__ import annotations

import io
import json
import os
import sys
import tarfile
from typing import NoReturn

WORKSPACE = "/workspace"
MAX_ARCHIVE = 2 * 1024 * 1024
MAX_FILES = 64
MAX_FILE = 262_144
MAX_TOTAL = 1_048_576
MARKER = "mgar-sandbox-error:"


def fail(message: str) -> NoReturn:
    print(f"{MARKER} {message}", file=sys.stderr, flush=True)
    raise SystemExit(125)


def safe_name(name: str) -> bool:
    """Accept only visible, relative, forward-slash file paths."""

    if not name or "\\" in name or "\0" in name or name.startswith("/"):
        return False
    parts = name.split("/")
    return all(part not in {"", ".", ".."} and not part.startswith(".") for part in parts)


def extract(payload: bytes) -> int:
    count = 0
    total = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:") as archive:
            for member in archive:
                count += 1
                if count > MAX_FILES or not member.isfile() or not safe_name(member.name):
                    fail("source archive contains an unsupported entry")
                if member.size < 0 or member.size > MAX_FILE:
                    fail("source archive entry exceeds the file bound")
                total += member.size
                if total > MAX_TOTAL:
                    fail("source archive exceeds the total bound")
                target = os.path.join(WORKSPACE, *member.name.split("/"))
                os.makedirs(os.path.dirname(target), mode=0o700, exist_ok=True)
                data = archive.extractfile(member)
                if data is None:
                    fail("source archive entry could not be read")
                with open(target, "xb") as handle:
                    handle.write(data.read())
                os.chmod(target, 0o600)
    except (tarfile.TarError, OSError) as exc:
        fail(f"source archive could not be extracted: {exc}")
    return count


def main() -> int:
    payload = sys.stdin.buffer.read(MAX_ARCHIVE + 1)
    if len(payload) > MAX_ARCHIVE:
        fail("source archive exceeds the sandbox bound")
    if extract(payload) == 0:
        fail("source archive is empty")
    command = list(sys.argv[1:])
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        fail("sandbox command is missing")
    print(json.dumps({"event": "sandbox", "bootstrap": "ok"}), flush=True)
    try:
        os.execv(command[0], command)
    except OSError as exc:
        fail(f"sandbox command could not start: {exc}")
    return 125


if __name__ == "__main__":
    raise SystemExit(main())
