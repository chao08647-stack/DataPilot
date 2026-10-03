from copy import deepcopy

import duckdb
import pytest

from insight.config import Settings
from insight.dashboards import Dashboards, filtered_query
from insight.enterprise import Enterprise, SourceInput, validate_definition
from insight.repository import InMemoryRepository


@pytest.fixture
def enterprise(tmp_path):
    path = tmp_path / "operations.duckdb"
    with duckdb.connect(str(path)) as db:
        db.execute("CREATE TABLE orders(day DATE, region VARCHAR, amount DOUBLE)")
        db.execute("INSERT INTO orders VALUES ('2025-01-01','east',10),('2025-02-01','west',20),('2025-02-02','east',5)")
    template = {"id": "operations", "title": "通用经营", "description": "测试非内置行业", "schema_version": "2", "date_range": {"start": "2025-01-01", "end": "2025-12-31"},
        "tables": [{"name": "orders", "grain": "订单", "columns": {"day": "DATE", "region": "VARCHAR", "amount": "DOUBLE"}}],
        "relations": [], "metrics": [{"id": "amount", "name": "金额", "formula": "SUM(amount)", "tables": ["orders"], "grain": "订单", "time_column": "orders.day", "unit": "元", "filters": [], "additivity": "additive"}],
        "dimensions": [{"id": "region", "column": "region", "table": "orders", "type": "string"}], "examples": [],
        "dashboard_templates": [{"id": "operations-board", "title": "经营", "cards": [{"title": "销售", "query": {"sql": "SELECT day,region,SUM(amount) AS amount FROM orders GROUP BY day,region ORDER BY day", "metric_ids": ["amount"], "filter_bindings": {"date_from": "day", "date_to": "day", "region": "region"}}, "chart": {"type": "bar", "x": "region", "y": ["amount"]}}]}]}
    repo = InMemoryRepository()
    service = Enterprise(repo, {"operations": template}, {"operations": path}, Settings(_env_file=None, data_dir=tmp_path, postgres_uri=""))
    service.setup()
    return service


def test_fourth_domain_without_enum_or_workflow_edit(enterprise):
    assert enterprise.domain("operations")["model_version"] == "2"
    assert enterprise.catalog("operations")["synthetic"]
    assert enterprise.require_model_access("operations")["id"] == "operations"
    from insight.models import RunCreate
    assert RunCreate(domain_id="anything-new", question="测试查询").scenario_id == "anything-new"


def test_model_consent_is_explicit_and_secret_reference_only(enterprise):
    body = {"name": "Readonly", "kind": "postgres", "config": {"host": "127.0.0.1", "database": "business", "user": "reader", "password_env": "INSIGHT_SOURCE_BUSINESS_PASSWORD", "allowed_tables": ["public.orders"]}}
    source = enterprise.create_source(body)
    domain = enterprise.create_domain({"id": "external", "title": "外部数据", "data_source_id": source["id"]})
    with pytest.raises(ValueError, match="未授权"):
        enterprise.require_model_access(domain["id"])
    enterprise.update_source(source["id"], {"model_access": {"metadata": True, "results": True}})
    assert enterprise.require_model_access("external")
    for extra in ({"password": "secret"}, {"password_env": "INSIGHT_LLM_API_KEY"}, {"allowed_tables": []}):
        bad = deepcopy(body)
        bad["config"].update(extra)
        with pytest.raises(ValueError):
            SourceInput.model_validate(bad)


def test_external_duckdb_path_not_arbitrary(enterprise, tmp_path):
    with pytest.raises(ValueError, match="data"):
        enterprise.create_source({"name": "no", "kind": "duckdb", "config": {"path": str(tmp_path.parent / "secret.duckdb")}})


def test_model_publish_validation_and_active_run_gate(enterprise):
    definition = enterprise.catalog("operations")
    validate_definition(definition)
    draft = enterprise.create_model("operations", definition)
    run = enterprise.repository.create_run("operations", "查询金额")
    with pytest.raises(ValueError, match="未结束"):
        enterprise.publish(draft["id"])
    enterprise.repository.update_run(run["run_id"], status="completed")
    assert enterprise.publish(draft["id"])["status"] == "published"
    assert enterprise.domain("operations")["model_version"] == draft["version"]
    assert enterprise.catalog("operations", "2")["schema_version"] == "2"


def test_board_refresh_bound_filters_and_persisted_definitions(enterprise):
    boards = Dashboards(enterprise)
    board = boards.create({"title": "经营看板", "domain_id": "operations", "template_id": "operations-board"})
    refreshed = boards.refresh(board["id"], {"date_from": "2025-02-01", "region": "east"})
    snapshot = refreshed["cards"][0]["snapshot"]
    assert snapshot["status"] == "ready", snapshot
    assert len(snapshot["query"]["rows"]) == 1
    assert float(snapshot["query"]["rows"][0][-1]) == 5
    restored = Dashboards(enterprise).get(board["id"])
    assert restored["filters"]["region"] == "east"
    assert restored["cards"][0]["query"]["sql"] == board["cards"][0]["query"]["sql"]
    drill = boards.drilldown(board["id"], board["cards"][0]["id"], "region", "west")
    assert drill["context"]["filters"]["region"] == "west"


def test_filters_use_bound_values_not_interpolated_sql():
    attack = "x' OR 1=1 --"
    sql, parameters, ignored = filtered_query("SELECT region,SUM(amount) AS amount FROM orders GROUP BY region", {"region": "region"}, {"region": attack, "date_from": "2025-01-01"}, "duckdb")
    assert attack not in sql and parameters == [attack] and ignored == ["date_from"]


def test_refresh_failure_retains_prior_evidence_and_flags_stale(enterprise, monkeypatch):
    boards = Dashboards(enterprise)
    board = boards.create({"title": "保留证据", "domain_id": "operations", "template_id": "operations-board"})
    first = boards.refresh(board["id"])
    def unavailable(*args, **kwargs):
        raise ConnectionError("private host and credentials must not leak")
    monkeypatch.setattr(enterprise, "execute", unavailable)
    second = boards.refresh(board["id"], {"region": "east"})
    snapshot = second["cards"][0]["snapshot"]
    assert snapshot["stale"] and snapshot["status"] == "failed"
    assert snapshot["query"] == first["cards"][0]["snapshot"]["query"]
    assert "private" not in snapshot["error"]


def test_no_silent_model_version_upgrade_and_no_client_evidence(enterprise):
    boards = Dashboards(enterprise)
    board = boards.create({"title": "版本", "domain_id": "operations", "template_id": "operations-board"})
    refreshed = boards.refresh(board["id"])
    tampered = deepcopy(refreshed["cards"])
    tampered[0]["snapshot"] = {"query": {"rows": [[99999999]]}}
    updated = boards.update(board["id"], {"cards": tampered})
    assert updated["cards"][0]["snapshot"] == refreshed["cards"][0]["snapshot"]
    draft = enterprise.create_model("operations", enterprise.catalog("operations"))
    enterprise.publish(draft["id"])
    with pytest.raises(ValueError, match="版本已变化"):
        boards.refresh(board["id"])


def test_pin_requires_same_domain_success_and_final_review(enterprise):
    boards = Dashboards(enterprise)
    board = boards.create({"title": "保存", "domain_id": "operations"})
    run = enterprise.repository.create_run("operations", "查金额")
    enterprise.repository.update_run(run["run_id"], status="completed", artifacts={"partial": True})
    with pytest.raises(ValueError, match="部分结果"):
        boards.pin(board["id"], {"run_id": run["run_id"], "query_id": "q1"})


def test_unapplied_filter_cannot_look_like_filtered_result(enterprise):
    boards = Dashboards(enterprise)
    board = boards.create({"title": "不适用", "domain_id": "operations"})
    board = boards.pin(board["id"], {"title": "总额", "query": {"sql": "SELECT SUM(amount) AS amount FROM orders", "metric_ids": ["amount"]}, "chart": {"type": "kpi", "y": ["amount"]}})
    result = boards.refresh(board["id"], {"region": "east"})
    assert result["cards"][0]["snapshot"]["status"] == "failed"
    assert "不适用筛选" in result["cards"][0]["snapshot"]["error"]
    assert "query" not in result["cards"][0]["snapshot"]


def test_qualified_remote_schema_can_be_saved_without_opening_connection(enterprise):
    catalog = enterprise.catalog("operations")
    catalog["dialect"] = "postgres"
    catalog["tables"][0]["name"] = "sales.orders"
    catalog["metrics"][0]["tables"] = ["sales.orders"]
    card = {"title": "外部指标", "query": {"sql": "SELECT SUM(amount) AS amount FROM sales.orders", "metric_ids": ["amount"]}, "chart": {"type": "kpi", "y": ["amount"]}}
    assert Dashboards(enterprise).validate_card(card, catalog)["query"]["sql"]


def test_pin_executed_star_wrapper_retains_filterable_columns(enterprise):
    boards = Dashboards(enterprise)
    board = boards.create({"title": "实际结果", "domain_id": "operations"})
    sql = "SELECT * FROM (SELECT region, SUM(amount) AS amount FROM orders GROUP BY region) AS diagnostic_input"
    query = enterprise.execute("operations", sql, ["orders"], "q1")
    query.update(source_sql=sql, metric_ids=["amount"])
    run = enterprise.repository.create_run("operations", "按区域查金额")
    enterprise.repository.update_run(run["run_id"], status="completed", artifacts={
        "model_version": board["model_version"], "partial": False, "queries": [query],
        "charts": [{"query_id": "q1", "title": "区域金额", "type": "table", "y": []}]})
    boards.pin(board["id"], {"run_id": run["run_id"], "query_id": "q1"})
    snapshot = boards.refresh(board["id"], {"region": "east"})["cards"][0]["snapshot"]
    assert snapshot["status"] == "ready", snapshot.get("error")
    assert snapshot["query"]["rows"] == [r for r in query["rows"] if r[0] == "east"]
