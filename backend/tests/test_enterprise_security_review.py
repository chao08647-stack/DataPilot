"""Review regressions: schema scopes, honest filtering, version and model-consent boundaries."""
from copy import deepcopy
from types import SimpleNamespace

import pytest
import test_enterprise

from insight.dashboards import Dashboards
from insight.runtime import Runtime


@pytest.fixture
def enterprise(tmp_path):
    return test_enterprise.enterprise.__wrapped__(tmp_path)


def test_remote_schema_card_preflight_accepts_published_physical_table(enterprise):
    catalog = enterprise.catalog("operations")
    catalog["dialect"] = "postgres"
    catalog["tables"][0]["name"] = "analytics.orders"
    catalog["tables"][0]["schema"] = "analytics"
    catalog["metrics"][0]["tables"] = ["analytics.orders"]
    card = {"title": "批准表", "query": {"sql": "SELECT SUM(amount) AS amount FROM analytics.orders", "metric_ids": ["amount"]},
            "chart": {"type": "kpi", "y": ["amount"]}}
    assert Dashboards(enterprise).validate_card(card, catalog)["query"]["sql"]


def test_unsupported_date_filter_cannot_claim_successful_filtered_snapshot(enterprise):
    boards = Dashboards(enterprise)
    board = boards.create({"title": "不允许假筛选", "domain_id": "operations"})
    boards.pin(board["id"], {"title": "全量金额", "query": {"sql": "SELECT SUM(amount) AS amount FROM orders", "metric_ids": ["amount"]},
                             "chart": {"type": "kpi", "y": ["amount"]}})
    snapshot = boards.refresh(board["id"], {"date_from": "2025-02-01"})["cards"][0]["snapshot"]
    # Fixture Feb sum is 25, all-time sum is 35. Returning 35 with ready/false
    # makes the front end say date filter was applied, although none was bound.
    assert snapshot["status"] == "failed" and snapshot["stale"]


def test_legacy_unversioned_evidence_must_not_inherit_new_metric_definition(enterprise):
    repo = enterprise.repository
    run = repo.create_run("operations", "旧口径查金额")
    repo.update_run(run["run_id"], status="completed", artifacts={
        "queries": [{"id": "q1", "sql": "SELECT SUM(amount) AS amount FROM orders", "columns": ["amount"], "rows": [[35]], "metric_ids": ["amount"]}],
        "charts": [{"type": "kpi", "query_id": "q1", "title": "旧金额口径", "y": ["amount"]}],
        "partial": False})
    definition = deepcopy(enterprise.catalog("operations"))
    definition["metrics"][0]["formula"] = "SUM(amount)*2"
    model = enterprise.create_model("operations", definition)
    enterprise.publish(model["id"])
    boards = Dashboards(enterprise)
    board = boards.create({"title": "新口径看板", "domain_id": "operations"})
    with pytest.raises(ValueError):
        boards.pin(board["id"], {"run_id": run["run_id"], "query_id": "q1"})


@pytest.mark.asyncio
async def test_revoking_consent_before_completion_prevents_new_repair_embedding(enterprise):
    run = enterprise.create_run("operations", "模型调用后撤回授权", None, "2", {})
    sent = []

    async def graph_result(state, config):
        # Equivalent to revocation while the final model HTTP call is in flight.
        source_id = enterprise.domain("operations")["data_source_id"]
        enterprise.update_source(source_id, {"model_access": {"metadata": False, "results": False}})
        return {**state, "analysis": {"summary": "查询完成"}, "partial": False,
                "repair_history": [{"category": "schema"}], "discovery": {"tables": ["orders"]},
                "plan": {"queries": [{"rationale": "已验证修复策略"}]}}

    async def remember(*args, **kwargs):
        # MemoryService.remember_repair sends newly extracted content to Embedder.
        sent.append(args)

    runtime = SimpleNamespace(repository=enterprise.repository, enterprise=enterprise,
                              graph=SimpleNamespace(ainvoke=graph_result), memory=SimpleNamespace(remember_repair=remember),
                              settings=enterprise.settings)
    await Runtime.drive(runtime, run)
    assert enterprise.repository.get_run(run["run_id"])["status"] == "completed"
    assert not sent


@pytest.mark.asyncio
async def test_builtin_seed_is_not_relabeled_for_user_edited_semantics(enterprise):
    seeded = []

    async def seed(scene):
        seeded.append(scene["schema_version"])
        return {"seeded": 0}

    runtime = SimpleNamespace(scenarios=enterprise.templates, enterprise=enterprise,
                              memory=SimpleNamespace(seed=seed), seed_report=[])
    await Runtime.seed_template(runtime, "operations")
    assert seeded == ["2"]
    changed = enterprise.catalog("operations")
    changed["metrics"][0]["formula"] = "SUM(amount)*2"
    draft = enterprise.create_model("operations", changed)
    enterprise.publish(draft["id"])
    assert (await Runtime.seed_template(runtime, "operations"))["skipped"] == "published_model_differs_from_template"
    assert seeded == ["2"]


@pytest.mark.asyncio
async def test_revoked_template_can_start_without_sending_seed_embeddings(enterprise):
    source = enterprise.domain("operations")["data_source_id"]
    enterprise.update_source(source, {"model_access": {"metadata": False, "results": False}})
    runtime = SimpleNamespace(scenarios=enterprise.templates, enterprise=enterprise, memory=None, seed_report=[])
    assert (await Runtime.seed_template(runtime, "operations"))["skipped"] == "model_access_not_authorized"
