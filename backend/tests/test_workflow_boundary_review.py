"""Offline boundary regressions; no model, database or service calls."""
from types import SimpleNamespace

import pytest

from insight.models import Analysis, Review, Summary
from insight.providers import ModelError
from insight.workflow import Workflow


def workflow_fixture(*, revoked=False, revoke_during=None):
    consent = {"enabled": not revoked}
    searches = []
    scene = {"id": "private-domain", "schema_version": "version-1", "dialect": "duckdb"}
    summary = {"text": "", "upto_id": 0}

    def require_model_access(domain_id):
        assert domain_id == "private-domain"
        if not consent["enabled"]:
            raise ValueError("model access revoked")

    async def preferences(*args):
        if revoke_during == "preferences":
            consent["enabled"] = False
        return {}

    async def search(*args, **kwargs):
        searches.append(args)
        return {"items": [], "mode": "hybrid"}

    async def complete(schema, *args):
        assert schema is Summary
        if revoke_during == "summary":
            consent["enabled"] = False
        return Summary(summary="retained history")

    def save_summary(thread, text, upto):
        summary.update(text=text, upto_id=upto)

    repo = SimpleNamespace(
        messages=lambda thread: [
            {"message_id": i, "role": "user", "content": f"private message {i}"}
            for i in range(1, 4)
        ],
        get_summary=lambda thread: dict(summary),
        save_summary=save_summary,
        event=lambda *args, **kwargs: None,
    )
    flow = Workflow(
        SimpleNamespace(summary_turns=2, summary_tokens=8000, recent_turns=1,
                        summary_max_tokens=1000, sql_attempts=3),
        {}, {}, repo, SimpleNamespace(preferences=preferences, search=search),
        SimpleNamespace(complete=complete), None,
        enterprise=SimpleNamespace(require_model_access=require_model_access, catalog=lambda *args: scene),
    )
    state = {"run_id": "run", "thread_id": "thread", "scenario_id": scene["id"],
             "model_version": "version-1", "question": "private user question", "budget": {"calls": 0},
             "error": {"query_id": "semantic", "category": "semantic", "message": "wrong denominator"},
             "attempts": {}, "discovery": {"tables": ["orders"]}}
    return flow, state, searches


@pytest.mark.asyncio
async def test_repair_checks_consent_before_retrieval_embedding():
    flow, state, searches = workflow_fixture(revoked=True)
    with pytest.raises(ValueError, match="revoked"):
        await flow.repair(state)
    assert searches == []


@pytest.mark.asyncio
@pytest.mark.parametrize("revoke_during", ["summary", "preferences"])
async def test_prepare_rechecks_consent_after_await_before_embedding(revoke_during):
    flow, state, searches = workflow_fixture(revoke_during=revoke_during)
    with pytest.raises(ValueError, match="revoked"):
        await flow.prepare(state)
    assert searches == []


@pytest.mark.asyncio
async def test_delivery_revision_cannot_start_followup_or_reference_new_evidence():
    flow, state, _ = workflow_fixture()

    async def ask(*args, **kwargs):
        return Analysis(summary="revised", follow_up_question="another query",
                        findings=[{"title": "fact", "detail": "verified", "evidence_ids": ["q1"]}])

    flow.ask = ask
    state.update(analysis={}, review={}, queries=[{
        "id": "q1", "columns": ["amount"], "rows": [[10]], "truncated": False,
    }])
    result = await flow.delivery_revision(state)
    assert result["delivery_revisions"] == 1
    assert result["analysis"]["follow_up_question"] is None
    state["queries"] = []
    with pytest.raises(ModelError, match="不存在"):
        await flow.delivery_revision(state)


@pytest.mark.asyncio
async def test_final_review_cannot_request_second_delivery_revision():
    flow, state, _ = workflow_fixture()

    async def ask(*args, **kwargs):
        return Review(action="repair", feedback="still wrong")

    flow.ask = ask
    state.update(analysis={}, charts=[], queries=[], delivery_revisions=1)
    with pytest.raises(ModelError, match="最终证据复核未通过"):
        await flow.final_review(state)


@pytest.mark.asyncio
async def test_delivery_revision_preserves_system_truncation_warning():
    flow, state, _ = workflow_fixture()
    warning = "部分查询达到 20,000 行或 10 MiB 保存上限；截断结果不能代表完整总体，请缩小范围或先聚合。"

    async def ask(*args, **kwargs):
        # Pydantic permits omitted limitations, so this is a valid model reply.
        return Analysis(summary="revised", findings=[{
            "title": "fact", "detail": "observed rows only", "evidence_ids": ["q1"],
        }])

    flow.ask = ask
    state.update(analysis={"summary": "before", "limitations": [warning]}, review={}, queries=[{
        "id": "q1", "columns": ["amount"], "rows": [[10]], "truncated": True,
    }])
    result = await flow.delivery_revision(state)
    assert warning in result["analysis"]["limitations"]
