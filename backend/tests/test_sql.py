import os
from pathlib import Path

import duckdb
import pytest

from insight.scenarios import generate_all, load_scenarios
from insight.sql import SQLRejected, error_category, execute_query, validate_sql


@pytest.fixture
def scenario():
    return {
        "tables": [
            {"name": "orders", "columns": {"order_id": "INTEGER", "customer_id": "INTEGER", "amount": "DOUBLE"}},
            {"name": "refunds", "columns": {"refund_id": "INTEGER", "order_id": "INTEGER", "amount": "DOUBLE"}},
            {"name": "customers", "columns": {"customer_id": "INTEGER", "name": "VARCHAR"}},
        ],
        "relations": [
            {"left_table": "orders", "left_column": "order_id", "right_table": "refunds", "right_column": "order_id"},
            {"left_table": "orders", "left_column": "customer_id", "right_table": "customers", "right_column": "customer_id"},
        ],
    }


@pytest.fixture
def business_db(tmp_path, scenario):
    path = tmp_path / "business.duckdb"
    with duckdb.connect(str(path)) as conn:
        conn.execute("CREATE TABLE orders(order_id INTEGER,customer_id INTEGER,amount DOUBLE)")
        conn.execute("CREATE TABLE refunds(refund_id INTEGER,order_id INTEGER,amount DOUBLE)")
        conn.execute("CREATE TABLE customers(customer_id INTEGER,name VARCHAR)")
        conn.execute("INSERT INTO orders VALUES (1,1,10),(2,2,20),(3,1,30)")
        conn.execute("INSERT INTO refunds VALUES (1,1,2),(2,1,3),(3,2,5)")
        conn.execute("INSERT INTO customers VALUES (1,'Alice'),(2,'Bob')")
    return path


@pytest.mark.parametrize("sql", [
    "SELECT SUM(amount) AS total FROM orders",
    "SELECT o.order_id,r.amount FROM orders o LEFT JOIN refunds r ON o.order_id=r.order_id ORDER BY 1",
    "WITH a AS (SELECT order_id,SUM(amount) AS amount FROM orders GROUP BY order_id) SELECT a.order_id,r.amount FROM a JOIN refunds r ON a.order_id=r.order_id",
    "WITH a AS (SELECT order_id AS key FROM orders), b AS (SELECT key AS key2 FROM a) SELECT b.key2,r.amount FROM b JOIN refunds r ON b.key2=r.order_id",
    "WITH a(k,c,n) AS (SELECT * FROM orders) SELECT a.k,r.amount FROM a JOIN refunds r ON a.k=r.order_id",
    "SELECT a.key,r.amount FROM (SELECT order_id AS key FROM orders) a JOIN refunds r ON a.key=r.order_id",
    "WITH a AS (SELECT order_id FROM orders UNION ALL SELECT order_id FROM orders) SELECT a.order_id,r.amount FROM a JOIN refunds r ON a.order_id=r.order_id",
    "SELECT SUM(amount) AS total FROM orders UNION ALL SELECT SUM(amount) AS total FROM refunds",
    "SELECT o.order_id,c.name,r.amount FROM orders o JOIN customers c ON o.customer_id=c.customer_id LEFT JOIN refunds r ON o.order_id=r.order_id AND r.amount>0",
    "SELECT COUNT(*) AS n FROM orders WHERE amount>10",
])
def test_safe_queries_execute_and_derived_keys_preserve_lineage(sql, scenario, business_db):
    result = execute_query(business_db, sql, scenario, [t["name"] for t in scenario["tables"]])
    assert result["rows"]
    assert result["columns"]
    assert not result["truncated"]


@pytest.mark.parametrize("sql", [
    "DELETE FROM orders",
    "DROP TABLE orders",
    "UPDATE orders SET amount=0",
    "CREATE TABLE bad AS SELECT * FROM orders",
    "COPY orders TO 'stolen.csv'",
    "ATTACH 'other.db' AS external",
    "INSTALL httpfs",
    "LOAD httpfs",
    "PRAGMA database_list",
    "SET enable_external_access=true",
    "SELECT * FROM orders; DELETE FROM orders",
    "SELECT * INTO stolen FROM orders",
    "SELECT * FROM read_csv_auto('private.csv')",
    "SELECT * FROM read_parquet('https://example.com/private.parquet')",
    "SELECT * FROM read_blob('private.bin')",
    "SELECT * FROM 'private.csv'",
    "SELECT * FROM glob('*')",
    "SELECT * FROM duckdb_tables()",
    "SELECT * FROM information_schema.tables",
    "SELECT * FROM main.orders",
    "SELECT * FROM pg_catalog.pg_tables",
    "SELECT * FROM query('SELECT * FROM orders')",
    "SELECT * FROM query_table('orders')",
    "SELECT * FROM range(10000000000)",
    "SELECT * FROM unnest([1,2,3])",
    "SELECT nextval('sequence')",
    "SELECT current_setting('home_directory')",
    "SELECT getenv('SECRET')",
    "WITH RECURSIVE a AS (SELECT 1 UNION ALL SELECT 1 FROM a) SELECT * FROM a",
])
def test_writes_external_resources_system_tables_functions_rejected(sql, scenario):
    with pytest.raises(SQLRejected):
        validate_sql(sql, scenario, [t["name"] for t in scenario["tables"]])


@pytest.mark.parametrize("sql", [
    "SELECT * FROM orders o JOIN refunds r ON o.amount=r.amount",
    "SELECT * FROM orders o JOIN refunds r ON o.order_id=r.order_id OR 1=1",
    "SELECT * FROM orders o JOIN refunds r ON (o.order_id=r.order_id OR o.amount=r.amount)",
    "SELECT * FROM orders o CROSS JOIN refunds r",
    "SELECT * FROM orders o, refunds r WHERE o.order_id=r.order_id",
    "SELECT * FROM orders NATURAL JOIN refunds",
    "SELECT * FROM orders JOIN refunds USING(order_id)",
    "WITH a AS (SELECT amount AS order_id FROM orders) SELECT * FROM a JOIN refunds r ON a.order_id=r.order_id",
    "WITH a AS (SELECT order_id+100 AS order_id FROM orders) SELECT * FROM a JOIN refunds r ON a.order_id=r.order_id",
    "WITH a AS (SELECT order_id FROM orders),b AS (SELECT amount AS order_id FROM refunds) SELECT * FROM a JOIN b ON a.order_id=b.order_id",
    "SELECT * FROM (SELECT amount AS order_id FROM orders) a JOIN refunds r ON a.order_id=r.order_id",
    "WITH a AS (SELECT order_id FROM orders UNION ALL SELECT amount FROM orders) SELECT * FROM a JOIN refunds r ON a.order_id=r.order_id",
    "WITH a AS (SELECT order_id FROM orders UNION ALL SELECT order_id+100 FROM orders) SELECT * FROM a JOIN refunds r ON a.order_id=r.order_id",
    "SELECT * FROM orders o WHERE EXISTS(SELECT 1 FROM refunds r WHERE o.amount=r.amount)",
    "SELECT * FROM orders WHERE amount IN(SELECT amount FROM refunds)",
    "SELECT (SELECT MAX(amount) FROM refunds) AS hidden FROM orders",
    "SELECT * REPLACE(amount AS order_id) FROM orders",
])
def test_join_bypass_via_cte_expression_union_or_subquery_rejected(sql, scenario):
    with pytest.raises(SQLRejected):
        validate_sql(sql, scenario, [t["name"] for t in scenario["tables"]])


def test_discovery_allowlist_cannot_expand_to_other_scenario_tables(scenario):
    with pytest.raises(SQLRejected):
        validate_sql("SELECT * FROM refunds", scenario, ["orders"])
    with pytest.raises(SQLRejected):
        validate_sql("SELECT * FROM other", scenario, ["other"])


def test_guard_error_classification_can_recall_dialect_experience(scenario):
    with pytest.raises(SQLRejected) as caught:
        validate_sql("SELECT DATE_FORMAT(CURRENT_DATE,'%Y-%m') FROM orders", scenario, ["orders"])
    assert "DATE_FORMAT" in str(caught.value).upper()
    assert error_category(str(caught.value)) == "dialect"
    assert error_category("禁止表函数、文件读取及外部访问。") == "policy"
    assert error_category("INTERRUPT Error: Interrupted!") == "timeout"


def test_row_limit_duplicate_columns_and_file_unchanged(scenario, business_db):
    before = business_db.read_bytes()
    result = execute_query(business_db, "SELECT order_id FROM orders ORDER BY order_id", scenario, ["orders"], limit=2)
    assert result["rows"] == [[1], [2]]
    assert result["truncated"]
    assert business_db.read_bytes() == before
    with pytest.raises(SQLRejected, match="列名重复"):
        execute_query(business_db, "SELECT order_id AS x,amount AS X FROM orders", scenario, ["orders"])
    for limit in [0, -1, 20001, True]:
        with pytest.raises(ValueError, match="limit"):
            execute_query(business_db, "SELECT 1", scenario, ["orders"], limit=limit)
    for timeout in [0, -1, float("inf"), 61]:
        with pytest.raises(ValueError, match="Timeout"):
            execute_query(business_db, "SELECT 1", scenario, ["orders"], timeout=timeout)


def test_heavy_approved_join_is_interrupted_and_connection_closes(scenario, tmp_path):
    path = tmp_path / "large.duckdb"
    with duckdb.connect(str(path)) as conn:
        conn.execute("CREATE TABLE orders AS SELECT 1 AS order_id,1 AS customer_id,range::DOUBLE AS amount FROM range(20000)")
        conn.execute("CREATE TABLE refunds AS SELECT range AS refund_id,1 AS order_id,range::DOUBLE AS amount FROM range(20000)")
    with pytest.raises((duckdb.InterruptException, TimeoutError)):
        execute_query(path, "SELECT SUM(SQRT(o.amount*r.amount)) AS total FROM orders o JOIN refunds r ON o.order_id=r.order_id", scenario, ["orders", "refunds"], timeout=0.005)
    # A timeout must not leave a locked/leaked connection or a live timer.
    assert execute_query(path, "SELECT COUNT(*) AS n FROM orders", scenario, ["orders"])["rows"] == [[20000]]


@pytest.fixture(params=["channel", "store_id"])
def composite_scene(request, tmp_path):
    key = request.param
    scenario = {
        "tables": [
            {"name": "sales", "columns": {"sale_id": "INTEGER", "customer_id": "INTEGER", key: "VARCHAR", "sale_date": "DATE", "amount": "DOUBLE"}},
            {"name": "observations", "columns": {key: "VARCHAR", "record_date": "DATE", "amount": "DOUBLE"}},
            {"name": "customers", "columns": {"customer_id": "INTEGER"}},
        ],
        "relations": [
            {"left_table": "sales", "left_column": key, "right_table": "observations", "right_column": key,
             "additional_keys": [{"left_column": "sale_date", "right_column": "record_date"}]},
            {"left_table": "sales", "left_column": "customer_id", "right_table": "customers", "right_column": "customer_id"},
        ],
    }
    path = tmp_path / f"composite-{key}.duckdb"
    with duckdb.connect(str(path)) as conn:
        conn.execute(f"CREATE TABLE sales(sale_id INTEGER,customer_id INTEGER,{key} VARCHAR,sale_date DATE,amount DOUBLE)")
        conn.execute(f"CREATE TABLE observations({key} VARCHAR,record_date DATE,amount DOUBLE)")
        conn.execute("CREATE TABLE customers(customer_id INTEGER)")
        conn.execute("INSERT INTO sales VALUES(1,1,'A','2025-01-01',10),(2,1,'A','2025-01-02',20)")
        conn.execute("INSERT INTO observations VALUES('A','2025-01-01',1),('A','2025-01-02',2)")
        conn.execute("INSERT INTO customers VALUES(1)")
    return key, scenario, path


def test_complete_composite_keys_execute_through_cte_and_reverse_join(composite_scene):
    key, scenario, path = composite_scene
    sql = f"""WITH a AS(SELECT {key},sale_date,SUM(amount) AS revenue FROM sales GROUP BY 1,2),
        b AS(SELECT {key},record_date,SUM(amount) AS measure FROM observations GROUP BY 1,2)
        SELECT a.{key},SUM(a.revenue) AS revenue,SUM(b.measure) AS measure FROM a
        LEFT JOIN b ON a.{key}=b.{key} AND a.sale_date=b.record_date GROUP BY 1"""
    assert execute_query(path, sql, scenario, ["sales", "observations"])["rows"] == [["A", 30.0, 3.0]]
    reverse = f"SELECT SUM(s.amount) AS revenue FROM observations o JOIN sales s ON o.record_date=s.sale_date AND o.{key}=s.{key}"
    assert execute_query(path, reverse, scenario, ["sales", "observations"])["rows"] == [[30.0]]


@pytest.mark.parametrize("condition", [
    "s.{key}=o.{key}",
    "s.{key}=o.{key} AND (s.sale_date=o.record_date OR 1=1)",
    "s.{key}=o.{key} OR s.sale_date=o.record_date",
    "s.sale_date=o.record_date",
])
def test_missing_or_optional_composite_date_key_rejected(composite_scene, condition):
    key, scenario, _ = composite_scene
    sql = f"SELECT COUNT(*) AS n FROM sales s JOIN observations o ON {condition.format(key=key)}"
    with pytest.raises(SQLRejected):
        validate_sql(sql, scenario, ["sales", "observations"])


def test_composite_keys_cannot_mix_two_aliases_of_same_physical_table(composite_scene):
    key, scenario, _ = composite_scene
    sql = f"""SELECT COUNT(*) AS n FROM sales first_sale
        JOIN customers c ON first_sale.customer_id=c.customer_id
        JOIN sales second_sale ON second_sale.customer_id=c.customer_id
        JOIN observations o ON first_sale.{key}=o.{key} AND second_sale.sale_date=o.record_date"""
    with pytest.raises(SQLRejected):
        validate_sql(sql, scenario, ["sales", "observations", "customers"])


@pytest.fixture(scope="module")
def all_scenario_data(tmp_path_factory):
    root = Path(__file__).resolve().parents[2] / "scenarios"
    scenarios = load_scenarios(root, profile="enterprise")
    target = Path(os.environ["INSIGHT_TEST_DATA_DIR"]) if os.getenv("INSIGHT_TEST_DATA_DIR") else tmp_path_factory.mktemp("sql_scenarios")
    paths = generate_all(root, target, profile="enterprise")
    return scenarios, paths


def test_all_checked_in_examples_and_gold_queries_pass_guard_and_execute(all_scenario_data):
    scenarios, paths = all_scenario_data
    for scenario_id, scenario in scenarios.items():
        allowed = [table["name"] for table in scenario["tables"]]
        for case in scenario["examples"] + scenario["evaluation"]:
            result = execute_query(paths[scenario_id], case["sql"], scenario, allowed, query_id=case["id"])
            assert result["columns"], f"{scenario_id}/{case['id']}"
            if "expected" in case:
                assert result["rows"] == case["expected"], f"{scenario_id}/{case['id']}"
@pytest.mark.parametrize("question,expected_divisor", [("展示销售额", 10000), ("本次用元展示销售额", 1), ("本次用万元展示销售额", 10000)])
def test_currency_preference_scales_presentation_not_evidence(question, expected_divisor):
    from insight.sql import apply_chart_units
    scene = {"metrics": [{"id": "sales", "unit": "元"}, {"id": "margin", "unit": "%"}]}
    queries = [{"id": "q1", "metric_ids": ["sales"], "columns": ["sales"], "rows": [[123000]]},
               {"id": "q2", "metric_ids": ["sales", "margin"], "columns": ["sales", "margin"], "rows": [[123000, 30]]}]
    charts = [{"type": "kpi", "query_id": "q1", "y": ["sales"]}, {"type": "bar", "query_id": "q2", "y": ["sales", "margin"]}]
    values = apply_chart_units(charts, queries, scene, {"currency_display_unit": "万"}, question)
    assert values[0]["value_divisor"] == expected_divisor
    assert "value_divisor" not in values[1]  # Never divide ratios/counts because another column is money.
    assert queries[0]["rows"] == [[123000]]
