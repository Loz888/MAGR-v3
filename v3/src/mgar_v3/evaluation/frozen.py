import hashlib
from pathlib import Path


def implementation_hash(root: Path) -> str:
    files = [
        *root.joinpath("src").rglob("*.py"),
        *root.joinpath("migrations").glob("*.sql"),
        root / "scripts/r4_live.py",
        root / "eval/r4-live/cases.json",
        root / "eval/r4-live/policy.md",
        root / "pyproject.toml",
        root / "uv.lock",
    ]
    value = hashlib.sha256()
    for path in sorted(files):
        value.update(str(path.relative_to(root)).encode() + b"\0" + path.read_bytes() + b"\0")
    return value.hexdigest()
