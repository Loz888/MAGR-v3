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
    # Only this session's uniquely generated disposable database can be removed.
    assert name.startswith("mgar_v3_test_") and len(name) == 45
    with psycopg.connect(base, autocommit=True) as conn:
        conn.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


@pytest.fixture
def settings(database: str, tmp_path) -> Settings:
    with psycopg.connect(database, autocommit=True) as conn:
        tables = conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname='app' "
            "AND tablename!='schema_migrations'"
        ).fetchall()
        conn.execute(
            sql.SQL("TRUNCATE {} RESTART IDENTITY CASCADE").format(
                sql.SQL(",").join(sql.Identifier("app", row[0]) for row in tables)
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
