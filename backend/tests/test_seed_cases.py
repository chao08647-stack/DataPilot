"""The built-in experience catalog only contains verified executable fixtures."""
from copy import deepcopy
from pathlib import Path

import duckdb
import pytest

from insight.scenarios import load_scenarios, validate_repair_seed
from insight.sql import error_category, validate_sql

ROOT = Path(__file__).resolve().parents[2]
SCENARIOS = load_scenarios(ROOT / "scenarios", profile="enterprise")
SEEDS = [seed for scene in SCENARIOS.values() for seed in scene["repair_experiences"]]


@pytest.mark.parametrize("seed", SEEDS, ids=lambda seed: seed["id"])
def test_repair_seed_fails_then_repairs_to_exact_expected(seed):
    assert validate_repair_seed(seed)
    scene = next(scene for scene in SCENARIOS.values() if seed in scene["repair_experiences"])
    validate_sql(seed["fixed_sql"], scene, seed["tables"])
    with duckdb.connect(":memory:") as db:
        for statement in seed["fixture_sql"]:
            db.execute(statement)
        if seed["error_category"] == "semantic":
            # These statements execute but have the wrong business result.
            validate_sql(seed["broken_sql"], scene, seed["tables"])
            wrong = [list(row) for row in db.execute(seed["broken_sql"]).fetchall()]
            assert wrong != seed["expected"]
        else:
            with pytest.raises(duckdb.Error) as exc:
                db.execute(seed["broken_sql"])
            assert error_category(str(exc.value)) == seed["error_category"]
    assert "SELECT " not in seed["content"].upper()
    assert "CREATE TABLE" not in seed["content"].upper()


@pytest.mark.parametrize("scene", list(SCENARIOS.values()), ids=lambda scene: scene["id"])
def test_each_scene_has_two_execution_two_semantic_and_distinct_holdout(scene):
    seeds = scene["repair_experiences"]
    assert sum(seed["error_category"] == "semantic" for seed in seeds) == 2
    assert sum(seed["error_category"] != "semantic" for seed in seeds) == 2
    fixture_queries = {seed[key] for seed in seeds for key in ["broken_sql", "fixed_sql"]}
    assert not fixture_queries.intersection({case["sql"] for case in scene["evaluation"]})


def test_wrong_expected_answer_never_marks_experience_verified():
    seed = deepcopy(SEEDS[0])
    seed["expected"] = [["wrong", -999]]
    assert not validate_repair_seed(seed)


def test_already_correct_sql_is_not_a_verified_repair():
    seed = deepcopy(next(seed for seed in SEEDS if seed["error_category"] == "semantic"))
    seed["broken_sql"] = seed["fixed_sql"]
    assert not validate_repair_seed(seed)


def test_execution_failure_cannot_masquerade_as_semantic_seed():
    seed = deepcopy(next(seed for seed in SEEDS if seed["error_category"] != "semantic"))
    seed["error_category"] = "semantic"
    assert not validate_repair_seed(seed)
