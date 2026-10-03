"""Publication-time metadata references; does not open databases or call models."""
from copy import deepcopy
from pathlib import Path

import pytest
import test_enterprise

from insight.enterprise import validate_definition
from insight.scenarios import load_scenarios


@pytest.fixture
def definition():
    return {
        "tables": [
            {"name": "orders", "grain": "order", "columns": {"id": "INT", "day": "DATE", "region": "TEXT", "amount": "DOUBLE"}},
            {"name": "refunds", "grain": "refund", "columns": {"order_id": "INT", "day": "DATE", "amount": "DOUBLE"}},
            {"name": "periods", "grain": "period", "columns": {"start": "DATE", "end": "DATE", "region": "TEXT"}},
        ],
        "metrics": [{"id": "sales", "name": "Sales", "formula": "SUM(amount)", "tables": ["orders"], "grain": "order",
                     "time_column": "orders.day", "filters": [], "unit": "currency", "additivity": "additive"}],
        "relations": [{"left_table": "orders", "left_column": "id", "right_table": "refunds", "right_column": "order_id",
                       "cardinality": "one_to_many", "additional_keys": [{"left_column": "day", "right_column": "day"}]}],
        "dimensions": [{"id": "region", "table": "orders", "column": "region", "type": "category", "levels": ["region"]}],
        "capabilities": ["profit_bridge", "future_business_capability"],
        "diagnostics": [{"id": "diagnostic", "tool": "profit_bridge", "queries": [
            {"id": "input", "sql": "SELECT SUM(amount) AS amount FROM orders", "metric_ids": ["sales"]}],
            "config": {"query_id": "input", "complete_snapshot": True}}],
        "dashboard_templates": [{"id": "board", "cards": [{"id": "card", "title": "Sales", "chart": {"type": "kpi", "y": ["amount"]},
            "query": {"id": "input", "sql": "SELECT SUM(amount) AS amount FROM orders", "metric_ids": ["sales"]},
            "analysis_recipe": {"tool": "profit_bridge", "config": {"query_id": "input"}}}]}],
    }


def temporal():
    return {"type": "temporal", "left_table": "orders", "left_column": "day", "right_table": "periods",
            "right_column": "start", "end_column": "end", "cardinality": "many_to_many", "preaggregation": "aggregate per period"}


def test_all_three_actual_enterprise_models_remain_publishable():
    scenes = load_scenarios(Path(__file__).resolve().parents[2] / "scenarios")
    assert set(scenes) == {"ecommerce", "retail", "saas"}
    for scene in scenes.values():
        validate_definition(scene)


def test_optional_sections_and_ordinary_card_query_id_can_be_omitted(definition):
    minimal = {key: value for key, value in definition.items() if key in {"tables", "metrics", "relations"}}
    validate_definition(minimal)
    card = definition["dashboard_templates"][0]["cards"][0]
    card.pop("id")
    card["query"].pop("id")
    card["analysis_recipe"]["config"] = {}
    validate_definition(definition)


def test_capabilities_are_extensible_not_tool_permissions(definition):
    validate_definition(definition)
    definition["capabilities"] = []
    validate_definition(definition)


def test_query_ids_are_local_to_diagnostic_and_card_input_sets(definition):
    another = deepcopy(definition["diagnostics"][0])
    another["id"] = "another"
    definition["diagnostics"].append(another)  # same input ID, different diagnostic
    card = definition["dashboard_templates"][0]["cards"][0]
    card["queries"] = [deepcopy(card["query"])]  # primary aliases first executed query
    validate_definition(definition)


@pytest.mark.parametrize("flags", [{}, {"end_inclusive": True, "allow_open_end": False}, {"end_inclusive": False, "allow_open_end": True}])
def test_valid_temporal_endpoints_and_optional_flags(definition, flags):
    relation = {**temporal(), **flags, "additional_keys": [{"left_column": "region", "right_column": "region"}]}
    definition["relations"].append(relation)
    validate_definition(definition)
    relation["kind"] = relation.pop("type")
    validate_definition(definition)


@pytest.mark.parametrize("mutation", [
    lambda d: d.update(dimensions="region"),
    lambda d: d["dimensions"].append(deepcopy(d["dimensions"][0])),
    lambda d: d["dimensions"][0].update(id=[]),
    lambda d: d["dimensions"][0].update(table="missing"),
    lambda d: d["dimensions"][0].update(column="missing"),
    lambda d: d["relations"][0].update(cardinality=[]),
    lambda d: d["relations"][0].update(additional_keys="day"),
    lambda d: d["relations"][0]["additional_keys"][0].update(left_column="missing"),
    lambda d: d["relations"][0]["additional_keys"][0].update(right_column="missing"),
    lambda d: d["relations"][0]["additional_keys"][0].pop("right_column"),
    lambda d: d.update(capabilities="profit_bridge"),
    lambda d: d.update(capabilities=[{}]),
    lambda d: d.update(capabilities=[""]),
    lambda d: d["diagnostics"][0].update(tool="arbitrary_python"),
    lambda d: d["diagnostics"][0].update(tool=[]),
    lambda d: d["diagnostics"].append(deepcopy(d["diagnostics"][0])),
    lambda d: d["diagnostics"][0].update(queries=[]),
    lambda d: d["diagnostics"][0]["queries"].append(deepcopy(d["diagnostics"][0]["queries"][0])),
    lambda d: d["diagnostics"][0]["queries"][0].update(id=None),
    lambda d: d["diagnostics"][0]["queries"][0].update(metric_ids=["missing"]),
    lambda d: d["diagnostics"][0].update(config=[]),
    lambda d: d["diagnostics"][0]["config"].update(query_id="missing"),
    lambda d: d["dashboard_templates"][0]["cards"][0]["analysis_recipe"].update(tool="missing"),
    lambda d: d["dashboard_templates"][0]["cards"][0]["analysis_recipe"]["config"].update(query_id="missing"),
    lambda d: d["dashboard_templates"][0]["cards"][0]["analysis_recipe"].update(query_map={"alias": "missing"}),
    lambda d: d["dashboard_templates"][0]["cards"][0].update(queries="input"),
    lambda d: d["dashboard_templates"][0]["cards"][0].update(queries=[{"id": "x", "sql": "SELECT 1"}, {"id": "x", "sql": "SELECT 2"}]),
])
def test_malformed_or_dangling_metadata_is_rejected_as_value_error(definition, mutation):
    mutation(definition)
    with pytest.raises(ValueError):
        validate_definition(definition)


@pytest.mark.parametrize("changes", [{"end_column": "missing"}, {"end_column": None}, {"right_column": "missing"},
                                     {"end_inclusive": "false"}, {"allow_open_end": 1}])
def test_invalid_temporal_references_and_boolean_types_are_rejected(definition, changes):
    definition["relations"].append({**temporal(), **changes})
    with pytest.raises(ValueError):
        validate_definition(definition)


def test_recipe_must_select_its_own_input_not_another_diagnostic(definition):
    another = deepcopy(definition["diagnostics"][0])
    another["id"] = "other"
    another["queries"][0]["id"] = "other_input"
    another["config"]["query_id"] = "other_input"
    definition["diagnostics"].append(another)
    definition["diagnostics"][0]["config"]["query_id"] = "other_input"
    with pytest.raises(ValueError, match="query_id"):
        validate_definition(definition)


def test_rejected_publication_does_not_change_current_model(tmp_path):
    service = test_enterprise.enterprise.__wrapped__(tmp_path)
    definition = service.catalog("operations")
    definition["dimensions"][0]["column"] = "missing"
    draft = service.create_model("operations", definition)
    with pytest.raises(ValueError, match="维度"):
        service.publish(draft["id"])
    assert service.domain("operations")["model_version"] == "2"
    assert service.store.get("model", draft["id"])["status"] == "draft"
