"""Install a hash-pinned official server without changing global executables or credentials."""

import hashlib
import io
import json
import platform
import tarfile
import urllib.request

from mgar_v3.settings import ROOT


def main() -> None:
    lock = json.loads((ROOT / "config/github-mcp-lock.json").read_text())
    if platform.system() != "Linux" or platform.machine() not in {"x86_64", "AMD64"}:
        raise SystemExit("The pinned installer supports Linux x86_64 only")
    target = ROOT / ".local/tools/github-mcp-server" / lock["version"] / "github-mcp-server"
    if target.exists() and hashlib.sha256(target.read_bytes()).hexdigest() == lock["binary_sha256"]:
        print("Pinned GitHub MCP server is installed")
        return
    with urllib.request.urlopen(lock["archive_url"], timeout=60) as response:
        archive = response.read(100_000_001)
    if len(archive) > 100_000_000 or hashlib.sha256(archive).hexdigest() != lock["archive_sha256"]:
        raise SystemExit("GitHub MCP archive checksum failed")
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        members = [
            m
            for m in tar.getmembers()
            if m.isfile() and m.name.rsplit("/", 1)[-1] == "github-mcp-server"
        ]
        if len(members) != 1:
            raise SystemExit("Unexpected GitHub MCP archive layout")
        stream = tar.extractfile(members[0])
        assert stream is not None
        binary = stream.read()
    if hashlib.sha256(binary).hexdigest() != lock["binary_sha256"]:
        raise SystemExit("GitHub MCP executable checksum failed")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(binary)
    target.chmod(0o755)
    print("Installed pinned official GitHub MCP server " + lock["version"])


if __name__ == "__main__":
    main()
