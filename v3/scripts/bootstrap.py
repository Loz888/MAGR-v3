"""Create only missing, local development secrets; never print their values."""

import os
import secrets
from pathlib import Path

root = Path(__file__).resolve().parents[1]
target = root / ".env"
if target.exists():
    print("Existing .env preserved")
else:
    password = secrets.token_hex(24)
    contents = (
        f"MGAR_V3_DB_PASSWORD={password}\n"
        f"MGAR_V3_DATABASE_URL=postgresql://mgar_v3:{password}@127.0.0.1:55433/mgar_v3\n"
        "MGAR_V3_REDIS_URL=redis://127.0.0.1:56379/0\n"
        f"MGAR_V3_LOCAL_TOKEN={secrets.token_hex(32)}\n"
        "MGAR_V3_ALLOW_PAID=0\nMGAR_V3_ALLOW_EXTERNAL_WRITE=0\n"
    )
    fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(contents)
    print("Created local .env (0600); no external credentials configured")
