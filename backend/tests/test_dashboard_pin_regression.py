"""Pinned chart identity survives deterministic refresh; no model calls."""
from copy import deepcopy

from test_enterprise import enterprise as enterprise_fixture

from insight.dashboards import Dashboards

enterprise = enterprise_fixture


def pinned(enterprise, chart, result, output="series"):
    boards = Dashboards(enterprise)
    board = boards.create({"title": "chart regression", "domain_id": "operations"})
    raw = enterprise.execute("operations", "SELECT day,region,amount FROM orders", ["orders"], "raw")
    rows = result["series"] if output == "series" else next(table["rows"] for table in result["tables"] if table["id"] == output)
    columns = list(rows[0])
    derived = {"id": "derived", "columns": columns, "rows": [[row[key] for key in columns] for row in rows],
               "derived": True, "recipe_output": output, "truncated": False, "evidence_ids": ["raw"], "sql": ""}
    calculation = {"tool": "period_overview", "status": "ok", "derived_query_id": "derived" if output == "series" else "unused",
                   "output_tables": [{"id": output, "query_id": "derived"}],
                   "recipe": {"tool": "period_overview", "config": {"query_id": "source"}, "query_map": {"source": "raw"}}}
    run = enterprise.repository.create_run("operations", "test pinned chart")
    enterprise.repository.update_run(run["run_id"], status="completed", artifacts={
        "model_version": board["model_version"], "partial": False, "queries": [raw, derived],
        "calculations": [calculation], "charts": [{**chart, "query_id": "derived"}]})
    boards.pin(board["id"], {"run_id": run["run_id"], "query_id": "derived"})
    return boards, board


def test_refresh_keeps_selected_combo_series_and_two_axis_units(enterprise, monkeypatch):
    rows = [{"period": "2025-01", "sales": 10., "orders": 2}, {"period": "2025-02", "sales": 20., "orders": 4}]
    result = {"status": "ok", "series": rows, "tables": [],
              "chart": {"type": "line", "x": "period", "y": ["sales"], "unit": "元"}}
    chart = {"type": "combo", "title": "双轴经营", "x": "period", "y": ["sales", "orders"], "unit": None,
             "series": [{"column": "sales", "type": "bar", "axis": "left", "unit": "元"},
                        {"column": "orders", "type": "line", "axis": "right", "unit": "单"}]}
    boards, board = pinned(enterprise, chart, result)
    monkeypatch.setattr("insight.analytics.run_analysis", lambda *_: deepcopy(result))
    snapshot = boards.refresh(board["id"])["cards"][0]["snapshot"]
    assert snapshot["status"] == "ready", snapshot
    assert snapshot["chart"]["type"] == "combo"
    assert snapshot["chart"]["y"] == ["sales", "orders"]
    assert snapshot["chart"]["series"] == chart["series"]
    assert snapshot["chart"]["unit"] is None


def test_secondary_channel_bar_is_not_overwritten_by_primary_waterfall(enterprise, monkeypatch):
    result = {"status": "ok", "series": [{"component": "gross", "delta": 10.}],
              "chart": {"type": "waterfall", "x": "component", "y": ["delta"], "start_value": 100., "unit": "元"},
              "tables": [{"id": "channel_contribution", "rows": [{"segment": "east", "profit_delta": 4.}, {"segment": "west", "profit_delta": 6.}],
                          "chart": {"type": "bar", "x": "segment", "y": ["profit_delta"], "unit": "元"}}]}
    chart = {"type": "horizontal_bar", "title": "渠道利润变化", "x": "segment", "y": ["profit_delta"], "unit": "元"}
    boards, board = pinned(enterprise, chart, result, "channel_contribution")
    monkeypatch.setattr("insight.analytics.run_analysis", lambda *_: deepcopy(result))
    snapshot = boards.refresh(board["id"])["cards"][0]["snapshot"]
    assert snapshot["status"] == "ready", snapshot
    assert snapshot["chart"]["type"] == "horizontal_bar"
    assert snapshot["chart"]["x"] == "segment"
    assert snapshot["chart"]["y"] == ["profit_delta"]
    assert snapshot["query"]["rows"] == [["east", 4.], ["west", 6.]]


def test_waterfall_baseline_updates_from_current_recalculation(enterprise, monkeypatch):
    result = {"status": "ok", "series": [{"component": "gross", "delta": 10.}], "tables": [],
              "chart": {"type": "waterfall", "x": "component", "y": ["delta"], "start_value": 150., "unit": "元"}}
    chart = {"type": "waterfall", "title": "最新基期", "x": "component", "y": ["delta"], "start_value": 100., "unit": "元"}
    boards, board = pinned(enterprise, chart, result)
    monkeypatch.setattr("insight.analytics.run_analysis", lambda *_: deepcopy(result))
    snapshot = boards.refresh(board["id"])["cards"][0]["snapshot"]
    assert snapshot["status"] == "ready", snapshot
    assert snapshot["chart"]["start_value"] == 150.
