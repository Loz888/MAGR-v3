import json
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP

backend = Path(sys.argv[1])
server = FastMCP("controlled-write-fixture")


@server.tool()
def add_issue_comment(owner: str, repo: str, issue_number: int, body: str) -> dict:
    assert owner == "example" and repo == "mgar-test" and issue_number == 1
    comments = json.loads(backend.read_text()) if backend.exists() else []
    comment = {"id": len(comments) + 1, "body": body}
    comments.append(comment)
    backend.write_text(json.dumps(comments))
    return comment


@server.tool()
def issue_read(
    owner: str, repo: str, issue_number: int, method: str, perPage: int, page: int
) -> list:
    assert method == "get_comments" and owner == "example" and repo == "mgar-test"
    return json.loads(backend.read_text()) if backend.exists() else []


if __name__ == "__main__":
    server.run(transport="stdio")
