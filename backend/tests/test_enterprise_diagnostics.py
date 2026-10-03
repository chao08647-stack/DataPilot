"""Real guarded DuckDB queries and computed dashboards, explicitly without models."""
import os
from pathlib import Path

import pytest

from insight.analytics import run_analysis
from insight.config import Settings
from insight.dashboards import Dashboards
from insight.enterprise import Enterprise, validate_definition
from insight.repository import InMemoryRepository
from insight.scenarios import generate_all, load_scenarios
from insight.sql import execute_query

ROOT = Path(__file__).resolve().parents[2]
SCENARIOS = load_scenarios(ROOT / "scenarios", profile="enterprise")


@pytest.fixture(scope="module")
def paths(tmp_path_factory):
    target = Path(os.environ["INSIGHT_TEST_DATA_DIR"]) if os.getenv("INSIGHT_TEST_DATA_DIR") else tmp_path_factory.mktemp("enterprise-data")
    return generate_all(ROOT / "scenarios", target, profile="enterprise")


@pytest.mark.parametrize("domain", SCENARIOS)
def test_semantic_template_can_be_published(domain):
    validate_definition(SCENARIOS[domain])


@pytest.mark.parametrize("domain,diagnostic", [(domain, d) for domain, s in SCENARIOS.items() for d in s["diagnostics"]], ids=lambda x: x["id"] if isinstance(x, dict) else x)
def test_diagnostic_has_real_query_evidence_and_reconciliation(paths, domain, diagnostic):
    scene = SCENARIOS[domain]
    queries = [execute_query(paths[domain], q["sql"], scene, [t["name"] for t in scene["tables"]], q["id"]) for q in diagnostic["queries"]]
    result = run_analysis(diagnostic["tool"], queries, {**diagnostic["config"], "query_id": queries[0]["id"]})
    assert result["status"] == "ok", result
    assert not any(q["truncated"] for q in queries)
    assert result["evidence_ids"] == [queries[0]["id"]]
    if diagnostic["tool"] == "inventory_reconciliation":
        assert result["reconciliation"]["absolute_residual"] == 5
        anomalies = [r for r in result["series"] if r["residual"]]
        assert [(r["store_id"], r["product_id"], r["residual"]) for r in anomalies] == [("3", "2", -5)]
        assert any(r["missing_snapshots"] for r in result["series"])
    else:
        assert result["reconciliation"]["balanced"]
    if diagnostic["tool"] == "profit_bridge":
        assert result["reconciliation"]["closing"] == pytest.approx(41186.19)
    if diagnostic["tool"] == "conversion_funnel":
        assert {f["name"]: f["value"] for f in result["facts"]}["付费转化率变化"] == -20
    if diagnostic["tool"] == "mrr_bridge":
        assert result["reconciliation"]["closing"] == 36800


@pytest.mark.parametrize("domain", SCENARIOS)
def test_dashboard_templates_validate_and_refresh_without_models(paths, domain, tmp_path):
    enterprise = Enterprise(InMemoryRepository(), SCENARIOS, paths, Settings(_env_file=None, data_dir=tmp_path, postgres_uri=""))
    enterprise.setup()
    service = Dashboards(enterprise)
    template = SCENARIOS[domain]["dashboard_templates"][0]
    board = service.create({"domain_id": domain, "template_id": template["id"], "title": "Offline evidence verification"})
    assert len(board["cards"]) >= 6
    result = service.refresh(board["id"])
    assert all(c["snapshot"]["status"] == "ready" for c in result["cards"]), [(c["id"], c["snapshot"].get("error")) for c in result["cards"]]
    assert any(c["snapshot"].get("calculation") for c in result["cards"])
    assert any(c["chart"]["type"] == "kpi" for c in result["cards"])
    filtered = service.refresh(board["id"], {"date_from": "2025-10-01", "date_to": "2025-11-30"})
    for card in filtered["cards"]:
        if not card["query"]["filter_bindings"].get("date_from"):
            assert card["snapshot"]["status"] == "failed"
            assert card["snapshot"]["stale"]
        else:
            assert card["snapshot"]["status"] == "ready", card["snapshot"]
    partial = service.refresh(board["id"], {"date_from": "2025-10-02"})
    assert any(c["snapshot"]["status"] == "failed" for c in partial["cards"])


def test_old_business_files_are_not_reused(paths):
    assert all(path.name.endswith("-enterprise-2.duckdb") for path in paths.values())


def test_no_gold_answers_in_runtime_domain_catalog(paths, tmp_path):
    enterprise = Enterprise(InMemoryRepository(), SCENARIOS, paths, Settings(_env_file=None, data_dir=tmp_path, postgres_uri=""))
    enterprise.setup()
    for domain in SCENARIOS:
        catalog = enterprise.catalog(domain)
        assert "evaluation" not in catalog and "repair_experiences" not in catalog
