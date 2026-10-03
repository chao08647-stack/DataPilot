"""Independent integration review: complete evidence versus persisted previews."""
from types import SimpleNamespace

import pytest

from insight.charts import validate_charts
from insight.models import Analysis
from insight.repository import InMemoryRepository
from insight.workflow import Workflow, initial_state


def fixture():
    repository = InMemoryRepository()
    run = repository.create_run("ecommerce", "review saved evidence")
    workflow = Workflow(SimpleNamespace(max_model_calls=24), {}, {}, repository, None, None, None)
    return workflow, initial_state(run)


def save(workflow, state, *, query_id="q1", truncated=False, count=120):
    return workflow.repository.save_query_result(state["run_id"], {
        "id": query_id, "columns": ["sequence", "amount"], "rows": [[i, i + 1] for i in range(count)],
        "truncated": truncated, "sql": "synthetic-test-only", "metric_ids": [],
    })


def test_model_evidence_is_fifty_rows_but_statistics_use_full_result():
    workflow, state = fixture()
    state["queries"] = [save(workflow, state)]
    evidence = workflow.evidence(state)
    assert len(evidence["queries"][0]["rows"]) == 50
    assert evidence["queries"][0]["prompt_sampled"]
    assert evidence["statistics"][0]["row_count"] == 120
    assert evidence["statistics"][0]["columns"]["amount"]["sum"] == sum(range(1, 121))


def test_delivery_deduplicates_verified_raw_inputs_without_mutating_saved_data():
    workflow, state = fixture()
    state["queries"] = [save(workflow, state, query_id="raw"), {**save(workflow, state, query_id="derived"), "derived": True}]
    state["calculations"] = [{"status": "ok", "evidence_ids": ["raw"]}]
    evidence = workflow.delivery_evidence(state)
    assert evidence["queries"][0]["rows"] == [] and evidence["queries"][0]["review_note"]
    assert len(evidence["queries"][1]["rows"]) == 50
    assert [s["query_id"] for s in evidence["statistics"]] == ["derived"]
    assert "adjacent_changes" not in evidence["statistics"][0]["columns"]["amount"]
    assert len(workflow.repository.get_query_result(state["run_id"], "raw")["rows"]) == 120
    assert len(state["queries"][0]["rows"]) == 50


@pytest.mark.asyncio
async def test_tools_receive_full_results_and_outputs_are_saved_not_checkpointed(monkeypatch):
    workflow, state = fixture()
    state["queries"] = [save(workflow, state)]
    state["diagnostic_bindings"] = [{"id": "diagnostic", "tool": "test-tool", "query_map": {"input": "q1"}, "config": {}}]

    def calculate(tool, inputs, config):
        assert len(inputs[0]["rows"]) == 120
        return {"status": "ok", "evidence_ids": ["input"], "facts": [],
                "series": [{"sequence": i, "value": i * 2} for i in range(120)],
                "tables": [{"id": "details", "rows": [{"id": i} for i in range(100)]}]}

    async def ask(*args, **kwargs):
        return Analysis(summary="tested", findings=[{"title": "fact", "detail": "rows", "evidence_ids": ["q1"]}])

    monkeypatch.setattr("insight.analytics.run_analysis", calculate)
    workflow.ask = ask
    result = await workflow.analysis(state)
    assert all(len(q["rows"]) <= 50 and q.get("result_ref") for q in result["queries"])
    assert len(workflow.repository.get_query_result(state["run_id"], "calc0_diagnostic")["rows"]) == 120
    assert len(workflow.repository.get_query_result(state["run_id"], "calc0_diagnostic_details")["rows"]) == 100
    assert "series" not in result["calculations"][0] and "tables" not in result["calculations"][0]


@pytest.mark.asyncio
async def test_supplement_cannot_erase_previous_truncation_partial_flag():
    workflow, state = fixture()
    state["previous_queries"] = [save(workflow, state, query_id="old", truncated=True)]
    state["queries"] = [save(workflow, state, query_id="new")]
    state["partial"] = True
    workflow.calculate = lambda state: ([], [])

    async def ask(*args, **kwargs):
        return Analysis(summary="tested", findings=[{"title": "fact", "detail": "rows", "evidence_ids": ["new"]}])

    workflow.ask = ask
    result = await workflow.analysis(state)
    assert result["partial"]


def test_chart_validation_sees_invalid_row_beyond_preview():
    workflow, state = fixture()
    raw = {"id": "chart", "columns": ["label", "value"], "rows": [[str(i), 1] for i in range(120)], "truncated": False}
    raw["rows"][110][1] = -5
    state["queries"] = [workflow.repository.save_query_result(state["run_id"], raw)]
    chart = {"type": "pie", "query_id": "chart", "title": "share", "x": "label", "y": ["value"]}
    assert validate_charts([chart], state["queries"])[0]["type"] == "pie"  # why previews are insufficient
    assert validate_charts([chart], workflow.full_queries(state))[0]["type"] == "table"
