import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import duckdb
import psycopg
import pytest

from insight.connectors import (
    ConnectorError,
    DuckDBConnector,
    MySQLConnector,
    PostgresConnector,
    connector_for,
)
from insight.sql import SQLRejected, bind_parameters, validate_charts, validate_sql


@pytest.fixture
def scene():
    return {"tables": [
        {"name": "orders", "columns": {"id": "INTEGER", "amount": "DOUBLE", "day": "DATE"}},
        {"name": "refunds", "columns": {"order_id": "INTEGER", "amount": "DOUBLE"}},
    ], "relations": [{"left_table": "orders", "left_column": "id", "right_table": "refunds", "right_column": "order_id"}]}


@pytest.fixture
def duck_source(tmp_path):
    path = tmp_path / "business.duckdb"
    with duckdb.connect(str(path)) as conn:
        conn.execute("CREATE TABLE orders(id INTEGER,amount DOUBLE,day DATE)")
        conn.execute("CREATE TABLE refunds(order_id INTEGER,amount DOUBLE)")
        conn.execute("INSERT INTO orders VALUES(1,10,'2025-01-01'),(2,20,'2025-01-02'),(3,30,'2025-01-03')")
        conn.execute("INSERT INTO refunds VALUES(1,2),(1,3),(2,4)")
    return {"id": "test", "kind": "duckdb", "config": {"path": str(path), "allowed_tables": ["orders", "refunds"]}}


def test_duckdb_connector_metadata_test_and_bound_board_query(scene, duck_source):
    connector = connector_for(duck_source)
    before = Path(duck_source["config"]["path"]).read_bytes()
    assert connector.test()["connected"]
    metadata = connector.introspect()
    assert {t["name"] for t in metadata} == {"orders", "refunds"}
    result = connector.execute("SELECT * FROM (SELECT id,day,amount FROM orders) AS board_data WHERE day>=CAST(? AS DATE) AND amount>? ORDER BY id",
                               scene, ["orders"], "board:query", parameters=["2025-01-02", 5], limit=1)
    assert result["rows"] == [[2, "2025-01-02", 20.0]]
    assert result["truncated"]
    assert Path(duck_source["config"]["path"]).read_bytes() == before
    assert not connector.cancel("board:query")


def test_source_and_published_table_scope_intersection(scene, duck_source):
    duck_source["config"]["allowed_tables"] = ["main.orders"]
    connector = connector_for(duck_source)
    assert [t["name"] for t in connector.introspect()] == ["orders"]
    assert connector.execute("SELECT COUNT(*) AS n FROM main.orders", scene, ["orders"])["rows"] == [[3]]
    with pytest.raises(SQLRejected):
        connector.execute("SELECT * FROM refunds", scene, ["orders", "refunds"])
    with pytest.raises(SQLRejected):
        connector.execute("SELECT * FROM orders", scene, ["refunds"])
    duck_source["config"]["allowed_tables"] = []
    connector = connector_for(duck_source)
    assert not connector.introspect()
    with pytest.raises(SQLRejected):
        connector.execute("SELECT * FROM orders", scene, ["orders"])


@pytest.mark.parametrize("sql,expected", [
    ("SELECT o.id FROM orders o WHERE EXISTS(SELECT 1 FROM refunds r WHERE r.order_id=o.id) ORDER BY o.id", [[1], [2]]),
    ("SELECT o.id FROM orders o WHERE o.id IN(SELECT r.order_id FROM refunds r) ORDER BY o.id", [[1], [2]]),
    ("SELECT o.id,(SELECT SUM(r.amount) FROM refunds r WHERE r.order_id=o.id) AS total FROM orders o ORDER BY o.id", [[1, 5.0], [2, 4.0], [3, None]]),
    ("SELECT id FROM orders WHERE amount>(SELECT AVG(amount) FROM orders)", [[3]]),
    ("SELECT id,ROW_NUMBER() OVER(ORDER BY amount DESC) AS rank FROM orders ORDER BY id", [[1, 3], [2, 2], [3, 1]]),
])
def test_controlled_subqueries_and_windows_execute(scene, duck_source, sql, expected):
    assert connector_for(duck_source).execute(sql, scene, ["orders", "refunds"])["rows"] == expected


@pytest.mark.parametrize("dialect,schema", [("duckdb", "main"), ("postgres", "public"), ("mysql", "business")])
@pytest.mark.parametrize("sql", [
    "SELECT * FROM orders o WHERE EXISTS(SELECT 1 FROM refunds r WHERE r.amount=o.amount)",
    "SELECT * FROM orders o WHERE EXISTS(SELECT 1 FROM refunds r WHERE r.order_id=o.id OR 1=1)",
    "SELECT * FROM orders o WHERE o.amount IN(SELECT r.amount FROM refunds r)",
    "SELECT (SELECT r.amount FROM refunds r) FROM orders",
    "SELECT public.date_trunc('month',day) FROM orders",
    "SELECT * FROM information_schema.tables",
    "SELECT * FROM orders; DELETE FROM orders",
    "SELECT * FROM orders FOR UPDATE",
    "SELECT pg_sleep(100)",
    "SELECT LOAD_FILE('/etc/passwd')",
    "SELECT NEXTVAL('counter')",
])
def test_enterprise_guard_keeps_write_system_function_and_relation_boundary(scene, dialect, schema, sql):
    with pytest.raises(SQLRejected):
        validate_sql(sql, scene, ["orders", "refunds"], dialect=dialect, schemas=[schema], allow_subqueries=True)


def test_schema_qualification_and_no_default_udf_search(scene):
    assert '"public"."orders"' in validate_sql("SELECT * FROM orders", scene, ["orders"], dialect="postgres", schemas=["public"])
    assert '`business`.`orders`' in validate_sql("SELECT * FROM orders", scene, ["orders"], dialect="mysql", schemas=["business"])
    with pytest.raises(SQLRejected):
        validate_sql("SELECT * FROM private.orders", scene, ["orders"], dialect="postgres", schemas=["public"])
    with pytest.raises(SQLRejected):
        validate_sql("SELECT amount::evil_type FROM orders", scene, ["orders"], dialect="postgres", schemas=["public"])
    with pytest.raises(SQLRejected):
        validate_sql("SELECT @secret:=1", scene, ["orders"], dialect="mysql", schemas=["business"])


def test_bind_values_never_interpolates_question_marks_or_percent_literals():
    dangerous = "x' OR 1=1 --"
    for dialect in ["duckdb", "postgres", "mysql"]:
        sql, values = bind_parameters("SELECT '?' AS literal,'100%' AS pct WHERE ?=?", dialect, [dangerous, "safe"])
        assert dangerous not in sql and values == (dangerous, "safe")
        assert "'?'" in sql
        if dialect != "duckdb":
            assert "'100%%'" in sql and sql.count("%s") == 2
        named_sql = "SELECT $v AS v,$v AS repeated" if dialect == "duckdb" else "SELECT :v AS v,:v AS repeated"
        bound, values = bind_parameters(named_sql, dialect, {"v": 15})
        assert values == (15, 15)
    with pytest.raises(ValueError):
        bind_parameters("SELECT ?", "postgres", [])
    with pytest.raises(ValueError):
        bind_parameters("SELECT ?", "mysql", None)


@pytest.fixture
def temporal_scene(tmp_path):
    path = tmp_path / "temporal.duckdb"
    with duckdb.connect(str(path)) as conn:
        conn.execute("CREATE TABLE business_calendar(calendar_date DATE)")
        conn.execute("CREATE TABLE subscription_versions(effective_from DATE,effective_to DATE,monthly_price DOUBLE)")
        conn.execute("INSERT INTO business_calendar VALUES('2025-01-31'),('2025-02-28')")
        conn.execute("INSERT INTO subscription_versions VALUES('2025-01-01','2025-02-01',10),('2025-02-01',NULL,20)")
    scene = {"tables": [
        {"name": "business_calendar", "columns": {"calendar_date": "DATE"}},
        {"name": "subscription_versions", "columns": {"effective_from": "DATE", "effective_to": "DATE", "monthly_price": "DOUBLE"}},
    ], "relations": [{"type": "temporal", "left_table": "business_calendar", "left_column": "calendar_date",
                      "right_table": "subscription_versions", "right_column": "effective_from", "end_column": "effective_to",
                      "end_inclusive": False, "allow_open_end": True}]}
    connector = DuckDBConnector({"id": "temporal", "kind": "duckdb", "config": {"path": str(path)}})
    return connector, scene


def test_temporal_join_as_of_boundary_and_null_end_execute(temporal_scene):
    connector, scene = temporal_scene
    sql = "SELECT c.calendar_date,SUM(v.monthly_price) AS mrr FROM business_calendar c LEFT JOIN subscription_versions v ON c.calendar_date>=v.effective_from AND(c.calendar_date<v.effective_to OR v.effective_to IS NULL) GROUP BY c.calendar_date ORDER BY c.calendar_date"
    assert connector.execute(sql, scene, [t["name"] for t in scene["tables"]])["rows"] == [["2025-01-31", 10.0], ["2025-02-28", 20.0]]
    reverse = "SELECT SUM(v.monthly_price) AS n FROM subscription_versions v JOIN business_calendar c ON v.effective_from<=c.calendar_date AND(v.effective_to>c.calendar_date OR v.effective_to IS NULL)"
    assert connector.execute(reverse, scene, [t["name"] for t in scene["tables"]])["rows"] == [[30.0]]


@pytest.mark.parametrize("condition", [
    "c.calendar_date>=v.effective_from",
    "c.calendar_date=v.effective_from",
    "c.calendar_date>=v.effective_from AND(c.calendar_date<v.effective_to OR 1=1)",
    "c.calendar_date>=v.effective_from AND c.calendar_date<=v.effective_to",
    "c.calendar_date>=v.effective_from OR c.calendar_date<v.effective_to",
])
def test_temporal_join_incomplete_or_wrong_boundary_rejected(temporal_scene, condition):
    connector, scene = temporal_scene
    with pytest.raises(SQLRejected):
        connector.execute(f"SELECT COUNT(*) AS n FROM business_calendar c JOIN subscription_versions v ON {condition}", scene, [t["name"] for t in scene["tables"]])


def test_connector_rejects_plain_credentials_and_non_source_env(monkeypatch):
    with pytest.raises(ConnectorError):
        connector_for({"kind": "postgres", "config": {"password": "not-a-real-secret"}})
    connector = PostgresConnector({"kind": "postgres", "config": {"host": "localhost", "database": "test", "user": "reader", "password_env": "INSIGHT_LLM_API_KEY"}})
    with pytest.raises(ConnectorError, match="INSIGHT_SOURCE"):
        connector._connection_values()
    connector.config["password_env"] = "INSIGHT_SOURCE_TEST_PASSWORD"
    monkeypatch.setenv("INSIGHT_SOURCE_TEST_PASSWORD", "synthetic-pass@:#'")
    assert connector._connection_values()["password"] == "synthetic-pass@:#'"
    assert "password" not in connector.source["config"]


class FakePGCursor:
    def __init__(self, connection):
        self.connection = connection
        self.description = [SimpleNamespace(name="id"), SimpleNamespace(name="amount")]
        self.remaining = [(1, 10), (2, 20), (3, 30)]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.connection.cursor_closed = True

    def execute(self, sql, parameters=None):
        self.connection.calls.append((sql, parameters))
        self.connection.started.set()
        if self.connection.block:
            assert self.connection.cancelled.wait(3)
            raise psycopg.errors.QueryCanceled("cancelled")

    def fetchmany(self, size):
        result, self.remaining = self.remaining[:size], self.remaining[size:]
        return result


class FakePG:
    def __init__(self, block=False):
        self.calls = []
        self.read_only = False
        self.closed = self.rolled_back = self.cursor_closed = False
        self.cancelled, self.started = threading.Event(), threading.Event()
        self.block = block

    def execute(self, sql, parameters=None):
        self.calls.append((sql, parameters))
        return SimpleNamespace(fetchall=lambda: [("public", "orders", "id", "integer", 1), ("private", "secret", "secret", "text", 1)])

    def cursor(self, name=None):
        assert name == "insight_readonly_preview"
        return FakePGCursor(self)

    def cancel_safe(self, timeout):
        self.cancelled.set()

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


def pg_source():
    return {"id": "pg", "kind": "postgres", "config": {"host": "localhost", "database": "business", "user": "reader", "password_env": "INSIGHT_SOURCE_TEST_PASSWORD", "schemas": ["public"], "allowed_tables": ["public.orders", "public.refunds"]}}


def test_postgres_driver_readonly_binding_server_cursor_and_metadata(scene, monkeypatch):
    conn = FakePG()
    captured = {}
    monkeypatch.setenv("INSIGHT_SOURCE_TEST_PASSWORD", "synthetic")
    def connect(**kwargs):
        captured.update(kwargs)
        return conn
    monkeypatch.setattr(psycopg, "connect", connect)
    connector = PostgresConnector(pg_source())
    result = connector.execute("SELECT id,amount FROM orders WHERE amount>?", scene, ["orders"], parameters=[5], limit=2)
    assert conn.read_only and conn.rolled_back and conn.closed and conn.cursor_closed
    assert "default_transaction_read_only=on" in captured["options"]
    assert "pg_catalog" in conn.calls[0][0] and "statement_timeout" in conn.calls[0][0]
    assert conn.calls[-1][1] == (5,) and "%s" in conn.calls[-1][0]
    assert result["rows"] == [[1, 10], [2, 20]] and result["truncated"]
    assert [table["name"] for table in connector.introspect()] == ["public.orders"]


def test_postgres_cancel_only_its_registered_query_and_cleans_up(scene, monkeypatch):
    conn = FakePG(block=True)
    monkeypatch.setenv("INSIGHT_SOURCE_TEST_PASSWORD", "synthetic")
    monkeypatch.setattr(psycopg, "connect", lambda **kwargs: conn)
    connector = PostgresConnector(pg_source())
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(connector.execute, "SELECT id,amount FROM orders", scene, ["orders"], "run1:q1")
        assert conn.started.wait(2)
        assert not connector.cancel("run2:q1")
        assert connector.cancel("run1:q1")
        with pytest.raises(TimeoutError):
            future.result(timeout=4)
    assert conn.closed and conn.rolled_back
    assert not connector.cancel("run1:q1")


def test_chart_new_shapes_validate_required_fields():
    query = {"id": "q1", "columns": ["cohort", "period", "value", "total"], "rows": [["2025-01", "M1", 0.8, 100]]}
    heatmap = {"type": "heatmap", "title": "retention", "query_id": "q1", "x": "period", "group_by": "cohort", "y": ["value"]}
    waterfall = {"type": "waterfall", "title": "bridge", "query_id": "q1", "x": "period", "y": ["value"], "start_value": 100, "total_column": "total"}
    funnel = {"type": "funnel", "title": "funnel", "query_id": "q1", "x": "period", "y": ["total"]}
    assert len(validate_charts([heatmap, waterfall, funnel], [query])) == 3
    for bad in [{**heatmap, "group_by": "missing"}, {**waterfall, "start_value": float("nan")}, {**waterfall, "total_column": "missing"}]:
        assert validate_charts([bad], [query])[0]["type"] == "table"


class FakeMySQLCursor:
    def __init__(self, connection):
        self.connection = connection
        self.description = [("id",), ("amount",)]
        self.query = ""
        self.remaining = [(1, 10), (2, 20), (3, 30)]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def execute(self, query, parameters=None):
        self.query = query
        self.connection.calls.append((query, parameters))

    def fetchmany(self, size):
        result, self.remaining = self.remaining[:size], self.remaining[size:]
        return result

    def fetchall(self):
        return [("business", "orders", "id", "int", 1), ("mysql", "user", "authentication_string", "text", 1)]


class FakeMySQL:
    def __init__(self):
        self.calls = []
        self.closed = self.rolled_back = False

    def cursor(self):
        return FakeMySQLCursor(self)

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True

    def thread_id(self):
        return 321


def test_mysql_real_driver_contract_readonly_binding_limit_and_own_cancel(scene, monkeypatch):
    conn, captured = FakeMySQL(), []
    def connect(**kwargs):
        captured.append(kwargs)
        return conn
    fake_driver = SimpleNamespace(connect=connect, cursors=SimpleNamespace(SSCursor=object), Error=type("DriverError", (Exception,), {}))
    monkeypatch.setitem(sys.modules, "pymysql", fake_driver)
    monkeypatch.setenv("INSIGHT_SOURCE_TEST_PASSWORD", "synthetic-pass")
    connector = MySQLConnector({"id": "mysql", "kind": "mysql", "config": {"host": "localhost", "database": "business", "user": "reader", "password_env": "INSIGHT_SOURCE_TEST_PASSWORD", "allowed_tables": ["orders"]}})
    result = connector.execute("SELECT id,amount FROM orders WHERE amount>?", scene, ["orders"], parameters=[5], timeout=3, limit=2)
    assert not captured[0]["local_infile"] and not captured[0]["autocommit"]
    assert conn.calls[0][0] == "SET SESSION TRANSACTION READ ONLY"
    assert conn.calls[1] == ("SET SESSION MAX_EXECUTION_TIME=%s", (3000,))
    assert conn.calls[2][0] == "START TRANSACTION READ ONLY"
    assert conn.calls[3][1] == (5,) and "LIMIT 3" in conn.calls[3][0] and "%s" in conn.calls[3][0]
    assert result["rows"] == [[1, 10], [2, 20]] and result["truncated"]
    assert conn.closed and conn.rolled_back
    assert [t["name"] for t in connector.introspect()] == ["business.orders"]
    connector._cancel_connection(conn)
    assert conn.calls[-1] == ("KILL QUERY 321", None)
    connector.config["schemas"] = ["other_database"]
    with pytest.raises(ConnectorError, match="configured database"):
        connector._connection_values()


def test_mysql_executable_comments_are_removed(scene):
    query = validate_sql("SELECT id FROM orders /*!50000 INTO OUTFILE '/tmp/secret' */", scene, ["orders"], dialect="mysql", schemas=["business"], allow_subqueries=True)
    assert "OUTFILE" not in query and "/*!" not in query


@pytest.mark.parametrize("dialect", ["duckdb", "postgres", "mysql"])
@pytest.mark.parametrize("query", [
    "SELECT id FROM orders WHERE EXISTS(WITH a AS (SELECT order_id FROM refunds WHERE amount=orders.amount) SELECT * FROM a)",
    "SELECT o.id FROM orders o WHERE EXISTS(SELECT 1 FROM (SELECT order_id FROM refunds WHERE amount=o.amount) r)",
])
def test_nested_derived_and_cte_cannot_hide_unapproved_correlation(scene, dialect, query):
    with pytest.raises(SQLRejected, match="关联"):
        validate_sql(query, scene, ["orders", "refunds"], dialect=dialect, allow_subqueries=True)


def test_nested_cte_with_complete_approved_correlation_is_accepted(scene):
    query = "SELECT o.id FROM orders o WHERE EXISTS(WITH a AS (SELECT r.order_id FROM refunds r WHERE r.order_id=o.id) SELECT * FROM a)"
    assert "EXISTS" in validate_sql(query, scene, ["orders", "refunds"], allow_subqueries=True)
