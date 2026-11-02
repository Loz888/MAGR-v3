import asyncio
from uuid import UUID

import httpx

from mgar_v3.coordination.engine import Engine
from mgar_v3.domain import DomainError, Investigation
from mgar_v3.graph.runner import FixtureInvestigator
from mgar_v3.live import LivePermit
from mgar_v3.mcp.client import ReadOnlyGitHubMCP
from mgar_v3.models.deepseek import DeepSeekInvestigator


class LiveInvestigator(FixtureInvestigator):
    mode = "live"

    def __init__(
        self,
        engine: Engine,
        run_id: UUID,
        permit: LivePermit,
        reader: ReadOnlyGitHubMCP | None = None,
        model_client: httpx.Client | None = None,
    ):
        self.engine, self.run_id, self.permit = engine, run_id, permit
        self.reader, self.model_client = reader, model_client

    def read(self) -> tuple[bytes, str, str]:
        self.permit.validate()
        profile = self.permit.profile
        reader = self.reader or ReadOnlyGitHubMCP(
            profile.repository,
            token=self.engine.settings.github_read_token,
            max_bytes=profile.max_source_bytes,
            redis_url=self.engine.settings.redis_url,
        )
        try:
            if profile.source_kind == "issue":
                result = asyncio.run(reader.read_issue(profile.issue_number))
                if result.version != profile.expected_source_hash:
                    raise DomainError("STALE_SOURCE", "Issue title or body changed")
            else:
                assert profile.source_ref is not None
                result = asyncio.run(reader.read_file(profile.source_path, profile.source_ref))
        except DomainError:
            raise
        except Exception:
            raise DomainError(
                "MCP_READ_FAILED", "GitHub source read did not complete", 502
            ) from None
        return result.content, result.uri, result.version

    def investigate(self, question: str, content: str, evidence_id: UUID) -> Investigation:
        return DeepSeekInvestigator(self.engine, self.permit, self.model_client).investigate(
            self.run_id, question, content, evidence_id, execution_epoch=self.execution_epoch
        )
