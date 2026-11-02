"""V3 adaptation of V2's closed SELECT grammar (fe7e04a, data/policy.py)."""

import json
import math
import re
import secrets
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import psycopg
import sqlglot
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from pydantic import SecretStr
from sqlglot import exp

from mgar_v3.domain import DomainError, digest

if TYPE_CHECKING:
    from mgar_v3.coordination.engine import Engine

ALLOWED = frozenset(
    {
        exp.Select,
        exp.Alias,
        exp.Column,
        exp.Identifier,
        exp.From,
        exp.Table,
        exp.Star,
        exp.Literal,
        exp.Placeholder,
        exp.Where,
        exp.Group,
        exp.Having,
        exp.Order,
        exp.Ordered,
        exp.Limit,
        exp.Count,
        exp.Sum,
        exp.Avg,
        exp.Min,
        exp.Max,
        exp.Distinct,
        exp.EQ,
        exp.NEQ,
        exp.GT,
        exp.GTE,
        exp.LT,
        exp.LTE,
        exp.And,
        exp.Or,
        exp.Not,
        exp.Paren,
        exp.Add,
        exp.Sub,
        exp.Mul,
        exp.Div,
        exp.Mod,
        exp.Is,
        exp.Null,
        exp.Boolean,
        exp.Neg,
        exp.Like,
        exp.ILike,
        exp.In,
        exp.Between,
    }
)


def validate_sql(statement: str, parameters: list[Any], schema: str) -> str:
    try:
        if (
            not re.fullmatch(r"r4_data_[a-f0-9]{12}", schema)
            or not 1 <= len(statement.encode()) <= 8192
            or statement.count("(") > 32
            or len(parameters) > 16
        ):
            raise ValueError("bound")
        if any(
            type(v) not in {str, bool, int, float, type(None)}
            or (isinstance(v, str) and len(v) > 4096)
            or (isinstance(v, float) and not math.isfinite(v))
            for v in parameters
        ):
            raise ValueError("parameter")
        parsed = sqlglot.parse(statement, read="postgres")
        if len(parsed) != 1 or type(parsed[0]) is not exp.Select:
            raise ValueError("select")
        tree = parsed[0]
        nodes = list(tree.walk())
        if len(nodes) > 256 or sum(type(n) is exp.Select for n in nodes) != 1:
            raise ValueError("shape")
        if any(type(n) not in ALLOWED or n.comments for n in nodes):
            raise ValueError("operation")
        tables = list(tree.find_all(exp.Table))
        if (
            len(tables) != 1
            or tables[0].name != "orders"
            or tables[0].db != schema
            or tables[0].catalog
        ):
            raise ValueError("scope")
        aliases = {n.alias for n in tree.find_all(exp.Alias)}
        for node in nodes:
            if isinstance(node, exp.Identifier) and not re.fullmatch(
                r"[a-z_][a-z0-9_]{0,62}", node.name
            ):
                raise ValueError("identifier")
            if isinstance(node, exp.Column) and (
                node.name
                not in {"event_id", "order_id", "status", "amount", "created_at"} | aliases
                or node.table not in {"", "orders"}
                or node.db
                or node.catalog
            ):
                raise ValueError("column")
        placeholders = list(tree.find_all(exp.Placeholder))
        if len(placeholders) != len(parameters) or any(n.this is not None for n in placeholders):
            raise ValueError("arity")
        limit = tree.args.get("limit")
        if limit and (
            type(limit.expression) is not exp.Literal
            or not limit.expression.is_int
            or not 0 <= int(limit.expression.this) <= 100
        ):
            raise ValueError("limit")
        return tree.sql(dialect="postgres")
    except Exception:
        raise DomainError(
            "SQL_POLICY_DENIED", "SQL is outside the closed readonly grammar", 403
        ) from None


def value_json(value: Any) -> Any:
    if value is None or type(value) in {str, int, bool}:
        return value
    if isinstance(value, Decimal) and value.is_finite():
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if type(value) is float and math.isfinite(value):
        return value
    raise DomainError("SQL_TYPE_UNSUPPORTED", "SQL result contains an unsupported value type")


class DataReader:
    def __init__(self, profile: dict[str, Any]):
        self.profile = profile
        self.dsn = SecretStr(profile["database_url"])
        values: dict[str, Any] = dict(conninfo_to_dict(self.dsn.get_secret_value()))
        if (
            values.get("host") not in {"127.0.0.1", "localhost"}
            or not values.get("dbname")
            or not re.fullmatch(r"r4_reader_[a-f0-9]{12}", values.get("user", ""))
            or set(values)
            - {"host", "port", "dbname", "user", "password", "sslmode", "connect_timeout"}
            or not re.fullmatch(r"r4_data_[a-f0-9]{12}", profile["schema"])
        ):
            raise DomainError(
                "DATA_PROFILE_DENIED", "Data source is not a bounded local reader", 403
            )
        self.connect: dict[str, Any] = {
            **values,
            "host": "127.0.0.1",
            "hostaddr": "127.0.0.1",
            "connect_timeout": 3,
            "sslmode": "disable",
            "options": "-c default_transaction_read_only=on -c search_path=pg_catalog",
        }

    def describe(self) -> dict[str, Any]:
        return {
            "schema": self.profile["schema"],
            "tables": {
                "orders": {
                    "event_id": "int8",
                    "order_id": "text",
                    "status": "text",
                    "amount": "numeric",
                    "created_at": "timestamptz",
                }
            },
            "fixture_version": self.profile["fixture_version"],
            "fixture_hash": self.profile["fixture_hash"],
        }

    def query(self, statement: str, parameters: list[Any]) -> dict[str, Any]:
        normalized = validate_sql(statement, parameters, self.profile["schema"])
        tree = cast(exp.Select, sqlglot.parse_one(normalized, read="postgres"))
        if tree.args.get("limit") is None:
            tree = tree.limit(101)
        executed = tree.sql(dialect="postgres")
        try:
            with psycopg.connect(**self.connect, autocommit=True) as conn:
                flags = conn.execute(
                    "SELECT rolsuper,rolcreatedb,rolcreaterole,rolreplication,rolbypassrls "
                    "FROM pg_roles WHERE rolname=current_user"
                ).fetchone()
                if flags is None or any(flags):
                    raise DomainError("DATA_ROLE_ELEVATED", "Reader has elevated privileges", 403)
                writable = conn.execute(
                    (
                        "SELECT "
                        "has_table_privilege(current_user,%s,'INSERT,UPDATE,DELETE,TRUNCAT"
                        "E,REFERENCES,TRIGGER') OR "
                        "has_schema_privilege(current_user,%s,'CREATE') OR "
                        "has_schema_privilege(current_user,'app','USAGE')"
                    ),
                    (self.profile["schema"] + ".orders", self.profile["schema"]),
                ).fetchone()
                if not writable or writable[0]:
                    raise DomainError(
                        "DATA_ROLE_ELEVATED", "Reader can write or enter app schema", 403
                    )
                with conn.transaction():
                    conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                    conn.execute(
                        "SELECT "
                        "set_config('statement_timeout','700',true),set_config('lock_timeo"
                        "ut','200',true)"
                    )
                    source = conn.execute(
                        sql.SQL(
                            "SELECT event_id,order_id,status,amount FROM {}.orders ORDER BY "
                            "event_id"
                        ).format(sql.Identifier(self.profile["schema"]))
                    ).fetchmany(1001)
                    observed = [
                        {"event_id": r[0], "order_id": r[1], "status": r[2], "amount": str(r[3])}
                        for r in source
                    ]
                    if len(source) > 1000 or digest(observed) != self.profile["fixture_hash"]:
                        raise DomainError("DATA_SOURCE_CHANGED", "Fixed dataset bytes changed")
                    snap = conn.execute("SELECT pg_current_snapshot()::text").fetchone()
                    with conn.cursor() as cursor:
                        cursor.execute(executed, parameters if parameters else None)
                        if cursor.description is None:
                            raise DomainError("SQL_RESULT_MISSING", "SQL returned no description")
                        columns = [
                            {"name": c.name, "type_oid": c.type_code} for c in cursor.description
                        ]
                        rows = [[value_json(c) for c in row] for row in cursor.fetchmany(101)]
                    if len(rows) > 100 or len({c["name"] for c in columns}) != len(columns):
                        raise DomainError(
                            "SQL_RESULT_LIMIT",
                            "SQL rows or duplicate labels exceed the result contract",
                        )
                    result = {
                        "schema_version": "typed_sql.v1",
                        "statement": normalized,
                        "executed_statement": executed,
                        "parameters": parameters,
                        "snapshot_id": snap[0] if snap else "",
                        "fixture_version": self.profile["fixture_version"],
                        "fixture_hash": self.profile["fixture_hash"],
                        "observed_at": datetime.now(UTC).isoformat(),
                        "columns": columns,
                        "rows": rows,
                        "row_count": len(rows),
                    }
                    if len(json.dumps(result).encode()) > 16384:
                        raise DomainError("SQL_RESULT_LIMIT", "SQL bytes exceed 16 KiB")
                    result["result_hash"] = digest(result)
                    return result
        except DomainError:
            raise
        except (psycopg.errors.QueryCanceled, psycopg.errors.LockNotAvailable):
            raise DomainError("SQL_TIMEOUT", "Readonly SQL exceeded its time bound") from None
        except psycopg.Error:
            raise DomainError("SQL_READ_FAILED", "Readonly SQL did not complete") from None


def provision(
    engine: "Engine", path: Path, rows: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    if rows is None:
        rows = [
            {"event_id": 1, "order_id": "o-1", "status": "paid", "amount": "12.50"},
            {"event_id": 2, "order_id": "o-1", "status": "paid", "amount": "12.50"},
            {"event_id": 3, "order_id": "o-2", "status": "paid", "amount": "7.00"},
            {"event_id": 4, "order_id": "o-3", "status": "cancelled", "amount": "9.00"},
        ]
    rows = sorted(
        [{**r, "amount": format(Decimal(r["amount"]), ".2f")} for r in rows],
        key=lambda r: r["event_id"],
    )
    fixture_hash = digest(rows)
    schema = "r4_data_" + fixture_hash[:12]
    values = conninfo_to_dict(engine.settings.database_url.get_secret_value())
    dbname = str(values["dbname"])
    role_scope = {
        "database": dbname,
        "project_id": engine.settings.project_id,
        "repository": engine.settings.repository.lower(),
        "actor": engine.settings.local_actor.lower(),
        "fixture_hash": fixture_hash,
    }
    role = "r4_reader_" + digest(role_scope)[:12]
    password = secrets.token_hex(32)
    with engine.transaction() as conn:
        conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(schema)))
        conn.execute(
            sql.SQL(
                "CREATE TABLE IF NOT EXISTS {}.orders(event_id bigint PRIMARY "
                "KEY,order_id text NOT NULL,status text NOT NULL,amount numeric(12,2) "
                "NOT NULL,created_at timestamptz NOT NULL)"
            ).format(sql.Identifier(schema))
        )
        existing = conn.execute(
            sql.SQL("SELECT count(*) AS n FROM {}.orders").format(sql.Identifier(schema))
        ).fetchone()
        assert existing is not None
        if not existing["n"]:
            for row in rows:
                conn.execute(
                    sql.SQL("INSERT INTO {}.orders VALUES (%s,%s,%s,%s,%s)").format(
                        sql.Identifier(schema)
                    ),
                    (
                        row["event_id"],
                        row["order_id"],
                        row["status"],
                        row["amount"],
                        "2026-01-01T00:00:00Z",
                    ),
                )
        role_exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (role,)).fetchone()
        if not role_exists:
            conn.execute(
                sql.SQL(
                    "CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
                    "NOREPLICATION NOBYPASSRLS"
                ).format(sql.Identifier(role))
            )
        conn.execute(
            sql.SQL("ALTER ROLE {} PASSWORD {}").format(sql.Identifier(role), sql.Literal(password))
        )
        conn.execute(
            sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                sql.Identifier(dbname), sql.Identifier(role)
            )
        )
        conn.execute(
            sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                sql.Identifier(schema), sql.Identifier(role)
            )
        )
        conn.execute(
            sql.SQL("GRANT SELECT ON {}.orders TO {}").format(
                sql.Identifier(schema), sql.Identifier(role)
            )
        )
    dsn = make_conninfo(
        host="127.0.0.1",
        port=values.get("port", "55433"),
        dbname=dbname,
        user=role,
        password=password,
    )
    profile = {
        "database_url": dsn,
        "reader_role": role,
        "schema": schema,
        "fixture_version": "orders-demo-v1:" + fixture_hash[:12],
        "fixture_hash": fixture_hash,
        "project_id": engine.settings.project_id,
        "repository": engine.settings.repository,
        "actor": engine.settings.local_actor.lower(),
    }
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(profile, indent=2) + "\n")
    path.chmod(0o600)
    return {k: v for k, v in profile.items() if k != "database_url"}
