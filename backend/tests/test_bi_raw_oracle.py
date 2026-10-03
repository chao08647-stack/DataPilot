"""Opt-in local corpus check: application outputs vs independently read facts.

Skips when the immutable synthetic database is not generated. It does not create
data, mutate storage, call a model, or inject gold numbers into runtime prompts.
"""
import json
from collections import defaultdict
from pathlib import Path

import duckdb
import pytest
from bi_raw_oracle import compute_gold

from insight.analytics import run_analysis
from insight.sql import validate_sql

ROOT = Path(__file__).resolve().parents[2]
DATABASE = ROOT / "data/ecommerce-light-bi-3-1.duckdb"


@pytest.fixture(scope="module")
def gold():
    if not DATABASE.exists():
        pytest.skip("Generate light-bi-3.1 synthetic data explicitly before local corpus checks")
    return compute_gold({"ecommerce": DATABASE})


@pytest.fixture(scope="module")
def actual(gold):
    scenario = json.loads((ROOT / "scenarios/ecommerce/light_bi.json").read_text(encoding="utf-8"))
    assert scenario["data_version"] == "light-bi-3.1"
    results = {}
    with duckdb.connect(str(DATABASE), read_only=True) as connection:
        for recipe in scenario["diagnostics"]:
            inputs = []
            for query in recipe["queries"]:
                validated = validate_sql(query["sql"], scenario, [table["name"] for table in scenario["tables"]], allow_subqueries=True)
                cursor = connection.execute(validated)
                inputs.append({"id": query["id"], "columns": [col[0] for col in cursor.description],
                               "rows": cursor.fetchall(), "truncated": False})
            result = run_analysis(recipe["tool"], inputs, recipe["config"])
            assert result["status"] == "ok", (recipe["id"], result)
            results[recipe["tool"]] = {"inputs": inputs, "result": result}
    return results


def match_fields(expected, received, fields):
    for key in fields:
        if isinstance(expected[key], (float, int)):
            assert received[key] == pytest.approx(expected[key], abs=1e-5, rel=1e-10), key
        else:
            assert received[key] == expected[key], key


def test_q01_all_twelve_months_and_totals_match_independent_raw_ledger(gold, actual):
    expected, result = gold["q01"], actual["period_overview"]["result"]
    assert [row["period"] for row in result["series"]] == [f"2025-{month:02}" for month in range(1, 13)]
    for reference, row in zip(expected["series"], result["series"]):
        match_fields(reference, row, reference)
    match_fields(expected["annual_summary"], result["tables"][0]["rows"][0], expected["annual_summary"])
    assert expected["annual_summary"]["orders"] == 41248
    assert expected["annual_summary"]["sales"] == 43945055.28
    assert expected["annual_summary"]["contribution_profit"] == 16006312.23


def test_q02_category_allocations_restore_independently_observed_quarters(gold, actual):
    expected, result = gold["q02"], actual["grouped_profit_change"]["result"]
    raw = actual["grouped_profit_change"]["inputs"][0]
    rows = [dict(zip(raw["columns"], row)) for row in raw["rows"]]
    index = {(row["period"], row["channel"], row["category"]): row for row in rows}
    assert len(rows) == len(expected["detail"])
    for reference in expected["detail"]:
        match_fields(reference, index[(reference["period"], reference["channel"], reference["category"])], reference)
    assert result["reconciliation"]["opening"] == pytest.approx(expected["periods"]["2025-Q3"]["contribution_profit"])
    assert result["reconciliation"]["closing"] == pytest.approx(expected["periods"]["2025-Q4"]["contribution_profit"])
    for table in result["tables"]:
        assert sum(row["sales_delta"] for row in table["rows"]) == pytest.approx(expected["sales_delta"])
        assert sum(row["profit_delta"] for row in table["rows"]) == pytest.approx(expected["profit_delta"])


def test_q03_first_touch_cost_and_realized_value_match_raw_exclusive_90day_window(gold, actual):
    expected, result = gold["q03"]["series"], actual["customer_value_90d"]["result"]["series"]
    index = {row["channel"]: row for row in result}
    assert len(result) == len(expected) == 4
    for reference in expected:
        match_fields(reference, index[reference["channel"]], reference)
    assert sum(row["new_customers"] for row in expected) == 1500
    assert sum(row["eligible_90d"] for row in expected) == 1500


def test_q04_mature_order_sets_and_refund_amount_groupings_match_raw_refunds(gold, actual):
    expected, output = gold["q04"], actual["refund_diagnosis"]
    raw = output["inputs"][0]
    rows = {row[0]: dict(zip(raw["columns"], row)) for row in raw["rows"]}
    for reference in expected["series"]:
        match_fields(reference, rows[reference["group"]], reference)
    assert sum(row["refunded_orders"] for row in expected["series"]) == 1121
    assert expected["refund_count"] == 1174
    for table in output["result"]["tables"]:
        dimension = table["rows"][0].keys() - {"refunded_amount", "amount_share_pct"}
        dimension = next(iter(dimension))
        grouped = defaultdict(float)
        for row in expected["refund_reasons"]:
            grouped[row[dimension]] += row["refunded_amount"]
        assert set(grouped) == {row[dimension] for row in table["rows"]}
        for row in table["rows"]:
            assert row["refunded_amount"] == pytest.approx(grouped[row[dimension]])
        assert sum(row["refunded_amount"] for row in table["rows"]) == pytest.approx(expected["refunded_amount"])
        assert sum(row["amount_share_pct"] for row in table["rows"]) == pytest.approx(100, abs=1e-4)


def test_q05_fixed_customer_type_and_mature_repurchase_cohorts_match_raw_orders(gold, actual):
    expected, result = gold["q05"], actual["customer_repeat_cohort"]["result"]
    assert len(result["series"]) == len(expected["series"]) == 12
    for reference, row in zip(expected["series"], result["series"]):
        match_fields(reference, row, reference)
    table = result["tables"][0]["rows"]
    assert len(table) == len(expected["new_existing"]) == 2
    for reference, row in zip(expected["new_existing"], table):
        match_fields(reference, row, reference)
    assert sum(row["sales"] for row in table) == pytest.approx(gold["q01"]["annual_summary"]["sales"])
    assert sum(row["contribution_profit"] for row in table) == pytest.approx(gold["q01"]["annual_summary"]["contribution_profit"])


def test_month_channel_allocation_and_acquisition_coverage_have_no_silent_loss(gold):
    check = gold["coverage_checks"]
    assert check["unallocated_2025_month_channels"] == []
    assert check["acquisition_channels_without_first_customers"] == []
    assert check["allocated_2025_marketing"] == check["recorded_2025_marketing"] == 949277.93
