"""Opt-in connector verification against this project's isolated native services.

PostgreSQL probe creates a fresh schema and genuinely SELECT-only role in
insight_agents_checks, then removes only those newly created probe objects.
Never connects to the application's business/history database.
"""
import argparse
import json
import os
import secrets
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
if (ROOT / ".runtime" / "deps").is_dir():
    sys.path.insert(0, str(ROOT / ".runtime" / "deps"))

import psycopg  # noqa: E402 -- explicit project-local dependency path above
from psycopg import sql as psql  # noqa: E402

from insight.connectors import MySQLConnector, PostgresConnector  # noqa: E402
from insight.sql import SQLRejected  # noqa: E402


def postgres_probe():
    credentials = json.loads((ROOT / ".runtime" / "local-postgres.json").read_text(encoding="utf-8"))
    admin = {"host": "127.0.0.1", "port": 15432, "user": "insight", "password": credentials["password"], "connect_timeout": 5}
    with psycopg.connect(**admin, dbname="postgres", autocommit=True) as conn:
        actual_path = Path(conn.execute("SHOW data_directory").fetchone()[0]).resolve()
        if actual_path != (ROOT / ".runtime" / "postgres").resolve():
            raise RuntimeError("Refusing a PostgreSQL cluster outside this project's runtime")
        if not conn.execute("SELECT 1 FROM pg_database WHERE datname=%s", ("insight_agents_checks",)).fetchone():
            conn.execute("CREATE DATABASE insight_agents_checks")
    suffix = uuid4().hex[:12]
    schema, role = f"connector_probe_{suffix}", f"insight_readonly_{suffix}"
    password = secrets.token_urlsafe(32)
    env_name = f"INSIGHT_SOURCE_PROBE_{suffix.upper()}"
    previous = os.environ.get(env_name)
    os.environ[env_name] = password
    created_schema = created_role = False
    started = time.perf_counter()
    try:
        with psycopg.connect(**admin, dbname="insight_agents_checks", autocommit=True) as conn:
            conn.execute(psql.SQL("CREATE SCHEMA {}").format(psql.Identifier(schema)))
            created_schema = True
            conn.execute(psql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD {}").format(psql.Identifier(role), psql.Literal(password)))
            created_role = True
            conn.execute(psql.SQL("CREATE TABLE {}.orders(id INTEGER, amount NUMERIC, day DATE)").format(psql.Identifier(schema)))
            conn.execute(psql.SQL("CREATE TABLE {}.refunds(order_id INTEGER, amount NUMERIC)").format(psql.Identifier(schema)))
            conn.execute(psql.SQL("INSERT INTO {}.orders VALUES(1,10,'2025-01-01'),(2,20,'2025-01-02'),(3,30,'2025-01-03')").format(psql.Identifier(schema)))
            conn.execute(psql.SQL("INSERT INTO {}.refunds VALUES(1,2),(1,3),(2,4)").format(psql.Identifier(schema)))
            conn.execute(psql.SQL("GRANT CONNECT ON DATABASE insight_agents_checks TO {}").format(psql.Identifier(role)))
            conn.execute(psql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(psql.Identifier(schema), psql.Identifier(role)))
            conn.execute(psql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA {} TO {}").format(psql.Identifier(schema), psql.Identifier(role)))
        source = {"id": "postgres-live-check", "kind": "postgres", "config": {
            "host": "127.0.0.1", "port": 15432, "database": "insight_agents_checks", "user": role,
            "password_env": env_name, "schemas": [schema], "allowed_tables": [f"{schema}.orders", f"{schema}.refunds"]}}
        connector = PostgresConnector(source)
        assert connector.test()["status"] == "ok"
        tables = connector.introspect()
        assert {table["name"] for table in tables} == {f"{schema}.orders", f"{schema}.refunds"}
        scene = {"tables": tables, "relations": [{"left_table": f"{schema}.orders", "left_column": "id", "right_table": f"{schema}.refunds", "right_column": "order_id"}]}
        allowed = [table["name"] for table in tables]
        result = connector.execute("SELECT id,amount FROM orders WHERE day>=CAST(? AS DATE) ORDER BY id", scene, allowed, "bound", parameters=["2025-01-02"], limit=1)
        assert result["rows"] == [[2, 20.0]] and result["truncated"]
        result = connector.execute("SELECT o.id,(SELECT SUM(r.amount) FROM refunds r WHERE r.order_id=o.id) AS refund FROM orders o ORDER BY o.id", scene, allowed, "subquery")
        assert result["rows"] == [[1, 5.0], [2, 4.0], [3, None]]
        try:
            connector.execute("SELECT * FROM pg_catalog.pg_authid", scene, allowed, "system")
        except SQLRejected:
            pass
        else:
            raise AssertionError("System catalog access was not rejected")
        # Connect directly without the connector's read-only session option. A
        # genuine restricted role, not merely transaction mode, must deny INSERT.
        reader = {**admin, "user": role, "password": password, "dbname": "insight_agents_checks"}
        with psycopg.connect(**reader) as conn:
            try:
                conn.execute(psql.SQL("INSERT INTO {}.orders VALUES(99,99,'2025-01-01')").format(psql.Identifier(schema)))
            except psycopg.errors.InsufficientPrivilege:
                conn.rollback()
            else:
                conn.rollback()
                raise AssertionError("Reader role has unexpected INSERT privileges")
        with psycopg.connect(**admin, dbname="insight_agents_checks", autocommit=True) as conn:
            conn.execute(psql.SQL("INSERT INTO {}.orders SELECT 1,g::NUMERIC,DATE '2025-01-01' FROM generate_series(1,12000) g").format(psql.Identifier(schema)))
            conn.execute(psql.SQL("INSERT INTO {}.refunds SELECT 1,g::NUMERIC FROM generate_series(1,12000) g").format(psql.Identifier(schema)))
        try:
            connector.execute("SELECT SUM(SQRT(o.amount*r.amount)) AS total FROM orders o JOIN refunds r ON o.id=r.order_id", scene, allowed, "timeout", timeout=0.02)
        except TimeoutError:
            pass
        else:
            raise AssertionError("Deliberately expensive query did not time out")
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(connector.execute,
                "SELECT SUM(SQRT(o.amount*r.amount)) AS total FROM orders o JOIN refunds r ON o.id=r.order_id",
                scene, allowed, "cancel", timeout=10)
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                with connector._lock:
                    active = connector._active.get("cancel")
                if active is not None and active.callback is not None:
                    break
                time.sleep(0.01)
            assert connector.cancel("cancel")
            try:
                future.result(timeout=6)
            except TimeoutError:
                pass
            else:
                raise AssertionError("Cancelled query unexpectedly completed")
        return {"status": "passed", "real_engine": True, "read_only_role": True, "parameter_binding": True,
                "schema_scope": True, "metadata": True, "controlled_subquery": True, "row_limit": True,
                "statement_timeout": True, "explicit_cancellation": True, "elapsed_seconds": round(time.perf_counter() - started, 3)}
    finally:
        # Only delete the unique schema/role just created above, never the shared
        # checks database or unrelated check runs. Cluster identity was verified.
        with psycopg.connect(**admin, dbname="insight_agents_checks", autocommit=True) as conn:
            if created_schema:
                conn.execute(psql.SQL("DROP SCHEMA {} CASCADE").format(psql.Identifier(schema)))
            if created_role:
                conn.execute(psql.SQL("REVOKE CONNECT ON DATABASE insight_agents_checks FROM {}").format(psql.Identifier(role)))
                conn.execute(psql.SQL("DROP ROLE {}").format(psql.Identifier(role)))
        if previous is None:
            os.environ.pop(env_name, None)
        else:
            os.environ[env_name] = previous


def mysql_probe(config_path):
    if config_path is None:
        return {"status": "pending", "real_engine": False, "reason": "No explicitly configured isolated native MySQL check instance"}
    path = Path(config_path).resolve()
    if not path.is_relative_to((ROOT / ".runtime").resolve()):
        raise RuntimeError("MySQL check configuration must be under this project's .runtime")
    source = json.loads(path.read_text(encoding="utf-8"))
    config = source["config"]
    if config.get("host") not in {"127.0.0.1", "localhost"} or config.get("database") != "insight_agents_checks":
        raise RuntimeError("MySQL check requires the dedicated loopback checks database")
    connector = MySQLConnector(source)
    # The caller must provision dedicated fixtures and a SELECT-only reader; this
    # path never installs a server or writes to an existing arbitrary database.
    status = connector.test()
    if status["status"] != "ok":
        raise RuntimeError("Isolated MySQL connector test did not pass")
    tables = connector.introspect()
    scene = {"tables": tables, "relations": []}
    allowed = [table["name"] for table in tables]
    result = connector.execute("SELECT id,amount FROM orders WHERE amount>? ORDER BY id", scene, allowed, "mysql-bound", parameters=[10], limit=1)
    assert result["rows"] == [[2, 20.0]] and result["truncated"]
    return {"status": "passed", "real_engine": True, "parameter_binding": True, "row_limit": True,
            "read_only_session": True, "role_grants_verified": False,
            "limitations": ["Dedicated fixture setup and reader grants are provisioned separately; role DML-denial and cancellation are not verified by this optional probe"]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", required=True)
    parser.add_argument("--postgres", action="store_true")
    parser.add_argument("--mysql-config", type=Path)
    args = parser.parse_args()
    report = {}
    failed = False
    if args.postgres:
        try:
            report["postgres"] = postgres_probe()
        except Exception as exc:  # noqa: BLE001 -- public report never prints connection credentials
            report["postgres"] = {"status": "failed", "error_type": type(exc).__name__}
            failed = True
    try:
        report["mysql"] = mysql_probe(args.mysql_config)
    except Exception as exc:  # noqa: BLE001 -- public report never prints connection credentials
        report["mysql"] = {"status": "failed", "error_type": type(exc).__name__}
        failed = True
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(1 if failed else 0)
