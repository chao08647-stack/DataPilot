import pytest

from insight.charts import apply_chart_units, validate_charts
from insight.models import Chart


def query(rows=None, **kwargs):
    return {"id": "q", "columns": ["month", "orders", "aov", "channel"],
            "rows": rows or [["2025-01", 10, 250, "甲"], ["2025-02", 12, None, "甲"]], "truncated": False, **kwargs}


def test_combo_has_distinct_units_and_preserves_null():
    chart = Chart(type="combo", title="月度经营", query_id="q", x="month", series=[
        {"column": "orders", "name": "订单量", "unit": "单", "type": "bar", "axis": "left"},
        {"column": "aov", "name": "客单价", "unit": "元", "type": "line", "axis": "right"},
    ]).model_dump()
    assert validate_charts([chart], [query()])[0]["type"] == "combo"
    chart["series"][1]["axis"] = "left"
    assert validate_charts([chart], [query()])[0]["type"] == "table"


@pytest.mark.parametrize("kind", ["pie", "donut"])
def test_incomplete_or_negative_share_not_pie(kind):
    chart = {"type": kind, "title": "占比", "query_id": "q", "x": "month", "y": ["orders"]}
    assert validate_charts([chart], [query(truncated=True)])[0]["type"] == "table"
    assert validate_charts([chart], [query([["2025-01", -2, 250, "甲"]])])[0]["type"] == "table"


def test_duplicate_long_table_dimensions_are_not_implicitly_summed():
    chart = {"type": "stacked_bar", "title": "渠道", "query_id": "q", "x": "month", "y": ["orders"], "group_by": "channel"}
    data = query([["2025-01", 10, 250, "甲"], ["2025-01", 11, 251, "甲"]])
    assert validate_charts([chart], [data])[0]["type"] == "table"


def test_overlapping_distinct_counts_cannot_be_used_as_share_denominator():
    chart = {"type": "pie", "title": "占比", "query_id": "q", "x": "month", "y": ["orders"]}
    assert validate_charts([chart], [query(non_additive_columns=["orders"])])[0]["type"] == "table"


def test_kpi_checks_full_row_count_not_preview_length():
    chart = {"type": "kpi", "title": "销售", "query_id": "q", "y": ["orders"]}
    data = query([["2025-01", 10, 250, "甲"]], total_rows=100)
    assert validate_charts([chart], [data])[0]["type"] == "table"


def test_mixed_units_not_scaled_by_currency_preference():
    chart = {"type": "combo", "title": "月度", "query_id": "q", "y": ["orders", "aov"], "series": [{"column": "orders", "unit": "单"}, {"column": "aov", "unit": "元"}]}
    data = query(metric_ids=["money"])
    out = apply_chart_units([chart], [data], {"metrics": [{"id": "money", "unit": "元"}]}, {"currency_display_unit": "万元"}, "")
    assert out[0].get("value_divisor", 1) == 1


@pytest.mark.parametrize("kind", ["kpi", "table"])
def test_non_axis_views_allow_distinct_units_per_column(kind):
    chart = {"type": kind, "title": "经营总览", "query_id": "q", "y": ["orders", "aov"],
             "series": [{"column": "orders", "unit": "单"}, {"column": "aov", "unit": "元/单"}]}
    actual = validate_charts([chart], [query([["2025", 10, 250, "甲"]])])[0]
    assert actual["type"] == kind and not actual.get("validation_error")
