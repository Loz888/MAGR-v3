from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import Field

from mgar_v3.domain import Model


class Upload(Model):
    title: Annotated[str, Field(min_length=1, max_length=200)]
    source_uri: Annotated[str, Field(pattern=r"^(kb://|https://)[^\s]{1,500}$")]
    source_version: Annotated[str, Field(min_length=1, max_length=128)]
    media_type: Literal[
        "text/markdown", "text/plain", "text/x-python", "text/x-code", "application/pdf"
    ]
    content_base64: Annotated[str, Field(min_length=1, max_length=2700000)]


class Search(Model):
    query: Annotated[str, Field(min_length=1, max_length=2000)]
    snapshot_id: UUID | None = None
    top_k: Annotated[int, Field(strict=True, ge=1, le=8)] = 6
    candidate_k: Annotated[int, Field(strict=True, ge=1, le=20)] = 20
    source_versions: list[str] = Field(default_factory=list, max_length=4)
    max_context_bytes: Annotated[int, Field(strict=True, ge=512, le=16384)] = 8192


class ParsedChunk(Model):
    text: Annotated[str, Field(min_length=1, max_length=4096)]
    locator: dict[str, Any]


class ACLUpdate(Model):
    readers: Annotated[list[str], Field(max_length=32)] = Field(default_factory=list)
    revoked: bool = False


class ReadChunk(Model):
    chunk_id: UUID
    snapshot_id: UUID


class MemoryConfirmation(Model):
    summary_hash: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    report_hash: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
