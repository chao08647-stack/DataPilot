import pytest
from pydantic import ValidationError

from insight.investigation import InvestigationNodes
from insight.models import Investigation, PeriodSelection, QueryDraft, SingleQueryPlan


def test_pair_periods_have_a_strict_schema_not_untyped_keys():
    with pytest.raises(ValidationError):
        PeriodSelection.model_validate({"baseline": "2025-Q3", "current": "2025-Q4"})
    assert set(PeriodSelection.model_json_schema()["required"]) == {"baseline_period", "current_period"}


def test_single_diagnostic_input_rejects_multiple_queries():
    query = {"id": "q", "sql": "SELECT 1", "rationale": "test"}
    with pytest.raises(ValidationError):
        SingleQueryPlan(queries=[query, query])
    assert SingleQueryPlan.model_json_schema()["properties"]["queries"]["maxItems"] == 1


@pytest.mark.asyncio
async def test_trend_scope_excludes_pair_tool_and_preserves_all_months():
    class Nodes(InvestigationNodes):
        def scene(self, state):
            return {"metrics": [{"id": "sales"}], "diagnostics": [
                {"id": "monthly", "supported_query_types": ["trend"], "period_policy": "all"},
                {"id": "bridge", "supported_query_types": ["comparison"], "period_policy": "pair"},
            ]}

        async def ask(self, state, schema, node, instructions, extra):
            assert [d["id"] for d in extra["available_diagnostics"]] == ["monthly"]
            assert extra["existing_evidence"][0]["id"] == "existing"
            return Investigation(steps=[{"objective": "月度趋势", "metric_ids": ["sales"], "grain": "月"}],
                                 diagnostic_ids=["monthly"], period_selections={"monthly": {"baseline_period": "2025-01", "current_period": "2025-12"}})

        def emit(self, *args):
            pass

    result = await Nodes().investigate({"intent": {"query_type": "trend"}, "discovery": {}, "previous_queries": [{"id": "existing"}]})
    assert result["investigation"]["period_selections"] == {}


def test_continuous_contract_is_not_cropped_even_with_stale_pair_config():
    class Nodes(InvestigationNodes):
        def scene(self, state):
            return {"dialect": "duckdb"}

        def diagnostic_contracts(self, state):
            return [{"query_id": "q", "period_policy": "all", "output_columns": ["period"],
                     "config": {"baseline_period": "2025-01", "current_period": "2025-12"}}]

    draft = QueryDraft(id="q", sql="SELECT period FROM monthly", rationale="test")
    Nodes().bind_diagnostic_periods({}, [draft])
    assert draft.sql == "SELECT period FROM monthly"
