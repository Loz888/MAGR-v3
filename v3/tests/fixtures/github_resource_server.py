import hashlib

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, EmbeddedResource, TextContent, TextResourceContents

server = FastMCP("controlled-official-file-shape")


@server.tool()
def get_file_contents(owner: str, repo: str, path: str, ref: str) -> CallToolResult:
    data = "def count_orders():\n    return 3\n"
    digest = hashlib.sha1(
        b"blob " + str(len(data.encode())).encode() + b"\0" + data.encode()
    ).hexdigest()
    if path == "bad-hash.py":
        digest = "a" * 40
    if path == "oversized.py":
        data = "x" * 500
    uri = f"repo://{owner}/{repo}/sha/{ref}/contents/{path}"
    if path == "wrong-uri.py":
        uri = "repo://outsider/other/sha/" + ref + "/contents/" + path
    return CallToolResult(
        content=[
            TextContent(type="text", text=f"successfully downloaded text file (SHA: {digest})"),
            EmbeddedResource(
                type="resource",
                resource=TextResourceContents(uri=uri, mimeType="text/plain", text=data),
            ),
        ]
    )


if __name__ == "__main__":
    server.run(transport="stdio")
