"""Render a bounded body proposal against an exact frozen function frame."""

import difflib

from mgar_v3.domain import DomainError
from mgar_v3.repair.domain import Snapshot


def body_diff(snapshot: Snapshot, body: str) -> str:
    if not body.strip() or len(body.encode()) > 16384 or "\x00" in body or "\r" in body:
        raise DomainError("PATCH_BODY_INVALID", "Provide bounded Go statements without CR/NUL")
    if len(snapshot.editable_paths) != 1:
        raise DomainError("PATCH_BODY_FRAME_DENIED", "Exactly one editable function file required")
    path = snapshot.editable_paths[0]
    source = snapshot.files[path]
    marker = "func PaidRevenueCents(orders []Order) int {"
    # These owned pure-function examples end with this single allowed function.
    # Unsupported layouts fail closed; never find a fuzzy location in another file.
    if source.count(marker) != 1 or not source.endswith("}\n"):
        raise DomainError("PATCH_BODY_FRAME_DENIED", "Frozen function frame is unsupported")
    start = source.index(marker) + len(marker)
    end = source.rfind("\n}")
    if end < start:
        raise DomainError("PATCH_BODY_FRAME_DENIED", "Frozen function body bounds differ")
    replacement = source[:start] + "\n" + body.strip("\n") + source[end:]
    return "".join(
        difflib.unified_diff(
            source.splitlines(keepends=True),
            replacement.splitlines(keepends=True),
            fromfile="a/" + path,
            tofile="b/" + path,
        )
    )
