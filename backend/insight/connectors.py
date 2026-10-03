"""Explicitly scoped read-only business connectors; never application persistence.

Credentials are resolved from INSIGHT_SOURCE_* environment references at connection
time. Each query owns a connection and a cancellation handle; callers run these
synchronous methods in a worker thread, never on the ASGI event loop.
"""
from __future__ import annotations

import datetime as dt
import math
import os
import re
import threading
import time
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from typing import Protocol

import duckdb
import psycopg

from insight.query_results import MAX_RESULT_ROWS, bounded_rows, cursor_rows
from insight.sql import SQLRejected, bind_parameters, execute_query, json_value, validate_sql


class ConnectorError(ValueError):
    """Public error with no connection string or credential details."""


class ConnectorProtocol(Protocol):
    def test(self) -> dict: ...
    def introspect(self) -> list[dict]: ...
    def execute(self, sql, scenario, allowed, query_id="q1", *, parameters=None, timeout=10, limit=MAX_RESULT_ROWS) -> dict: ...
    def cancel(self, query_id) -> bool: ...


SYSTEM_SCHEMAS = {"information_schema", "pg_catalog", "mysql", "sys", "performance_schema"}


class _Running:
    def __init__(self):
        self.cancelled = threading.Event()
        self.done = threading.Event()
        self.expired = threading.Event()
        self.callback = None

    def interrupt(self):
        if self.callback is not None and not self.done.is_set():
            try:
                self.callback()
            except Exception:
                # Database-side timeout is independent of best-effort client cancel.
                pass


class Connector:
    kind = ""

    def __init__(self, source: dict):
        self.source = deepcopy(source)
        self.config = self.source.get("config", {})
        if not isinstance(self.config, dict):
            raise ConnectorError("Invalid data source configuration")
        if set(self.config) & {"password", "uri", "dsn", "connection_string", "api_key"}:
            raise ConnectorError("Credentials must be INSIGHT_SOURCE_* environment references")
        self._active, self._lock = {}, threading.Lock()

    def _password(self):
        reference = self.config.get("password_env", "")
        if not isinstance(reference, str) or not re.fullmatch(r"INSIGHT_SOURCE_[A-Z0-9_]+", reference):
            raise ConnectorError("password_env must reference an INSIGHT_SOURCE_* variable")
        if reference not in os.environ:
            raise ConnectorError("The data source credential environment variable is not set")
        return os.environ[reference]

    @property
    def schemas(self):
        default = "public" if self.kind == "postgres" else self.config.get("database", "main")
        configured = self.config.get("schemas", [default])
        if not isinstance(configured, list) or not configured or any(not isinstance(x, str) or not x for x in configured):
            raise ConnectorError("At least one authorized schema is required")
        if any(s.lower() in SYSTEM_SCHEMAS or s.lower().startswith("pg_") for s in configured):
            raise ConnectorError("System schemas cannot be authorized")
        if self.kind == "mysql" and any(s != self.config.get("database") for s in configured):
            raise ConnectorError("MySQL connector is restricted to its configured database")
        if self.kind == "duckdb" and any(s != "main" for s in configured):
            raise ConnectorError("DuckDB connector is restricted to the main schema")
        return configured

    def _table_allowed(self, name):
        allowed = self.config.get("allowed_tables")
        if allowed is None:
            return True
        if not isinstance(allowed, list) or not all(isinstance(v, str) for v in allowed):
            raise ConnectorError("allowed_tables must be a list")
        leaf = name.rsplit(".", 1)[-1]
        return name in allowed or leaf in allowed

    def _scope(self, scenario, allowed):
        scoped = []
        for table in scenario["tables"]:
            name = table["name"]
            full_name = f"{table['schema']}.{name}" if table.get("schema") and "." not in name else name
            authority_name = full_name if "." in full_name else f"{self.schemas[0]}.{full_name}"
            leaf = full_name.rsplit(".", 1)[-1]
            qualified_schema = full_name.rsplit(".", 1)[0] if "." in full_name else None
            if qualified_schema and qualified_schema not in self.schemas:
                continue
            if (name in allowed or full_name in allowed or authority_name in allowed or leaf in allowed) and self._table_allowed(authority_name):
                scoped.append(full_name)
        return scoped

    @contextmanager
    def _reserve(self, query_id):
        state = _Running()
        with self._lock:
            if query_id in self._active:
                raise ConnectorError("query_id is already active; use run-scoped query IDs")
            self._active[query_id] = state
        try:
            yield state
        finally:
            state.done.set()
            with self._lock:
                self._active.pop(query_id, None)

    def cancel(self, query_id):
        with self._lock:
            state = self._active.get(query_id)
        if state is None:
            return False
        state.cancelled.set()
        state.interrupt()
        return True  # cancellation requested, not a server-side completion guarantee

    @staticmethod
    def _limits(timeout, limit):
        if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 < timeout <= 60:
            raise ConnectorError("Timeout must be positive and at most 60 seconds")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_RESULT_ROWS:
            raise ConnectorError("Result limit must be between 1 and 20000")

    @contextmanager
    def _watchdog(self, state, timeout, cancel):
        state.callback = cancel

        def worker():
            deadline = time.monotonic() + timeout
            while not state.done.wait(0.01):
                if state.cancelled.is_set() or time.monotonic() >= deadline:
                    state.expired.set()
                    while not state.done.is_set():
                        state.interrupt()
                        state.done.wait(0.05)
                    return

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        try:
            if state.cancelled.is_set():
                raise TimeoutError("SQL execution cancelled")
            yield
            if state.expired.is_set() or state.cancelled.is_set():
                raise TimeoutError("SQL execution cancelled or timed out")
        finally:
            state.done.set()
            thread.join()

    @staticmethod
    def _artifact(sql, query_id, columns, rows, limit, started):
        if len(columns) != len({name.lower() for name in columns}):
            raise SQLRejected("Result columns must have unique aliases")
        return {"id": query_id, "sql": sql, "columns": columns,
                **(rows if isinstance(rows, dict) else bounded_rows(columns, rows, limit=limit, normalize=json_value)),
                "elapsed_ms": round((time.perf_counter() - started) * 1000),
                "queried_at": dt.datetime.now(dt.UTC).isoformat()}

    def test(self):
        try:
            result = self.execute("SELECT 1 AS connected", {"tables": [], "relations": []}, [], "connection-test", timeout=5, limit=1)
            return {"status": "ok", "kind": self.kind, "read_only": True, "connected": result["rows"] == [[1]]}
        except Exception as exc:
            return {"status": "error", "kind": self.kind, "error": f"Read-only connection test failed ({type(exc).__name__})"}


class DuckDBConnector(Connector):
    kind = "duckdb"

    @property
    def path(self):
        value = self.config.get("path")
        if not isinstance(value, (str, Path)) or not value:
            raise ConnectorError("DuckDB requires an explicit local path")
        path = Path(value).expanduser().resolve()
        if not path.is_file():
            raise ConnectorError("DuckDB data file does not exist")
        return path

    def introspect(self):
        self.schemas  # validate scope even for metadata reads
        with duckdb.connect(str(self.path), read_only=True, config={"enable_external_access": False, "threads": 2, "memory_limit": "256MB"}) as conn:
            rows = conn.execute("""SELECT c.table_name,c.column_name,c.data_type,c.ordinal_position
                FROM information_schema.columns c JOIN information_schema.tables t
                ON c.table_catalog=t.table_catalog AND c.table_schema=t.table_schema AND c.table_name=t.table_name
                WHERE c.table_schema='main' AND t.table_type='BASE TABLE' ORDER BY c.table_name,c.ordinal_position""").fetchall()
        return self._metadata(rows)

    def _metadata(self, rows):
        grouped = {}
        for name, column, data_type, _ in rows:
            if not self._table_allowed(f"main.{name}") or name.lower().startswith(("pg_", "duckdb_", "sqlite_")):
                continue
            grouped.setdefault(name, {"name": name, "schema": "main", "table_name": name, "columns": {}, "description": "", "grain": "未定义"})["columns"][column] = data_type
        return list(grouped.values())

    def execute(self, sql, scenario, allowed, query_id="q1", *, parameters=None, timeout=10, limit=MAX_RESULT_ROWS):
        self._limits(timeout, limit)
        scoped = self._scope(scenario, allowed)
        with self._reserve(query_id) as state:
            try:
                return execute_query(self.path, sql, scenario, scoped, query_id, parameters=parameters, timeout=timeout, limit=limit,
                                     schemas=self.schemas, allow_subqueries=True, cancel_event=state.cancelled,
                                     register_cancel=lambda callback: setattr(state, "callback", callback))
            except duckdb.InterruptException:
                raise TimeoutError("SQL execution cancelled or timed out") from None


class _RemoteConnector(Connector):
    def _connection_values(self):
        database, host, user = (self.config.get(key) for key in ("database", "host", "user"))
        if not all(isinstance(value, str) and value for value in (database, host, user)):
            raise ConnectorError("Database, host and user must be configured")
        default_port = 5432 if self.kind == "postgres" else 3306
        port = self.config.get("port", default_port)
        if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
            raise ConnectorError("Invalid data source port")
        self.schemas
        return {"host": host, "port": port, "user": user, "password": self._password(), "database": database}

    def _metadata(self, rows):
        grouped = {}
        for schema, name, column, data_type, _ in rows:
            full_name = f"{schema}.{name}"
            if schema not in self.schemas or not self._table_allowed(full_name) or name.lower().startswith(("pg_", "sqlite_", "duckdb_")):
                continue
            grouped.setdefault(full_name, {"name": full_name, "schema": schema, "table_name": name, "columns": {}, "description": "", "grain": "未定义"})["columns"][column] = data_type
        return list(grouped.values())


class PostgresConnector(_RemoteConnector):
    kind = "postgres"

    def _connect(self):
        values = self._connection_values()
        values["dbname"] = values.pop("database")
        try:
            conn = psycopg.connect(**values, connect_timeout=5, application_name="insight-agents-readonly", options="-c default_transaction_read_only=on")
            conn.read_only = True
            return conn
        except psycopg.Error:
            raise ConnectorError("PostgreSQL connection failed; check the authorized source configuration") from None

    @staticmethod
    def _configure(conn, timeout):
        # pg_catalog-only lookup prevents similarly named user functions overriding builtins.
        conn.execute("SELECT set_config('search_path','pg_catalog',true),set_config('statement_timeout',%s,true),set_config('lock_timeout',%s,true)",
                     (str(max(1, int(timeout * 1000))), str(min(2000, max(1, int(timeout * 1000))))))

    def introspect(self):
        conn = self._connect()
        try:
            self._configure(conn, 10)
            rows = conn.execute("""SELECT c.table_schema,c.table_name,c.column_name,c.data_type,c.ordinal_position
                FROM information_schema.columns c JOIN information_schema.tables t
                ON c.table_schema=t.table_schema AND c.table_name=t.table_name
                WHERE c.table_schema=ANY(%s) AND t.table_type='BASE TABLE'
                ORDER BY c.table_schema,c.table_name,c.ordinal_position""", (self.schemas,)).fetchall()
            return self._metadata(rows)
        except psycopg.Error:
            raise ConnectorError("PostgreSQL metadata read failed") from None
        finally:
            try:
                conn.rollback()
            finally:
                conn.close()

    def execute(self, sql, scenario, allowed, query_id="q1", *, parameters=None, timeout=10, limit=MAX_RESULT_ROWS):
        self._limits(timeout, limit)
        validated = validate_sql(sql, scenario, self._scope(scenario, allowed), dialect="postgres", schemas=self.schemas, allow_subqueries=True)
        bound, values = bind_parameters(validated, "postgres", parameters)
        started = time.perf_counter()
        with self._reserve(query_id) as state:
            conn = self._connect()
            try:
                self._configure(conn, timeout)
                with self._watchdog(state, timeout, lambda: conn.cancel_safe(timeout=1)):
                    # A named server-side cursor avoids materializing a huge result client-side.
                    with conn.cursor(name="insight_readonly_preview") as cursor:
                        cursor.execute(bound, values)
                        # Start the named cursor before reading its description.
                        first = cursor.fetchmany(1)
                        columns = [column.name for column in cursor.description]
                        from itertools import chain
                        rows = bounded_rows(columns, chain(first, cursor_rows(cursor)), limit=limit, normalize=json_value)
                return self._artifact(validated, query_id, columns, rows, limit, started)
            except psycopg.errors.QueryCanceled:
                raise TimeoutError("SQL execution cancelled or timed out") from None
            except psycopg.Error as exc:
                message = getattr(getattr(exc, "diag", None), "message_primary", None)
                raise ConnectorError((message or "PostgreSQL query failed")[:600]) from None
            finally:
                try:
                    conn.rollback()
                finally:
                    conn.close()


class MySQLConnector(_RemoteConnector):
    kind = "mysql"

    def _connect(self, timeout=5):
        try:
            import pymysql
        except ImportError:
            raise ConnectorError("Install the PyMySQL connector dependency first") from None
        values = self._connection_values()
        try:
            return pymysql.connect(**values, charset="utf8mb4", autocommit=False, local_infile=False,
                                   connect_timeout=min(5, max(1, int(timeout))), read_timeout=max(2, int(timeout) + 2),
                                   write_timeout=max(2, int(timeout)), cursorclass=pymysql.cursors.SSCursor)
        except pymysql.Error:
            raise ConnectorError("MySQL connection failed; check the authorized source configuration") from None

    @staticmethod
    def _configure(conn, timeout):
        with conn.cursor() as cursor:
            cursor.execute("SET SESSION TRANSACTION READ ONLY")
            cursor.execute("SET SESSION MAX_EXECUTION_TIME=%s", (max(1, int(timeout * 1000)),))
            cursor.execute("START TRANSACTION READ ONLY")

    def _cancel_connection(self, target):
        # KILL QUERY is a control-plane action restricted to this connector's own
        # connection ID, not arbitrary model SQL. The same account can cancel itself.
        connection_id = int(target.thread_id())
        if connection_id <= 0:
            return
        control = self._connect(timeout=2)
        try:
            with control.cursor() as cursor:
                cursor.execute(f"KILL QUERY {connection_id}")
        finally:
            control.close()

    def introspect(self):
        conn = self._connect()
        try:
            self._configure(conn, 10)
            with conn.cursor() as cursor:
                cursor.execute("""SELECT c.TABLE_SCHEMA,c.TABLE_NAME,c.COLUMN_NAME,c.COLUMN_TYPE,c.ORDINAL_POSITION
                    FROM information_schema.COLUMNS c JOIN information_schema.TABLES t
                    ON c.TABLE_SCHEMA=t.TABLE_SCHEMA AND c.TABLE_NAME=t.TABLE_NAME
                    WHERE c.TABLE_SCHEMA=%s AND t.TABLE_TYPE='BASE TABLE'
                    ORDER BY c.TABLE_NAME,c.ORDINAL_POSITION""", (self.config["database"],))
                return self._metadata(cursor.fetchall())
        except Exception as exc:
            if isinstance(exc, ConnectorError):
                raise
            raise ConnectorError("MySQL metadata read failed") from None
        finally:
            try:
                conn.rollback()
            finally:
                conn.close()

    def execute(self, sql, scenario, allowed, query_id="q1", *, parameters=None, timeout=10, limit=MAX_RESULT_ROWS):
        self._limits(timeout, limit)
        validated = validate_sql(sql, scenario, self._scope(scenario, allowed), dialect="mysql", schemas=self.schemas, allow_subqueries=True)
        bound, values = bind_parameters(validated, "mysql", parameters)
        started = time.perf_counter()
        with self._reserve(query_id) as state:
            conn = self._connect(timeout=min(60, timeout + 1))
            try:
                self._configure(conn, timeout)
                with self._watchdog(state, timeout, lambda: self._cancel_connection(conn)):
                    with conn.cursor() as cursor:
                        # Limit on the server, not only fetchmany; SSCursor otherwise
                        # drains outstanding rows when closed.
                        cursor.execute(f"SELECT * FROM ({bound}) AS insight_preview LIMIT {limit + 1}", values)
                        columns = [column[0] for column in cursor.description]
                        rows = bounded_rows(columns, cursor_rows(cursor), limit=limit, normalize=json_value)
                return self._artifact(validated, query_id, columns, rows, limit, started)
            except Exception as exc:
                if isinstance(exc, (SQLRejected, ConnectorError, TimeoutError)):
                    raise
                code = exc.args[0] if exc.args else None
                if code in {1317, 3024} or state.expired.is_set() or state.cancelled.is_set():
                    raise TimeoutError("SQL execution cancelled or timed out") from None
                if code in {2002, 2003, 2006, 2013}:
                    raise ConnectorError("MySQL connection lost during query") from None
                message = exc.args[1] if len(exc.args) > 1 and isinstance(exc.args[1], str) else "MySQL query failed"
                raise ConnectorError(message[:600]) from None
            finally:
                try:
                    conn.rollback()
                finally:
                    conn.close()


def connector_for(source: dict) -> ConnectorProtocol:
    kind = source.get("kind")
    if kind == "mysql":
        raise ConnectorError("MySQL is currently suspended; use PostgreSQL or DuckDB")
    connector = {"duckdb": DuckDBConnector, "postgres": PostgresConnector}.get(kind)
    if connector is None:
        raise ConnectorError("Unsupported data source kind")
    return connector(source)
