import hashlib
import os
import re
import tempfile
from pathlib import Path

from mgar_v3.domain import DomainError


class ArtifactStore:
    def __init__(self, root: Path, max_bytes: int = 2_000_000):
        self.root = root
        self.max_bytes = max_bytes
        root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def path(self, content_hash: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{64}", content_hash):
            raise DomainError("INVALID_ARTIFACT", "Invalid content hash", 400)
        return self.root / content_hash[:2] / content_hash

    def put(self, content: bytes) -> str:
        if len(content) > self.max_bytes:
            raise DomainError("ARTIFACT_TOO_LARGE", "Artifact exceeds byte limit", 413)
        content_hash = hashlib.sha256(content).hexdigest()
        target = self.path(content_hash)
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if target.exists():
            self.read(content_hash)
            return content_hash
        descriptor, temporary = tempfile.mkstemp(dir=target.parent, prefix=".pending-")
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
            directory_fd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return content_hash

    def read(self, content_hash: str) -> bytes:
        try:
            content = self.path(content_hash).read_bytes()
        except FileNotFoundError as exc:
            raise DomainError("ARTIFACT_MISSING", "Artifact bytes are unavailable", 404) from exc
        if len(content) > self.max_bytes or hashlib.sha256(content).hexdigest() != content_hash:
            raise DomainError("ARTIFACT_CORRUPTED", "Artifact hash or size does not match")
        return content
