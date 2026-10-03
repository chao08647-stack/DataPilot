"""Hand-audited adversarial orders, independent of template data and gold SQL.

Eligible mature orders: 1,2,3,4,5,6,9,10,11 (nine).
Fully classifiable: 1,2,3,4,6 (five); late: 1,3,6 (three).
Refunded within 30d: 1,2,6 (three); late-and-refunded: 1,6 (two).
Refund amounts: order 1 = 10+20, order 2 = 5, order 6 = 7; total 42.
Orders 5/9/10/11 are unknown, not on-time; 7 immature, 8 outside cohort,
12 cancelled. Package count, carrier count and reason count are not order count.
"""
from pathlib import Path

import duckdb
import pytest

from insight.analytics import run_analysis
from insight.scenarios import load_scenarios
from insight.sql import execute_query

ROOT = Path(__file__).resolve().parents[2]
# These hand-audited orders target the original 2025-12-31 enterprise contract.
# Current light-BI observation windows and wider table schemas are tested separately.
SCENE = load_scenarios(ROOT / "scenarios", profile="enterprise")["ecommerce"]
DIAGNOSTIC = next(d for d in SCENE["diagnostics"] if d["id"] == "refund-fulfillment")
QUERIES = {q["id"]: q for q in DIAGNOSTIC["queries"]}
ALLOWED = [t["name"] for t in SCENE["tables"]]


@pytest.fixture
def fixture_database(tmp_path):
    path = tmp_path / "independent-multiple-packages.duckdb"
    with duckdb.connect(str(path)) as db:
        for table in SCENE["tables"]:
            fields = ",".join(f'"{name}" {kind}' for name, kind in table["columns"].items())
            db.execute(f'CREATE TABLE "{table["name"]}" ({fields})')
        orders = [(i, 1, "2025-10-01", "web", "completed", 100) for i in range(1, 13)]
        orders[5] = (6, 1, "2025-11-30", "web", "completed", 100)
        orders[6] = (7, 1, "2025-12-10", "web", "completed", 100)
        orders[7] = (8, 1, "2025-09-25", "web", "completed", 100)
        orders[11] = (12, 1, "2025-10-01", "web", "cancelled", 100)
        db.executemany("INSERT INTO orders VALUES (?,?,?,?,?,?)", orders)
        # The first order spans three packages and two carriers, with one late.
        shipments = [
            (101, 1, "2025-10-03", "2025-10-03", "A"),
            (102, 1, "2025-10-03", "2025-10-05", "B"),
            (103, 1, "2025-10-03", "2025-10-02", "A"),
            (201, 2, "2025-10-03", "2025-10-02", "A"),
            (202, 2, "2025-10-03", "2025-10-03", "B"),
            (301, 3, "2025-10-03", "2025-10-05", "A"),
            (401, 4, "2025-10-03", "2025-10-03", "A"),
            (501, 5, "2025-10-03", "2025-10-03", "A"),
            (502, 5, "2025-10-03", None, "B"),
            (601, 6, "2025-12-02", "2025-12-04", "A"),
            (602, 6, "2025-12-02", "2025-12-02", "B"),
            (701, 7, "2025-12-12", "2025-12-12", "A"),
            (801, 8, "2025-09-27", "2025-09-27", "A"),
            (1001, 10, None, "2025-10-03", "A"),
            (1101, 11, "2025-10-03", "2026-01-01", "A"),
            (1201, 12, "2025-10-03", "2025-10-03", "A"),
        ]
        db.executemany("INSERT INTO shipments VALUES (?,?,?,?,?)", shipments)
        refunds = [
            (101, 1, 11, "2025-10-05", 10, "completed"),
            (102, 1, 12, "2025-10-10", 20, "completed"),
            (103, 1, 11, "2025-10-15", 999, "processing"),
            (104, 1, 11, "2025-12-01", 999, "completed"),  # Mature but outside 30d.
            (201, 2, 21, "2025-10-06", 5, "completed"),
            (202, 2, 21, "2025-09-30", 999, "completed"),  # Before original order.
            (501, 5, 51, "2025-10-10", 100, "completed"), # Partly undelivered.
            (601, 6, 61, "2025-12-30", 7, "completed"),   # Exactly day 30, next month.
            (602, 6, 61, "2025-12-31", 999, "completed"), # Day 31: excluded.
            (701, 7, 71, "2025-12-20", 100, "completed"),
            (801, 8, 81, "2025-10-02", 100, "completed"), # Refund date in range, order not.
            (901, 9, 91, "2025-10-10", 100, "completed"), # No package data.
            (1001, 10, 101, "2025-10-10", 100, "completed"),
            (1101, 11, 111, "2025-10-10", 100, "completed"),
            (1201, 12, 121, "2025-10-10", 100, "completed"),
        ]
        db.executemany("INSERT INTO refunds VALUES (?,?,?,?,?,?)", refunds)
        db.execute("INSERT INTO suppliers VALUES (1,'S1','region')")
        db.execute("INSERT INTO products VALUES (1,'P1','category',100,30)")
        db.execute("INSERT INTO product_batches VALUES (1,1,1,'2025-01-01')")
        # Even a repeated mapping must not fan out a refund amount.
        db.execute("INSERT INTO order_item_batches VALUES (11,1),(11,1),(12,1),(21,1)")
        db.executemany("INSERT INTO return_cases VALUES (?,?,?,?,?)", [
            (1, 101, "late", "closed", 11), (2, 101, "damaged", "closed", 11),
            (3, 101, "late", "closed", 11), (4, 102, "packaging", "closed", 12),
            (5, 102, "packaging", "closed", 12), (6, 201, "size", "closed", 21),
        ])
    return path


def execute(path, query_id):
    return execute_query(path, QUERIES[query_id]["sql"], SCENE, ALLOWED, query_id)


def test_primary_counts_are_orders_not_packages_carriers_refunds(fixture_database):
    result = execute(fixture_database, "refund_input")
    assert [dict(zip(result["columns"], row)) for row in result["rows"]] == [{
        "group": "web", "orders": 5, "refunded_orders": 3, "late_orders": 3,
        "late_refunded_orders": 2, "mature_orders": 9, "unknown_fulfillment_orders": 4,
    }]


def test_tool_compares_late_and_on_time_inside_same_channel(fixture_database):
    result = run_analysis("refund_diagnosis", [execute(fixture_database, "refund_input")])
    assert result["status"] == "ok"
    row = result["series"][0]
    assert row["refund_pct"] == 60
    assert row["late_refund_pct"] == pytest.approx(66.6667)
    assert row["on_time_refund_pct"] == 50
    assert row["gap_pp"] == pytest.approx(16.6667)


def test_refund_reasons_use_same_order_cohort_window_and_one_amount_per_refund(fixture_database):
    result = execute(fixture_database, "refund_reasons")
    rows = [dict(zip(result["columns"], row)) for row in result["rows"]]
    assert {(r["supplier_name"], r["product_name"], r["reason"]): (r["refunded_orders"], r["refunded_amount"]) for r in rows} == {
        ("S1", "P1", "packaging"): (1, 20), ("S1", "P1", "多原因"): (1, 10),
        ("S1", "P1", "size"): (1, 5), ("未记录", "未记录", "未记录"): (1, 7),
    }
    assert sum(r["refunded_amount"] for r in rows) == 42
    # Order 1 legitimately appears in two reason groups: do not sum group distinct counts.
    assert sum(r["refunded_orders"] for r in rows) == 4


def test_additional_on_time_package_and_duplicate_reason_do_not_change_counts(fixture_database):
    before = {key: execute(fixture_database, key)["rows"] for key in QUERIES}
    with duckdb.connect(str(fixture_database)) as db:
        db.execute("INSERT INTO shipments VALUES(203,2,'2025-10-03','2025-10-03','C')")
        db.execute("INSERT INTO return_cases VALUES(7,201,'size','closed',21)")
    assert before == {key: execute(fixture_database, key)["rows"] for key in QUERIES}


def test_unknown_fulfillment_is_insufficient_not_automatically_on_time(fixture_database):
    with duckdb.connect(str(fixture_database)) as db:
        db.execute("UPDATE shipments SET delivered_date=NULL")
    query = execute(fixture_database, "refund_input")
    row = dict(zip(query["columns"], query["rows"][0]))
    assert row["orders"] == row["late_orders"] == 0
    assert row["unknown_fulfillment_orders"] == row["mature_orders"] == 9
    result = run_analysis("refund_diagnosis", [query])
    assert result["status"] == "insufficient_data" and not result["facts"]
    assert execute(fixture_database, "refund_reasons")["rows"] == []


def test_refund_dashboards_share_fixed_contract_and_channel_binding(fixture_database):
    cards = {card["id"]: card for board in SCENE["dashboard_templates"] for card in board["cards"]}
    for card_id, query_id in [("late-refund", "refund_input"), ("refund-suppliers", "refund_reasons")]:
        definition = cards[card_id]["query"]
        assert definition["sql"] == QUERIES[query_id]["sql"]
        assert definition["metric_ids"] == QUERIES[query_id]["metric_ids"]
        assert "carrier" not in definition["filter_bindings"]
        assert "channel" in definition["filter_bindings"]
        actual = execute_query(fixture_database, definition["sql"], SCENE, ALLOWED, card_id)
        assert actual["rows"] == execute(fixture_database, query_id)["rows"]


def test_overall_metric_is_not_silently_redefined_as_delivered_subset():
    metrics = {m["id"]: m for m in SCENE["metrics"]}
    assert "shipments" not in metrics["mature_refund_rate"]["tables"]
    assert "shipments" in metrics["delivered_cohort_refund_rate"]["tables"]
    assert "refund_amount" not in QUERIES["refund_reasons"]["metric_ids"]
    assert "delivery_late_rate" not in QUERIES["refund_input"]["metric_ids"]
