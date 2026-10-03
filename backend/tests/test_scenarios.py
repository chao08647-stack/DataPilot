"""Offline acceptance: scenario data, independent gold results and actual skill routing."""
import os
from pathlib import Path

import duckdb
import pytest

from insight.scenarios import _equal_rows, generate_all, load_scenarios
from insight.skills import SkillRegistry
from insight.sql import validate_sql

ROOT = Path(__file__).resolve().parents[2]
SCENARIOS = load_scenarios(ROOT / "scenarios", profile="enterprise")
CASES = [(name, case) for name, scene in SCENARIOS.items() for case in scene["evaluation"]]
EXAMPLES = [(name, case) for name, scene in SCENARIOS.items() for case in scene["examples"]]
REGISTRY = SkillRegistry(ROOT / "skills")


@pytest.fixture(scope="module")
def databases(tmp_path_factory):
    target = Path(os.environ["INSIGHT_TEST_DATA_DIR"]) if os.getenv("INSIGHT_TEST_DATA_DIR") else tmp_path_factory.mktemp("scenarios")
    return generate_all(ROOT / "scenarios", target, profile="enterprise")


@pytest.mark.parametrize("name,case", CASES, ids=[case["id"] for _, case in CASES])
def test_gold_query_returns_checked_in_expected_result(databases, name, case):
    assert case["expected"], "A gold query cannot silently have missing expected output"
    scene = SCENARIOS[name]
    safe_sql = validate_sql(case["sql"], scene, [table["name"] for table in scene["tables"]])
    with duckdb.connect(str(databases[name]), read_only=True) as db:
        actual = db.execute(safe_sql).fetchall()
    assert _equal_rows(actual, case["expected"])


@pytest.mark.parametrize("name,example", EXAMPLES, ids=[f"{name}-{e['id']}" for name, e in EXAMPLES])
def test_replay_is_real_query_with_bound_chart_columns(databases, name, example):
    scene = SCENARIOS[name]
    sql = validate_sql(example["sql"], scene, [table["name"] for table in scene["tables"]])
    with duckdb.connect(str(databases[name]), read_only=True) as db:
        query = db.execute(sql)
        names = [column[0] for column in query.description]
        assert example["x"] in names
        assert all(column in names for column in example["y"])
        assert len(query.fetchall()) > 0


@pytest.mark.parametrize("name", list(SCENARIOS))
def test_scenario_contract_and_referential_integrity(databases, name):
    scene = SCENARIOS[name]
    assert scene["date_range"] == {"start": "2024-01-01", "end": "2025-12-31"}
    assert len(scene["tables"]) >= 12
    assert len(scene["repair_experiences"]) == 4
    assert len(scene["business_knowledge"]) >= 4
    assert len(scene["preferences"]) == 3
    assert len(scene["evaluation"]) >= 20
    assert len(scene["clarifications"]) == 2
    schema = {table["name"]: table["columns"] for table in scene["tables"]}
    for metric in scene["metrics"]:
        assert all(table in schema for table in metric["tables"])
        assert all(column in {c for t in metric["tables"] for c in schema[t]} for column in metric["columns"])
        assert all(metric[key] for key in ["id", "name", "expression", "description", "grain", "unit", "time_column"])
    with duckdb.connect(str(databases[name]), read_only=True) as db:
        for relation in scene["relations"]:
            lt, lc, rt, rc = (relation[key] for key in ("left_table", "left_column", "right_table", "right_column"))
            keys = [{"left_column": lc, "right_column": rc}, *relation.get("additional_keys", [])]
            assert all(key["left_column"] in schema[lt] and key["right_column"] in schema[rt] for key in keys)
            if relation.get("type") == "temporal":
                assert relation["end_column"] in schema[rt]
                continue  # Range overlap is not a mandatory foreign-key relationship.
            condition = " AND ".join(f'l."{key["left_column"]}"=r."{key["right_column"]}"' for key in keys)
            if lt == "inventory_periods":
                continue  # Opening snapshots may legitimately precede dataset coverage.
            count = db.execute(f'SELECT COUNT(*) FROM "{lt}" l LEFT JOIN "{rt}" r ON {condition} WHERE r."{rc}" IS NULL').fetchone()[0]
            assert count == 0


def test_existing_valid_database_is_not_rewritten(databases):
    directory = next(iter(databases.values())).parent
    before = {key: (path.stat().st_mtime_ns, path.stat().st_size) for key, path in databases.items()}
    assert generate_all(ROOT / "scenarios", directory, profile="enterprise") == databases
    after = {key: (path.stat().st_mtime_ns, path.stat().st_size) for key, path in databases.items()}
    assert before == after


def test_existing_invalid_database_is_preserved(tmp_path):
    existing = tmp_path / "ecommerce-enterprise-2.duckdb"
    existing.write_bytes(b"not-a-database-user-content")
    with pytest.raises(FileExistsError, match="Refusing to replace"):
        generate_all(ROOT / "scenarios", tmp_path, profile="enterprise")
    assert existing.read_bytes() == b"not-a-database-user-content"


def test_data_generation_is_reproducible(databases, tmp_path):
    second = generate_all(ROOT / "scenarios", tmp_path, profile="enterprise")
    for name, scene in SCENARIOS.items():
        with duckdb.connect(str(databases[name]), read_only=True) as first, duckdb.connect(str(second[name]), read_only=True) as other:
            for table in scene["tables"]:
                sql = f'SELECT COUNT(*),SUM(hash(t)) FROM "{table["name"]}" t'
                assert first.execute(sql).fetchall() == other.execute(sql).fetchall()


@pytest.mark.parametrize("skill", REGISTRY.skills, ids=lambda s: s["id"])
def test_every_skill_has_positive_and_negative_routing(skill):
    scenario = "ecommerce" if "*" in skill["scenarios"] else skill["scenarios"][0]
    node = skill["allowed_nodes"][0]
    positive = REGISTRY.select(node, scenario, skill["tags"])
    assert skill["id"] in [s["id"] for s in positive]
    wrong_node = next(n for n in ["intent", "discovery", "sql", "repair", "analysis", "review", "visualization"] if n not in skill["allowed_nodes"])
    assert skill["id"] not in [s["id"] for s in REGISTRY.select(wrong_node, scenario, skill["tags"])]
    if not skill["base"]:
        assert skill["id"] not in [s["id"] for s in REGISTRY.select(node, scenario, ["unrelated-tag"])]
    if "*" not in skill["scenarios"]:
        other = next(name for name in SCENARIOS if name not in skill["scenarios"])
        assert skill["id"] not in [s["id"] for s in REGISTRY.select(node, other, skill["tags"])]
    assert "正向示例" in skill["instructions"] and "负向示例" in skill["instructions"]
    assert len(skill["instructions"]) > 250


def test_skill_registry_caps_context_and_does_not_grant_tools():
    assert len(REGISTRY.skills) == 18
    for node in ["intent", "discovery", "sql", "repair", "analysis", "review", "visualization"]:
        for name in SCENARIOS:
            selected = REGISTRY.select(node, name, [tag for s in REGISTRY.skills for tag in s["tags"]])
            assert len([s for s in selected if s["base"]]) == 1
            assert len([s for s in selected if not s["base"]]) <= 2
            assert all(node in s["allowed_nodes"] for s in selected)


@pytest.mark.parametrize("name", ["ecommerce", "retail"])
def test_cross_fact_relation_declares_complete_grain(name):
    composites = [r for r in SCENARIOS[name]["relations"] if r.get("additional_keys")]
    relation = next(r for r in composites if r["left_table"] in {"orders", "transactions"})
    assert len(relation["additional_keys"]) == 1
    assert "聚合" in relation["description"]
    assert "日期" in relation["description"]
