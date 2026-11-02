"""Install only the pinned official Chinese vector package, without a model API."""

import hashlib
import json
import shutil
import stat
import zipfile
from pathlib import PurePosixPath

import httpx

from mgar_v3.settings import ROOT


def main() -> None:
    lock = json.loads((ROOT / "config/rag-model-lock.json").read_text())
    archive = ROOT / ".local/models/downloads/zh_core_web_md-3.8.0-py3-none-any.whl"
    archive.parent.mkdir(parents=True, exist_ok=True)
    if not archive.exists():
        temporary = archive.with_suffix(".pending")
        try:
            with httpx.stream("GET", lock["url"], follow_redirects=True, timeout=60) as response:
                response.raise_for_status()
                size = 0
                with temporary.open("wb") as output:
                    for block in response.iter_bytes():
                        size += len(block)
                        if size > 100_000_000:
                            raise RuntimeError("Model archive exceeds its bound")
                        output.write(block)
            temporary.replace(archive)
        finally:
            temporary.unlink(missing_ok=True)
    if hashlib.sha256(archive.read_bytes()).hexdigest() != lock["sha256"]:
        raise RuntimeError("Official model archive SHA256 mismatch")
    target = ROOT / ".local/models/zh_core_web_md-3.8.0"
    target.mkdir(parents=True, exist_ok=True)
    files = {}
    with zipfile.ZipFile(archive) as zipped:
        for member in zipped.infolist():
            if not member.filename.startswith(lock["archive_prefix"]) or member.is_dir():
                continue
            relative = PurePosixPath(member.filename.removeprefix(lock["archive_prefix"]))
            if (
                relative.is_absolute()
                or ".." in relative.parts
                or stat.S_ISLNK(member.external_attr >> 16)
            ):
                raise RuntimeError("Unsafe model archive member")
            if member.file_size > 80_000_000:
                raise RuntimeError("Model member exceeds its bound")
            destination = target / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            with zipped.open(member) as source, destination.open("wb") as output:
                shutil.copyfileobj(source, output)
            files[str(relative)] = hashlib.sha256(destination.read_bytes()).hexdigest()
    (target / "mgar-model-receipt.json").write_text(
        json.dumps({"archive_sha256": lock["sha256"], "files": files}, indent=2) + "\n"
    )
    print("Installed verified local Chinese vector model; no paid API calls.")


if __name__ == "__main__":
    main()
