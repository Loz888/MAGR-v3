import os

from mcp.server.fastmcp import FastMCP

server = FastMCP("controlled-github-fixture")


@server.tool()
def get_file_contents(owner: str, repo: str, path: str, ref: str) -> dict:
    assert owner == "example" and repo == "mgar-test"
    assert path == "README.md" and ref == "a" * 40
    assert "MGAR_V3_DEEPSEEK_API_KEY" not in os.environ
    assert "MGAR_V3_DATABASE_URL" not in os.environ
    return {
        "content": "Observation: a controlled MCP source\n",
        "encoding": "utf-8",
        "sha": "b" * 40,
    }


@server.tool()
def issue_read(owner: str, repo: str, issue_number: int, method: str) -> dict:
    assert owner == "example" and repo == "mgar-test" and issue_number == 1 and method == "get"
    return {"title": "Controlled issue", "body": "Observation: a versioned issue source"}


@server.tool()
def create_issue() -> str:
    raise AssertionError("write tools must never be called by this adapter")


if __name__ == "__main__":
    server.run(transport="stdio")
