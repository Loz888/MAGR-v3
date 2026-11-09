"""V3.3 M04 context compaction with safety invariants.

The repository-analysis loop used to trim its prompt by deleting the oldest
history entries until the encoded payload fit.  That silently discarded
permissions, failed attempts and open questions.  This module replaces blind
trimming with compaction: repeated observations are merged, long tool outputs
become bounded segment references, older observations collapse into a
structured summary, and every safety-relevant fact is carried forward and
verified before the prompt is used.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from pydantic import BaseModel, Field

from mgar_v3.domain import DomainError, Model
from mgar_v3.evidence.artifacts import ArtifactStore

TokenCounter = Callable[[Any], int]

_SEGMENT_CHARS = 400
_PREVIEW_LADDER = (900, 500, 240, 120)
_ERROR_CODE = re.compile(r"^([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+):")


def estimate_tokens(value: Any) -> int:
    """Conservative character-based estimate; three bytes per token."""

    if isinstance(value, str):
        text = value
    elif isinstance(value, BaseModel):
        encoded = value.model_dump(mode="json")
        text = json.dumps(encoded, ensure_ascii=False, separators=(",", ":"))
    else:
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    return max(1, len(text.encode()) // 3)


class LineRange(Model):
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)


class SegmentRef(Model):
    """A pointer to a long source segment instead of its full text."""

    reference: str
    locator: LineRange | None = None
    artifact_hash: str | None = None
    total_chars: int = Field(ge=0)


class Observation(Model):
    """One tool observation or decision note in the execution history."""

    tool: str
    reference: str = ""
    content: str = ""
    evidence_id: str | None = None
    source_version: str | None = None
    truncated: bool = False
    error_code: str | None = None
    artifact_hash: str | None = None
    locator: LineRange | None = None
    segment: SegmentRef | None = None

    @classmethod
    def from_mapping(cls, item: Mapping[str, Any]) -> Observation:
        raw_locator = item.get("locator")
        locator: LineRange | None = None
        if isinstance(raw_locator, Mapping):
            locator = LineRange.model_validate(dict(raw_locator))
        evidence_id = item.get("evidence_id")
        source_version = item.get("source_version")
        error_code = item.get("error_code")
        artifact_hash = item.get("artifact_hash")
        return cls(
            tool=str(item.get("tool") or ""),
            reference=str(item.get("reference") or ""),
            content=str(item.get("content") or ""),
            evidence_id=str(evidence_id) if evidence_id else None,
            source_version=str(source_version) if source_version else None,
            truncated=bool(item.get("truncated")),
            error_code=str(error_code) if error_code else None,
            artifact_hash=str(artifact_hash) if artifact_hash else None,
            locator=locator,
        )

    @property
    def digest(self) -> str:
        """Identity of the observation content, ignoring its evidence grant."""

        encoded = json.dumps(
            {
                "tool": self.tool,
                "reference": self.reference,
                "content": self.content,
                "error_code": self.error_code,
            },
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
        return hashlib.sha256(encoded).hexdigest()


class PermissionSnapshot(Model):
    """The exact authority under which a model call is made."""

    project_id: str
    repository: str
    commit: str
    actor: str
    read_only: bool = True
    allowed_tools: list[str] = Field(default_factory=list)
    budgets: dict[str, int] = Field(default_factory=dict)

    @property
    def digest(self) -> str:
        encoded = json.dumps(
            self.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        ).encode()
        return hashlib.sha256(encoded).hexdigest()

    def escalations(self, base: PermissionSnapshot) -> list[str]:
        """Return every way this snapshot is broader than ``base``."""

        problems: list[str] = []
        for name in ("project_id", "repository", "commit", "actor"):
            if getattr(self, name) != getattr(base, name):
                problems.append(f"permission_scope_changed:{name}")
        if base.read_only and not self.read_only:
            problems.append("permission_escalation:read_only")
        for tool in sorted(set(self.allowed_tools) - set(base.allowed_tools)):
            problems.append(f"permission_escalation:tool:{tool}")
        for name, value in sorted(self.budgets.items()):
            if name not in base.budgets:
                problems.append(f"permission_escalation:budget:{name}")
            elif value > base.budgets[name]:
                problems.append(f"permission_escalation:budget:{name}")
        return problems


class ContextInvariants(Model):
    """Everything compaction must never weaken or forget."""

    permissions: PermissionSnapshot
    available_tools: list[str] = Field(default_factory=list)
    failed_attempts: list[str] = Field(default_factory=list)
    pending_approvals: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    artifact_hashes: dict[str, str] = Field(default_factory=dict)


class CompactionSummary(Model):
    """Structured replacement for the observations that were folded away."""

    observations_total: int
    observations_kept: int
    observations_deduplicated: int
    observations_compressed: int
    observations_omitted: int
    failed_attempts: list[str] = Field(default_factory=list)
    pending_approvals: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    artifact_hashes: dict[str, str] = Field(default_factory=dict)
    permission_digest: str = ""


class CompactionResult(Model):
    observations: list[Observation] = Field(default_factory=list)
    summary: CompactionSummary
    tokens: int = 0


class ArtifactPage(Model):
    content_hash: str
    offset: int
    end_offset: int
    total_bytes: int
    text: str
    continuation: str | None = None
    previous: str | None = None
    full_read: bool = False


def failure_signature(observation: Observation) -> str | None:
    """A stable description of a failed attempt, or ``None`` for a success."""

    if observation.error_code:
        label = observation.error_code
    else:
        match = _ERROR_CODE.match(observation.content.strip())
        if match is None:
            if observation.tool != "tool_error":
                return None
            label = "tool_error"
        else:
            label = match.group(1)
    where = f"{observation.tool} {observation.reference}".strip()
    return f"{where}: {label}" if where else label


def merge_failed_attempts(
    explicit: Sequence[str], observations: Sequence[Observation]
) -> list[str]:
    values = [*explicit]
    for observation in observations:
        signature = failure_signature(observation)
        if signature is not None:
            values.append(signature)
    return list(dict.fromkeys(values))


def compress_observation(
    observation: Observation, max_chars: int = _SEGMENT_CHARS
) -> tuple[Observation, bool]:
    """Replace long content with a preview plus a segment reference."""

    if max_chars < 32:
        raise DomainError("CONTEXT_BOUND", "Segment preview must be at least 32 characters", 400)
    if len(observation.content) <= max_chars:
        return observation, False
    total = (
        observation.segment.total_chars
        if observation.segment is not None
        else len(observation.content)
    )
    where = observation.reference or observation.tool
    if observation.locator is not None:
        where += f" lines {observation.locator.start_line}-{observation.locator.end_line}"
    marker = (
        f"\n…({total} characters total from {where}; request that exact segment again "
        "for the full text)"
    )
    segment = SegmentRef(
        reference=observation.reference,
        locator=observation.locator,
        artifact_hash=observation.artifact_hash,
        total_chars=total,
    )
    compacted = observation.model_copy(
        update={
            "content": observation.content[:max_chars] + marker,
            "truncated": True,
            "segment": segment,
        }
    )
    return compacted, True


def _deduplicate(
    observations: Sequence[Observation],
) -> tuple[list[Observation], list[Observation]]:
    last: dict[str, int] = {}
    for index, observation in enumerate(observations):
        last[observation.digest] = index
    kept: list[Observation] = []
    duplicates: list[Observation] = []
    for index, observation in enumerate(observations):
        if last[observation.digest] != index:
            duplicates.append(observation)
        else:
            kept.append(observation)
    return kept, duplicates


def _size(observations: Sequence[Observation], counter: TokenCounter) -> int:
    return counter([item.model_dump(mode="json") for item in observations])


def compact_observations(
    observations: Sequence[Observation],
    *,
    token_budget: int,
    recent_window: int = 4,
    counter: TokenCounter = estimate_tokens,
    failed_attempts: Sequence[str] = (),
    pending_approvals: Sequence[str] = (),
    open_questions: Sequence[str] = (),
    permissions: PermissionSnapshot | None = None,
) -> CompactionResult:
    """Compact history into a bounded window plus a lossless safety summary."""

    if token_budget < 32:
        raise DomainError("CONTEXT_BOUND", "Observation budget must be at least 32 tokens", 400)
    if not 1 <= recent_window <= 32:
        raise DomainError("CONTEXT_BOUND", "Recent window must be 1 to 32 observations", 400)
    total = len(observations)
    kept, duplicates = _deduplicate(observations)
    split = max(0, len(kept) - recent_window)
    older, recent = kept[:split], kept[split:]
    compacted: list[Observation] = []
    compressed = 0
    for observation in older:
        item, changed = compress_observation(observation)
        compacted.append(item)
        compressed += int(changed)
    omitted = 0
    while compacted and _size([*compacted, *recent], counter) > token_budget:
        compacted.pop(0)
        omitted += 1
    for preview_chars in _PREVIEW_LADDER:
        if _size([*compacted, *recent], counter) <= token_budget:
            break
        recent = [compress_observation(item, preview_chars)[0] for item in recent]
    while len(recent) > 1 and _size([*compacted, *recent], counter) > token_budget:
        recent.pop(0)
        omitted += 1
    if recent and _size([*compacted, *recent], counter) > token_budget:
        recent[-1] = recent[-1].model_copy(update={"content": "", "truncated": True})
    compressed += sum(1 for item in recent if item.segment is not None)
    result = [*compacted, *recent]
    summary = CompactionSummary(
        observations_total=total,
        observations_kept=len(result),
        observations_deduplicated=len(duplicates),
        observations_compressed=compressed,
        observations_omitted=omitted,
        failed_attempts=merge_failed_attempts(failed_attempts, observations),
        pending_approvals=list(dict.fromkeys(pending_approvals)),
        open_questions=list(dict.fromkeys(open_questions)),
        evidence_ids=sorted(
            {item.evidence_id for item in observations if item.evidence_id}
        ),
        artifact_hashes={
            item.artifact_hash: item.artifact_hash
            for item in observations
            if item.artifact_hash
        },
        permission_digest=permissions.digest if permissions is not None else "",
    )
    return CompactionResult(
        observations=result, summary=summary, tokens=_size(result, counter)
    )


def _missing(expected: Sequence[str], actual: Sequence[str]) -> list[str]:
    expected_counts = Counter(expected)
    actual_counts = Counter(actual)
    return sorted(
        value
        for value, count in expected_counts.items()
        if actual_counts[value] < count
    )


def diff_invariants(before: ContextInvariants, after: ContextInvariants) -> list[str]:
    """Return every safety property that compaction weakened or dropped."""

    problems = list(after.permissions.escalations(before.permissions))
    for tool in sorted(set(after.available_tools) - set(before.available_tools)):
        problems.append(f"tool_escalation:{tool}")
    for value in _missing(before.failed_attempts, after.failed_attempts):
        problems.append(f"missing_failed_attempt:{value}")
    for value in _missing(before.pending_approvals, after.pending_approvals):
        problems.append(f"missing_pending_approval:{value}")
    for value in _missing(before.open_questions, after.open_questions):
        problems.append(f"missing_open_question:{value}")
    for value in _missing(before.evidence_ids, after.evidence_ids):
        problems.append(f"missing_evidence:{value}")
    for content_hash, previous in sorted(before.artifact_hashes.items()):
        current = after.artifact_hashes.get(content_hash)
        if current is None:
            problems.append(f"missing_artifact:{content_hash}")
        elif current != previous:
            problems.append(f"artifact_hash_changed:{content_hash}")
    return problems


def validate_compaction(before: ContextInvariants, after: ContextInvariants) -> None:
    """Fail closed when a compacted context loses authority or safety facts."""

    problems = diff_invariants(before, after)
    if problems:
        raise DomainError(
            "CONTEXT_INVARIANT_VIOLATED",
            "Compaction changed protected context: " + "; ".join(problems),
            409,
        )


def verify_artifacts(store: ArtifactStore, content_hashes: Iterable[str]) -> dict[str, str]:
    """Re-read every referenced artifact so its bytes still match its hash."""

    verified: dict[str, str] = {}
    for content_hash in sorted(set(content_hashes)):
        store.read(content_hash)
        verified[content_hash] = content_hash
    return verified


def _encode_cursor(content_hash: str, offset: int) -> str:
    value = json.dumps(
        {"hash": content_hash, "offset": offset}, separators=(",", ":")
    ).encode()
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _decode_cursor(cursor: str, content_hash: str) -> int:
    try:
        value = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        if value["hash"] != content_hash:
            raise ValueError("hash")
        offset = value["offset"]
        if type(offset) is not int or offset < 0:
            raise ValueError("offset")
        return offset
    except (ValueError, TypeError, KeyError):
        raise DomainError(
            "CONTEXT_CURSOR_INVALID", "Artifact cursor is invalid", 403
        ) from None


class ArtifactPaginator:
    """Deterministic forward/backward paging over an immutable artifact."""

    def __init__(self, artifacts: ArtifactStore, page_bytes: int = 1024):
        if not 128 <= page_bytes <= 8192:
            raise DomainError(
                "CONTEXT_BOUND", "Artifact page size must be 128 to 8192 bytes", 400
            )
        self.artifacts = artifacts
        self.page_bytes = page_bytes

    def page(self, content_hash: str, cursor: str | None = None) -> ArtifactPage:
        offset = _decode_cursor(cursor, content_hash) if cursor else 0
        raw = self.artifacts.read(content_hash)
        if offset > len(raw):
            raise DomainError(
                "CONTEXT_CURSOR_INVALID", "Artifact offset is outside source", 403
            )
        end = min(len(raw), offset + self.page_bytes)
        while end > offset:
            try:
                text = raw[offset:end].decode("utf-8")
                break
            except UnicodeDecodeError:
                end -= 1
        else:
            raise DomainError("CONTEXT_BINARY_UNSUPPORTED", "Artifact is not readable UTF-8")
        continuation = (
            _encode_cursor(content_hash, end) if end < len(raw) else None
        )
        previous = (
            _encode_cursor(content_hash, max(0, offset - self.page_bytes))
            if offset > 0
            else None
        )
        return ArtifactPage(
            content_hash=content_hash,
            offset=offset,
            end_offset=end,
            total_bytes=len(raw),
            text=text,
            continuation=continuation,
            previous=previous,
            full_read=offset == 0 and end == len(raw),
        )
