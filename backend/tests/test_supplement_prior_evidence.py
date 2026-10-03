"""Supplement prompt continuity without graph execution or any real model calls."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from insight.models import Discovery, Intent
from insight.repository import InMemoryRepository
from insight.workflow import Workflow


@pytest.mark.parametrize("node,schema", [("intent", Intent), ("discovery", Discovery)])
@pytest.mark.parametrize("has_previous", [False, True])
@pytest.mark.parametrize("persisted", [False, True])
async def test_prior_evidence_only_for_supplement_with_bounded_sample(node, schema, has_previous, persisted):
    repository = InMemoryRepository()
    run = repository.create_run("retail", "补查已识别风险对象的到货日期")
    rows = [[f"store-product-{i}", i] for i in range(1, 121)]
    query = {"id": "prior-query", "columns": ["object_id", "available"],
             "rows": rows, "truncated": False}
    if persisted:
        query = repository.save_query_result(run["run_id"], query)
    model = SimpleNamespace(complete=AsyncMock(return_value="captured"))
    skills = SimpleNamespace(select=lambda *args, **kwargs: [], render=lambda selected: "")
    workflow = Workflow(None, {"retail": {"id": "retail"}}, {}, repository, None, model, skills)
    state = {"run_id": run["run_id"], "scenario_id": "retail", "question": run["question"],
             "budget": {}, "queries": [], "previous_queries": [query] if has_previous else []}

    assert await workflow.ask(state, schema, node, "fixture") == "captured"
    model.complete.assert_awaited_once()
    payload = model.complete.await_args.args[2]
    if not has_previous:
        assert "prior_evidence" not in payload
        return

    evidence = payload["prior_evidence"]
    sample = evidence["queries"][0]
    assert sample["id"] == "prior-query"
    assert sample["rows"] == rows[:50] and sample["prompt_sampled"] is True
    assert all(len(item["rows"]) <= 50 for item in evidence["queries"])
    statistics = evidence["statistics"][0]
    assert statistics["query_id"] == sample["id"] and statistics["row_count"] == 120
    assert statistics["columns"]["available"]["sum"] == sum(range(1, 121))
    assert len(state["previous_queries"][0]["rows"]) == (50 if persisted else 120)
