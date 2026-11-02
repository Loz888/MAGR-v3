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


def test_configured_github_reference_is_opt_in_and_preserves_explicit_tokens(settings, monkeypatch):
    from pydantic import SecretStr

    from mgar_v3.settings import Settings

    monkeypatch.setenv("GH_TOKEN", "fixture-existing-github-token")
    plain = Settings(database_url=settings.database_url, local_token=settings.local_token)
    assert plain.github_read_token is None and plain.github_write_token is None
    enabled = Settings(
        database_url=settings.database_url,
        local_token=settings.local_token,
        github_use_gh_token=True,
        github_read_token=SecretStr("fixture-read-token"),
    )
    assert enabled.github_read_token.get_secret_value() == "fixture-read-token"
    assert enabled.github_write_token.get_secret_value() == "fixture-existing-github-token"


def test_unverified_native_server_is_rejected_before_credentials_are_used(settings, tmp_path):
    from mgar_v3.mcp.factory import github_client

    binary = tmp_path / "wrong-server"
    binary.write_text("not the approved executable")
    binary.chmod(0o755)
    settings.github_mcp_binary = binary
    with pytest.raises(DomainError) as error:
        github_client(settings, "example/mgar-test")
    assert error.value.code == "MCP_BINARY_UNVERIFIED"


def test_only_explicit_github_credential_reaches_stdio_server(settings, monkeypatch):
    import json

    from pydantic import SecretStr

    monkeypatch.setenv("MGAR_V3_DEEPSEEK_API_KEY", "fixture-model-secret")
    monkeypatch.setenv("MGAR_V3_LOCAL_TOKEN", "fixture-control-secret")
    server = Path(__file__).parent / "fixtures/mcp_server.py"
    client = ReadOnlyGitHubMCP(
        "example/mgar-test",
        command=sys.executable,
        args=(str(server),),
        token=SecretStr("fixture-github-credential"),
        redis_url=settings.redis_url,
    )

    async def inventory():
        async with client.session() as session:
            result = await session.call_tool("credential_inventory")
            return json.loads(result.content[0].text)

    assert asyncio.run(inventory()) == {
        "github": True,
        "model": False,
        "database": False,
        "control": False,
    }


def test_official_embedded_resource_file_read_verifies_blob_and_scope():
    import hashlib

    server = Path(__file__).parent / "fixtures/github_resource_server.py"
    client = ReadOnlyGitHubMCP("example/mgar-test", command=sys.executable, args=(str(server),))
    source = asyncio.run(client.read_file("valid.py", "a" * 40))
    assert source.content.startswith(b"def count_orders")
    assert (
        source.version
        == hashlib.sha1(
            b"blob " + str(len(source.content)).encode() + b"\0" + source.content
        ).hexdigest()
    )
    for path in ("bad-hash.py", "wrong-uri.py"):
        with pytest.raises(DomainError):
            asyncio.run(client.read_file(path, "a" * 40))
