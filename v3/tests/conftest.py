import hashlib
from collections.abc import Iterator
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from mgar_v3.coordination.engine import Engine
from mgar_v3.domain import TriggerInput
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.settings import Settings
from mgar_v3.store.migrate import migrate


@pytest.fixture(scope="session")
def database() -> Iterator[str]:
    base = Settings().database_url.get_secret_value()
    name = "mgar_v3_test_" + uuid4().hex
    with psycopg.connect(base, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    dsn = make_conninfo(base, dbname=name)
    migrate(dsn)
    yield dsn
    role = "r4_reader_" + hashlib.sha256(name.encode()).hexdigest()[:12]
    with psycopg.connect(dsn, autocommit=True) as conn:
        if conn.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (role,)).fetchone():
            conn.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
            conn.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))
    # Only this session's uniquely generated disposable database can be removed.
    assert name.startswith("mgar_v3_test_") and len(name) == 45
    with psycopg.connect(base, autocommit=True) as conn:
        conn.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


@pytest.fixture
def settings(database: str, tmp_path) -> Settings:
    with psycopg.connect(database, autocommit=True) as conn:
        for (schema,) in conn.execute(
            "SELECT nspname FROM pg_namespace WHERE nspname ~ '^r4_data_[a-f0-9]{12}$'"
        ).fetchall():
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
        tables = conn.execute(
            "SELECT schemaname,tablename FROM pg_tables WHERE schemaname IN ('app','knowledge') "
            "AND tablename!='schema_migrations'"
        ).fetchall()
        conn.execute(
            sql.SQL("TRUNCATE {} RESTART IDENTITY CASCADE").format(
                sql.SQL(",").join(sql.Identifier(row[0], row[1]) for row in tables)
            )
        )
    return Settings(
        database_url=database,
        artifact_root=tmp_path / "artifacts",
        repository="example/mgar-test",
        local_actor="owner",
        allowed_actors=["owner"],
        graph_version="r1-quotes-v1",
    )


@pytest.fixture
def engine(settings: Settings) -> Iterator[Engine]:
    value = Engine(settings, ArtifactStore(settings.artifact_root))
    yield value
    value.close()


@pytest.fixture
def trigger() -> TriggerInput:
    return TriggerInput(
        source="github",
        external_event_id="delivery-1",
        project_id="orders-demo",
        repository="example/mgar-test",
        issue_number=1,
        actor="owner",
        body="/mgar investigate Summarize documented duplicate-count symptoms",
    )
