"""PDF parser subprocess: no inherited credentials, bounded CPU/memory/output."""

import base64
import json
import resource
import sys
from typing import Any

from mgar_v3.domain import DomainError
from mgar_v3.rag.text import parse_untrusted


def main() -> None:
    resource.setrlimit(resource.RLIMIT_CPU, (4, 4))
    resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_NOFILE, (32, 32))
    try:
        content = base64.b64decode(sys.stdin.buffer.read(2_700_001), validate=True)
        value: dict[str, Any] = {
            "chunks": [chunk.model_dump() for chunk in parse_untrusted(content, "application/pdf")]
        }
    except DomainError as exc:
        value = {"error": exc.code}
    print(json.dumps(value, ensure_ascii=False))


if __name__ == "__main__":
    main()
