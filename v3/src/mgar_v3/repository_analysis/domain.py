"""Typed contracts for read-only repository analysis.

Everything the model may request is enumerated here.  The repository, commit and
credential are supplied by the server and never by repository content or the
model.  All payload models reject unknown fields.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal, Self
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from mgar_v3.domain import Model, digest

REPOSITORY_ANALYSIS_GRAPH = "v32-repo-v1"
ANALYSIS_POLICY_VERSION = "repository-analysis-readonly-v1"

_OWNER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,98}$")
_REPOSITORY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,98}$")
_COMMIT = re.compile(r"^[a-f0-9]{40}$")
_REF_FORBIDDEN = set(" ~^:?*[\\\x00\n\r\t")


class RepositoryIdentity(Model):
    owner: Annotated[str, Field(min_length=1, max_length=99)]
    name: Annotated[str, Field(min_length=1, max_length=100)]

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"

    @property
    def canonical_url(self) -> str:
        return f"https://github.com/{self.full_name}"


def parse_repository_url(value: str) -> tuple[RepositoryIdentity, str | None, str | None]:
    """Return a normalized public GitHub identity plus optional tree/commit hints.

    Only ``https://github.com/<owner>/<repo>`` (optionally with ``.git``,
    ``/tree/<ref>`` or ``/commit/<sha>``) is accepted.  Every other host and
    path is rejected before any network client is constructed.
    """

    raw = value.strip()
    if not raw or len(raw) > 400:
        raise ValueError("A GitHub repository URL is required")
    if any(character in raw for character in "\x00\r\n\t"):
        raise ValueError("Repository URL contains control characters")
    parsed = urlsplit(raw)
    if parsed.scheme != "https" or parsed.hostname != "github.com":
        raise ValueError("Only https://github.com repository URLs are accepted")
    if parsed.username or parsed.password or parsed.port or parsed.query or parsed.fragment:
        raise ValueError("Repository URL cannot contain credentials, a port, query or fragment")
    segments = [segment for segment in parsed.path.split("/") if segment]
    if len(segments) < 2:
        raise ValueError("Repository URL must identify an owner and a repository")
    owner, repository = segments[0], segments[1]
    if repository.endswith(".git"):
        repository = repository[:-4]
    if not _OWNER.fullmatch(owner) or not _REPOSITORY.fullmatch(repository):
        raise ValueError("Repository owner or name is not a canonical GitHub identifier")
    if owner in {".", ".."} or repository in {".", ".."}:
        raise ValueError("Repository owner or name is not a canonical GitHub identifier")
    if len(segments) == 3 and segments[2] in {"issues", "pulls", "pull", "blob", "tree"}:
        raise ValueError("Repository URL may only point at a repository or a tree/commit")
    ref: str | None = None
    commit: str | None = None
    if len(segments) > 2:
        if segments[2] == "tree" and len(segments) >= 4:
            ref = validate_ref("/".join(segments[3:]))
        elif segments[2] == "commit" and len(segments) == 4:
            commit = validate_commit(segments[3])
        else:
            raise ValueError("Unsupported GitHub repository URL shape")
    if len(segments) > 4 and segments[2] != "tree":
        raise ValueError("Unsupported GitHub repository URL shape")
    return RepositoryIdentity(owner=owner.lower(), name=repository.lower()), ref, commit


def validate_ref(value: str) -> str:
    if not value or len(value) > 200:
        raise ValueError("Branch or tag name is outside the supported length")
    if any(character in _REF_FORBIDDEN for character in value):
        raise ValueError("Branch or tag name contains an unsupported character")
    if value.startswith(("/", "-", ".")) or ".." in value:
        raise ValueError("Branch or tag name is not canonical")
    segments = value.split("/")
    if any(not segment or segment in {".", ".."} for segment in segments):
        raise ValueError("Branch or tag name is not canonical")
    return value


def validate_commit(value: str) -> str:
    normalized = value.strip().lower()
    if not _COMMIT.fullmatch(normalized):
        raise ValueError("Commit must be a 40-character hexadecimal SHA")
    return normalized


def validate_repo_path(value: str, *, allow_empty: bool = False) -> str:
    if value == "":
        if allow_empty:
            return value
        raise ValueError("A repository path is required")
    if len(value) > 300 or any(character in value for character in "\x00\r\n\\"):
        raise ValueError("Repository path is not supported")
    if value.startswith(("/", "~")) or ":" in value:
        raise ValueError("Repository path must be repository-relative")
    segments = value.split("/")
    if any(segment in {".", ".."} for segment in segments):
        raise ValueError("Repository path cannot traverse outside the repository")
    if any(not segment for segment in segments):
        raise ValueError("Repository path contains an empty segment")
    return value


def _validate_search_query(value: str, *, full_name: str) -> str:
    if not value or len(value) > 500:
        raise ValueError("Code search query is outside the supported length")
    for match in re.finditer(r"repo:([^\s]+)", value):
        if match.group(1).lower() != full_name:
            raise ValueError("Code search cannot target a different repository")
    if "*" in value or "fork:" in value:
        raise ValueError("Code search cannot use wildcard or fork qualifiers")
    return value


class AnalysisLimits(Model):
    max_model_calls: Annotated[int, Field(strict=True, ge=1, le=12)] = 6
    max_tool_calls: Annotated[int, Field(strict=True, ge=1, le=40)] = 20
    max_input_tokens: Annotated[int, Field(strict=True, ge=4096, le=262144)] = 60000
    max_cost_micros: Annotated[int, Field(strict=True, ge=1, le=10000000)] = 500000
    max_seconds: Annotated[int, Field(strict=True, ge=30, le=3600)] = 600

    def bounded_by(self, other: AnalysisLimits) -> bool:
        return (
            self.max_model_calls <= other.max_model_calls
            and self.max_tool_calls <= other.max_tool_calls
            and self.max_input_tokens <= other.max_input_tokens
            and self.max_cost_micros <= other.max_cost_micros
            and self.max_seconds <= other.max_seconds
        )


class AnalysisModelConfig(Model):
    model_id: Annotated[str, Field(min_length=1, max_length=100)] = "deepseek-chat"
    price_version: Annotated[str, Field(min_length=1, max_length=100)] = (
        "repository-analysis-v1"
    )
    input_micros_per_million: Annotated[int, Field(strict=True, gt=0)] = 300000
    output_micros_per_million: Annotated[int, Field(strict=True, gt=0)] = 1200000
    context_tokens: Annotated[int, Field(strict=True, ge=4096, le=1048576)] = 131072
    max_output_tokens: Annotated[int, Field(strict=True, ge=512, le=8192)] = 2048

    @property
    def reservation_micros(self) -> int:
        numerator = (
            self.context_tokens * self.input_micros_per_million
            + self.max_output_tokens * self.output_micros_per_million
        )
        return (numerator + 999999) // 1000000


class AnalysisAuthorization(Model):
    approved_by: Annotated[str, Field(min_length=1, max_length=100)]
    expires_at: datetime
    allow_paid: bool = False
    allowed_repositories: Annotated[list[str], Field(min_length=1, max_length=100)] = Field(
        default_factory=lambda: ["*"]
    )
    limits: AnalysisLimits = Field(default_factory=AnalysisLimits)
    model: AnalysisModelConfig = Field(default_factory=AnalysisModelConfig)

    @model_validator(mode="after")
    def aware_expiry(self) -> Self:
        if self.expires_at.tzinfo is None:
            raise ValueError("Authorization expiry must include a timezone")
        return self

    def validate_scope(self, repository: str, limits: AnalysisLimits) -> None:
        if not self.allow_paid:
            raise ValueError("Repository analysis authorization does not allow paid calls")
        if self.expires_at <= datetime.now(UTC):
            raise ValueError("Repository analysis authorization has expired")
        allowed = {item.lower() for item in self.allowed_repositories}
        if "*" not in allowed and repository.lower() not in allowed:
            raise ValueError("Repository is outside the authorized read scope")
        if not limits.bounded_by(self.limits):
            raise ValueError("Task budget exceeds the authorized repository analysis limits")

    @property
    def authorization_hash(self) -> str:
        return digest(self.model_dump(mode="json"))


def load_analysis_authorization(
    path: Path, *, allowed_actors: list[str], allow_paid: bool
) -> AnalysisAuthorization:
    authorization = AnalysisAuthorization.model_validate_json(path.read_bytes())
    if not allow_paid:
        raise ValueError("Server has not enabled paid repository analysis")
    if authorization.approved_by.lower() not in {actor.lower() for actor in allowed_actors}:
        raise ValueError("Repository analysis approver is not configured")
    return authorization


class AnalysisBudgetInput(Model):
    limits: AnalysisLimits = Field(default_factory=AnalysisLimits)

    @property
    def digest(self) -> str:
        return digest(self.limits.model_dump(mode="json"))


class CreateAnalysisInput(Model):
    repository_url: Annotated[str, Field(min_length=1, max_length=400)]
    task: Annotated[str, Field(min_length=1, max_length=8000)]
    ref: str | None = None
    commit: str | None = None
    execution_mode: Literal["fixture", "live"] = "fixture"
    client_request_id: Annotated[str, Field(min_length=1, max_length=200)]
    limits: AnalysisLimits = Field(default_factory=AnalysisLimits)

    @field_validator("repository_url")
    @classmethod
    def validate_repository_url(cls, value: str) -> str:
        parse_repository_url(value)
        return value

    @field_validator("ref")
    @classmethod
    def validate_optional_ref(cls, value: str | None) -> str | None:
        return validate_ref(value) if value is not None else None

    @field_validator("commit")
    @classmethod
    def validate_optional_commit(cls, value: str | None) -> str | None:
        return validate_commit(value) if value is not None else None

    @model_validator(mode="after")
    def one_source_hint(self) -> Self:
        if self.ref and self.commit:
            raise ValueError("Specify a branch/tag or a commit, not both")
        return self

    def identity(self) -> RepositoryIdentity:
        identity, ref, commit = parse_repository_url(self.repository_url)
        if self.ref is not None and ref is not None and self.ref != ref:
            raise ValueError("URL tree reference conflicts with the explicit ref")
        if self.commit is not None and commit is not None and self.commit != commit:
            raise ValueError("URL commit conflicts with the explicit commit")
        return identity


class FollowUpInput(Model):
    client_request_id: Annotated[str, Field(min_length=1, max_length=200)]
    body: Annotated[str, Field(min_length=1, max_length=8000)]


class ReadFileCall(Model):
    tool: Literal["read_file"]
    path: str
    start_line: Annotated[int | None, Field(strict=True, ge=1, le=10000000)] = None
    end_line: Annotated[int | None, Field(strict=True, ge=1, le=10000000)] = None

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return validate_repo_path(value)

    @model_validator(mode="after")
    def ordered_range(self) -> Self:
        if (self.start_line is None) != (self.end_line is None):
            raise ValueError("start_line and end_line must be supplied together")
        if self.start_line is not None and self.end_line is not None:
            if self.end_line < self.start_line:
                raise ValueError("end_line must not precede start_line")
        return self


class ListDirectoryCall(Model):
    tool: Literal["list_directory"]
    path: str = ""

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return validate_repo_path(value, allow_empty=True)


class SearchCodeCall(Model):
    tool: Literal["search_code"]
    query: Annotated[str, Field(min_length=1, max_length=500)]
    limit: Annotated[int, Field(strict=True, ge=1, le=30)] = 20

    @field_validator("query")
    @classmethod
    def reject_foreign_repository(cls, value: str) -> str:
        # The concrete repository is validated again by the tool gateway.
        if re.search(r"repo:[^\s]+", value):
            raise ValueError("The repository qualifier is supplied by the server")
        return _validate_search_query(value, full_name="__server_supplied__")


class ReadIssueCall(Model):
    tool: Literal["read_issue"]
    number: Annotated[int, Field(strict=True, gt=0, le=10000000)]


class ReadPullRequestCall(Model):
    tool: Literal["read_pull_request"]
    number: Annotated[int, Field(strict=True, gt=0, le=10000000)]


class RepositoryMetadataCall(Model):
    tool: Literal["repository_metadata"]


ToolCall = Annotated[
    ReadFileCall | ListDirectoryCall | SearchCodeCall | ReadIssueCall | ReadPullRequestCall
    | RepositoryMetadataCall,
    Field(discriminator="tool"),
]


class SourceClaim(Model):
    statement: Annotated[str, Field(min_length=1, max_length=2000)]
    evidence_id: UUID
    quote: Annotated[str, Field(min_length=1, max_length=3000)]


class RepoAnalysisReport(Model):
    conclusion: Annotated[str, Field(min_length=1, max_length=4000)]
    structure: Annotated[str, Field(min_length=1, max_length=4000)]
    key_files: Annotated[list[SourceClaim], Field(max_length=10)] = Field(default_factory=list)
    call_chains: Annotated[list[SourceClaim], Field(max_length=8)] = Field(default_factory=list)
    findings: Annotated[list[SourceClaim], Field(max_length=10)] = Field(default_factory=list)
    uncertainties: Annotated[list[str], Field(max_length=10)] = Field(default_factory=list)
    next_steps: Annotated[list[str], Field(max_length=10)] = Field(default_factory=list)


class AgentDecision(Model):
    action: Literal["tool", "final", "insufficient"]
    thought: Annotated[str, Field(min_length=1, max_length=2000)]
    tool_calls: Annotated[list[ToolCall], Field(max_length=4)] = Field(default_factory=list)
    report: RepoAnalysisReport | None = None
    insufficient_reason: Annotated[str | None, Field(max_length=2000)] = None

    @model_validator(mode="after")
    def action_shape(self) -> Self:
        if self.action == "tool":
            if not self.tool_calls or self.report is not None:
                raise ValueError("Tool decisions require tool_calls and no report")
        elif self.action == "final":
            if self.report is None or not self.report.key_files or not self.report.findings:
                raise ValueError("Final decisions require evidence-bound key files and findings")
            if self.tool_calls:
                raise ValueError("Final decisions cannot also request tools")
        else:
            if not self.insufficient_reason or self.report is not None:
                raise ValueError("Insufficient decisions require a reason and no report")
            if self.tool_calls:
                raise ValueError("Insufficient decisions cannot also request tools")
        return self


class AnalysisScope(Model):
    repository: str
    commit: str
    read_only: Literal[True] = True
    task_digest: str
    policy_version: str = ANALYSIS_POLICY_VERSION
    authorization_hash: str
    profile_hash: str
    limits: AnalysisLimits
    model: AnalysisModelConfig

    @field_validator("commit")
    @classmethod
    def fixed_commit(cls, value: str) -> str:
        return validate_commit(value)

    @field_validator("repository")
    @classmethod
    def canonical_repository(cls, value: str) -> str:
        return value.lower()


class AnalysisSpec(Model):
    run_id: UUID
    thread_id: UUID
    ticket_id: UUID
    repository: str
    requested_ref: str | None
    commit: str
    task: str
    execution_mode: Literal["fixture", "live"]
    limits: AnalysisLimits
    model: AnalysisModelConfig
    deadline_at: datetime
    model_calls_used: int = 0
    tool_calls_used: int = 0
    input_tokens_used: int = 0
    stop_reason: str | None = None
    report_hash: str | None = None
    authorization_hash: str
    profile_hash: str
    parent_run_id: UUID | None = None
    prior_report_hash: str | None = None
    prior_evidence_map: dict[str, str] = Field(default_factory=dict)
    execution_epoch: int = 0
    run_status: str = "queued"
    run_reason_code: str | None = None
    outcome: str | None = None

    @property
    def repository_uri(self) -> str:
        return f"https://github.com/{self.repository}/tree/{self.commit}"


class ToolObservation(Model):
    tool: str
    reference: str
    content: str
    evidence_id: UUID | None = None
    source_version: str | None = None
    truncated: bool = False


class PartialReport(Model):
    reason_code: str
    conclusion: str
    structure: str
    evidence: list[ToolObservation]
    uncertainties: list[str]
    next_steps: list[str]


def evidence_uri(repository: str, commit: str, path: str) -> str:
    return f"github://{repository}@{commit}/{path}"


def locate_quote(content: bytes, quote: str) -> tuple[int, int] | None:
    """Return 1-based start/end lines for a verbatim quote, or ``None``."""

    text = content.decode("utf-8", "replace").replace("\r\n", "\n").replace("\r", "\n")
    normalized = quote.replace("\r\n", "\n").replace("\r", "\n")
    if not normalized:
        return None
    index = text.find(normalized)
    if index < 0:
        return None
    start = text.count("\n", 0, index) + 1
    return start, start + normalized.count("\n")


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def request_digest(input_value: CreateAnalysisInput) -> str:
    return digest(
        {
            "repository_url": input_value.repository_url,
            "task": input_value.task,
            "ref": input_value.ref,
            "commit": input_value.commit,
            "execution_mode": input_value.execution_mode,
            "limits": input_value.limits.model_dump(mode="json"),
        }
    )


def mandate_scope(
    *,
    repository: str,
    commit: str,
    task: str,
    limits: AnalysisLimits,
    model: AnalysisModelConfig,
    authorization_hash: str,
    profile_hash: str,
) -> dict[str, Any]:
    return AnalysisScope(
        repository=repository,
        commit=commit,
        task_digest=sha256_text(task),
        authorization_hash=authorization_hash,
        profile_hash=profile_hash,
        limits=limits,
        model=model,
    ).model_dump(mode="json")
