import hashlib
from pathlib import Path

import psycopg

from mgar_v3.settings import ROOT, Settings


def migrate(dsn: str, directory: Path = ROOT / "migrations") -> list[str]:
    applied = []
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute("SELECT pg_advisory_lock(31010001)")
        connection.execute("CREATE SCHEMA IF NOT EXISTS app")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS app.schema_migrations "
            "(name text PRIMARY KEY, sha256 text NOT NULL, "
            "applied_at timestamptz NOT NULL DEFAULT now())"
        )
        for path in sorted(directory.glob("*.sql")):
            content = path.read_bytes()
            checksum = hashlib.sha256(content).hexdigest()
            existing = connection.execute(
                "SELECT sha256 FROM app.schema_migrations WHERE name=%s", (path.name,)
            ).fetchone()
            if existing:
                if existing[0] != checksum:
                    raise RuntimeError(f"Published migration changed: {path.name}")
                continue
            with connection.transaction():
                connection.execute(content.decode(), prepare=False)
                connection.execute(
                    "INSERT INTO app.schema_migrations(name,sha256) VALUES (%s,%s)",
                    (path.name, checksum),
                )
            applied.append(path.name)
    return applied


if __name__ == "__main__":
    print({"applied": migrate(Settings().database_url.get_secret_value())})
