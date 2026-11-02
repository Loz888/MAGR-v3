import asyncio
import sys
from pathlib import Path

import pytest

from mgar_v3.domain import DomainError
from mgar_v3.mcp.client import ReadOnlyGitHubMCP


def test_real_cross_process_mcp_initialize_list_and_read(monkeypatch):
    monkeypatch.setenv("MGAR_V3_DEEPSEEK_API_KEY", "fixture-canary-not-a-key")
    server = Path(__file__).parent / "fixtures/mcp_server.py"
    client = ReadOnlyGitHubMCP("example/mgar-test", command=sys.executable, args=(str(server),))
    source = asyncio.run(client.read_file("README.md", "a" * 40))
    assert source.content == b"Observation: a controlled MCP source\n"
    assert source.version == "b" * 40
    assert source.uri.endswith("ref=" + "a" * 40)


@pytest.mark.parametrize("path,ref", [("../secret", "a" * 40), ("README.md", "main")])
def test_paths_and_mutable_refs_are_rejected_before_tool_call(path, ref):
    client = ReadOnlyGitHubMCP("example/mgar-test", command="must-not-execute")
    with pytest.raises(DomainError):
        asyncio.run(client.read_file(path, ref))


def test_real_mcp_issue_source_has_content_bound_version():
    import hashlib

    server = Path(__file__).parent / "fixtures/mcp_server.py"
    client = ReadOnlyGitHubMCP("example/mgar-test", command=sys.executable, args=(str(server),))
    source = asyncio.run(client.read_issue(1))
    assert source.content.startswith(b"# Controlled issue")
    assert source.version == hashlib.sha256(source.content).hexdigest()
