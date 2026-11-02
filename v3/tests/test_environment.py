import importlib
from importlib.metadata import version
from shutil import copyfile
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo
from redis import Redis

from mgar_v3.settings import ROOT, Settings
from mgar_v3.store.migrate import migrate


def test_locked_runtime_imports():
    for name in ["fastapi", "pydantic", "langchain", "langgraph", "mcp", "psycopg", "redis"]:
        importlib.import_module(name)
    assert version("langgraph-checkpoint-postgres").startswith("3.")


def test_real_postgres_vector_and_idempotent_migrations(database):
    assert migrate(database) == []
    assert migrate(database) == []
    with psycopg.connect(database) as conn:
        assert conn.execute("SELECT '[1,2,3]'::vector <-> '[1,2,3]'::vector").fetchone() == (0.0,)
        assert conn.execute("SELECT count(*) FROM app.schema_migrations").fetchone() == (9,)


def test_real_redis_ping_and_reconstructable_cache():
    settings = Settings()
    cache = Redis.from_url(settings.redis_url.get_secret_value())
    assert cache.ping()
    key = "mgar-v3:test:environment"
    cache.set(key, b"disposable", ex=10)
    assert cache.get(key) == b"disposable"
    cache.delete(key)
    cache.close()


def test_call_limit_migration_preserves_existing_one_call_budgets(tmp_path):
    base = Settings().database_url.get_secret_value()
    name = "mgar_v3_budget_test_" + uuid4().hex
    with psycopg.connect(base, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        dsn = make_conninfo(base, dbname=name)
        old_migrations = tmp_path / "old-migrations"
        old_migrations.mkdir()
        for path in sorted((ROOT / "migrations").glob("*.sql"))[:4]:
            copyfile(path, old_migrations / path.name)
        assert len(migrate(dsn, old_migrations)) == 4
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(
                "INSERT INTO app.authorization_budgets(profile_hash,limit_micros,used_calls) "
                "VALUES (%s,1000000,1)",
                ("a" * 64,),
            )
        assert migrate(dsn) == [
            "0005_profile_call_limits.sql",
            "0006_r2_mailbox.sql",
            "0007_r3_router.sql",
            "0008_r4_knowledge.sql",
            "0009_r4_trials.sql",
        ]
        assert migrate(dsn) == []
        with psycopg.connect(dsn, autocommit=True) as conn:
            assert conn.execute(
                "SELECT call_limit,used_calls FROM app.authorization_budgets"
            ).fetchone() == (1, 1)
            with pytest.raises(psycopg.errors.CheckViolation):
                conn.execute("UPDATE app.authorization_budgets SET used_calls=2")
    finally:
        assert name.startswith("mgar_v3_budget_test_") and len(name) == 52
        with psycopg.connect(base, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
