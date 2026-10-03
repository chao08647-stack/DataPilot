import asyncio
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore

from insight.config import PROJECT_ROOT, Settings
from insight.main import create_app
from insight.memory import MemoryService
from insight.models import Analysis, Charts, Discovery, Intent, Investigation, Review, SQLPlan, Summary
from insight.repository import InMemoryRepository
from insight.runtime import Runtime
from insight.scenarios import generate_all, load_scenarios
from insight.workflow import initial_state


class FakeModel:
    """No network. Responses are deliberate test inputs, never a production fallback."""
    def __init__(self, *, clarify=False, execution_error=False, semantic_error=False, delay=0):
        self.clarify, self.execution_error, self.semantic_error, self.delay = clarify, execution_error, semantic_error, delay
        self.calls = []
        self.sql_count = 0
        self.review_count = 0

    async def complete(self, schema, system, payload, budget):
        await asyncio.sleep(self.delay)
        self.calls.append((schema.__name__, payload))
        budget["calls"] = budget.get("calls", 0) + 1
        if schema is Summary:
            return Summary(summary="历史用户要求：使用年度销售额。")
        if schema is Intent:
            return Intent(normalized_question=payload["question"], metric_ids=["sales"], task_tags=["trend", "comparison"],
                          clarification="请确认统计年份。" if self.clarify and "用户补充" not in payload["question"] else None)
        if schema is Discovery:
            return Discovery(metric_ids=["sales"], tables=["orders"], rationale="订单销售额")
        if schema is Investigation:
            return Investigation(steps=[{"objective": "核对正式指标", "metric_ids": payload["discovery"]["metric_ids"], "grain": "正式事实粒度"}])
        if schema is SQLPlan:
            self.sql_count += 1
            sql = "SELECT SUM(order_total) AS sales FROM orders WHERE status='completed'"
            if self.sql_count == 1 and self.execution_error:
                sql = "SELECT missing_column AS sales FROM orders"
            if self.sql_count == 1 and self.semantic_error:
                sql = "SELECT SUM(order_total) AS sales FROM orders"
            query_id = payload.get("plan", {}).get("queries", [{"id": "q1"}])[0]["id"]
            return SQLPlan(queries=[{"id": query_id, "sql": sql, "rationale": "按完成订单统计，修复字段或遗漏状态过滤"}])
        if schema is Review:
            self.review_count += 1
            if self.semantic_error and self.review_count == 1:
                return Review(action="repair", feedback="漏掉完成状态过滤，包含取消订单。")
            return Review(action="approve")
        if schema is Analysis:
            return Analysis(summary="销售额来自已完成订单。", findings=[{"title": "销售汇总", "detail": "按正式指标统计。", "evidence_ids": [payload["queries"][0]["id"]]}])
        if schema is Charts:
            return Charts(charts=[{"type": "kpi", "title": "销售额", "query_id": payload["queries"][0]["id"], "y": ["sales"]}])
        raise AssertionError(schema)


class TestRuntime(Runtime):
    __test__ = False

    def __init__(self, settings, paths, model=None, repo=None, saver=None, store=None, *, profile="enterprise"):
        super().__init__(settings)
        # Legacy flow assertions intentionally use their original metric/example contracts.
        # New boundary tests opt into the default light-BI profile explicitly.
        self.scenarios = load_scenarios(settings.scenario_root, profile=profile)
        self.paths = paths
        self.repository = repo or InMemoryRepository()
        self.model = model or FakeModel()
        self.test_saver = saver or InMemorySaver()
        self.test_store = store or InMemoryStore()
        self.memory = MemoryService(self.test_store, self.repository)

    async def start(self):
        for scenario in self.scenarios.values():
            await self.memory.seed(scenario)
        self.repository.recover_interrupted()
        self.bind(self.test_saver, self.test_store)


@pytest.fixture(scope="module")
def paths(tmp_path_factory):
    directory = Path(os.environ["INSIGHT_TEST_DATA_DIR"]) if os.environ.get("INSIGHT_TEST_DATA_DIR") else tmp_path_factory.mktemp("workflow-data")
    # Versioned enterprise-2 filenames stay separate from light-bi-3 databases.
    return generate_all(PROJECT_ROOT / "scenarios", directory, profile="enterprise")


@pytest.fixture(scope="module")
def bi_paths(tmp_path_factory):
    directory = Path(os.environ["INSIGHT_TEST_DATA_DIR"]) if os.environ.get("INSIGHT_TEST_DATA_DIR") else tmp_path_factory.mktemp("workflow-light-bi")
    return generate_all(PROJECT_ROOT / "scenarios", directory)


def settings():
    return Settings(_env_file=None, llm_base_url="https://fake.invalid", llm_model="offline-test", postgres_uri="")


async def run_to_pause(rt, run, answer=None):
    await rt.drive(run, answer)
    return rt.repository.get_run(run["run_id"])


@pytest.mark.parametrize("repair_mode", ["none", "execution", "semantic"])
async def test_end_to_end_and_verified_runtime_memory(paths, repair_mode):
    model = FakeModel(execution_error=repair_mode == "execution", semantic_error=repair_mode == "semantic")
    rt = TestRuntime(settings(), paths, model)
    await rt.start()
    run = rt.repository.create_run("ecommerce", "统计2025年的销售额")
    result = await run_to_pause(rt, run)
    assert result["status"] == "completed", result["error"]
    assert result["artifacts"]["queries"][0]["rows"]
    runtime = [m for m in await rt.memory.list("ecommerce") if m["origin"] == "runtime"]
    assert len(runtime) == (0 if repair_mode == "none" else 1)
    events = rt.repository.events(run["run_id"])
    assert any(e["type"] == "skill.loaded" and e["payload"]["content"] for e in events)
    if repair_mode != "none":
        assert any(e["type"] == "memory.recalled" and e["payload"]["kind"] == "sql_experience" for e in events)
    assert all("evaluation" not in str(payload) and "fixture_sql" not in str(payload) for _, payload in model.calls)
    await rt.stop()


async def test_checkpoint_resume_survives_new_runtime(paths):
    rt = TestRuntime(settings(), paths, FakeModel(clarify=True))
    await rt.start()
    run = rt.repository.create_run("ecommerce", "销售额怎么样")
    assert (await run_to_pause(rt, run))["status"] == "waiting_for_input"
    fresh = TestRuntime(settings(), paths, FakeModel(clarify=True), rt.repository, rt.test_saver, rt.test_store)
    await fresh.start()
    restored = fresh.repository.resume_run(run["run_id"])
    result = await run_to_pause(fresh, restored, "2025年")
    assert result["status"] == "completed", result["error"]
    assert result["thread_id"] == run["thread_id"] and "2025年" in result["question"]
    assert len([e for e in rt.repository.events(run["run_id"]) if e["type"] == "run.input_required"]) == 1
    await fresh.stop()


async def test_enabled_preferences_and_full_messages_summary(paths):
    rt = TestRuntime(settings(), paths)
    await rt.start()
    pref = next(m for m in await rt.memory.list("ecommerce") if m["kind"] == "preference")
    await rt.memory.update("ecommerce", pref["id"], active=True)
    prior = rt.repository.create_run("ecommerce", "历史请求")
    for i in range(9):
        rt.repository.add_message(prior["thread_id"], "user", f"第{i}轮问题", prior["run_id"])
        rt.repository.add_message(prior["thread_id"], "assistant", f"第{i}轮回答", prior["run_id"])
    rt.repository.update_run(prior["run_id"], status="completed")
    run = rt.repository.create_run("ecommerce", "本次只要KPI，不分渠道", prior["thread_id"])
    before = len(rt.repository.messages(run["thread_id"]))
    result = await run_to_pause(rt, run)
    assert result["status"] == "completed", result["error"]
    assert rt.repository.get_summary(run["thread_id"])["text"]
    assert len(rt.repository.messages(run["thread_id"])) == before+1
    payload = next(payload for name, payload in rt.model.calls if name == "Intent")
    assert payload["preferences"][pref["metadata"]["key"]] == pref["metadata"]["value"]
    assert "本次只要KPI" in payload["question"]
    assert await rt.memory.preferences("saas") == {}
    await rt.stop()


def test_http_replay_sse_and_resume(paths):
    config = settings()
    rt = TestRuntime(config, paths, FakeModel(clarify=True))
    with TestClient(create_app(config, rt)) as client:
        assert len(client.get("/api/v1/scenarios").json()) == 3
        before = len(rt.repository.list_threads())
        replay = client.get("/api/v1/scenarios/ecommerce/examples/profit-trend")
        assert replay.status_code == 200 and replay.json()["mode"] == "replay"
        assert len(rt.repository.list_threads()) == before and rt.model.calls == []
        create = client.post("/api/v1/runs", json={"scenario_id": "ecommerce", "question": "销售额如何"})
        assert create.status_code == 202
        run_id = create.json()["run_id"]
        with client.stream("GET", f"/api/v1/runs/{run_id}/events") as response:
            text = "".join(response.iter_text())
        assert "run.input_required" in text and "stream.closed" in text
        waiting = client.get(f"/api/v1/runs/{run_id}").json()
        cursor = waiting["events"][-1]["event_id"]
        assert client.post(f"/api/v1/runs/{run_id}/resume", json={"answer": "2025年"}).status_code == 202
        with client.stream("GET", f"/api/v1/runs/{run_id}/events?after={cursor}") as response:
            resumed = "".join(response.iter_text())
        assert "run.completed" in resumed
        assert client.get(f"/api/v1/runs/{run_id}").json()["status"] == "completed"
        assert client.post(f"/api/v1/runs/{run_id}/resume", json={"answer": "重复"}).status_code == 409
        assert len(rt.repository.list_threads()) == before+1
        assert client.post("/api/v1/runs", headers={"Origin": "https://untrusted.invalid"}, json={"scenario_id": "ecommerce", "question": "恶意请求"}).status_code == 403


async def test_default_dimension_is_not_lost_to_model_override_flag(paths):
    class MisclassifiedOverride(FakeModel):
        async def complete(self, schema, system, payload, budget):
            value = await super().complete(schema, system, payload, budget)
            if isinstance(value, Intent):
                value.dimension_override = True  # A model may mistake "no extra queries" for "no grouping".
            return value
    rt = TestRuntime(settings(), paths, MisclassifiedOverride())
    await rt.start()
    run = rt.repository.create_run("ecommerce", "分析2025年已完成订单销售额，不需要额外补数。")
    state = {**initial_state(run), "preferences": {"default_dimension": "channel"}}
    inferred = await rt.workflow.intent(state)
    assert inferred["intent"]["dimensions"] == ["channel"]
    state["question"] = "只看总额，不按渠道分组"
    assert (await rt.workflow.intent(state))["intent"]["dimensions"] == []
    await rt.stop()


async def test_multi_fact_plan_is_not_split_by_table_set(paths):
    rt = TestRuntime(settings(), paths)
    await rt.start()
    run = rt.repository.create_run("ecommerce", "每月销售额和毛利")
    state = {**initial_state(run), "intent": {"metric_ids": ["sales", "gross_profit"], "task_tags": []},
             "discovery": {"metric_ids": ["sales", "gross_profit"], "tables": ["orders", "order_items"], "dimensions": ["month"]}}
    output = await rt.workflow.sql(state)
    # Grain-aware planning sees both metrics; table-set equality no longer schedules queries.
    assert len(output["plan"]["queries"]) == 1
    calls = [payload for name, payload in rt.model.calls if name == "SQLPlan"]
    assert len(calls) == 1
    assert calls[0]["discovery"]["metric_ids"] == ["sales", "gross_profit"]
    assert {"sales", "gross_profit"}.issubset({m["id"] for m in calls[0]["catalog"]["metrics"]})
    await rt.stop()


async def test_unchanged_successful_query_does_not_spend_another_attempt(paths):
    rt = TestRuntime(settings(), paths)
    await rt.start()
    run = rt.repository.create_run("ecommerce", "两项查询")
    state = {**initial_state(run), "discovery": {"tables": ["orders"]}, "plan": {"queries": [
        {"id": "q1", "sql": "SELECT COUNT(*) AS n FROM orders", "rationale": "count"},
        {"id": "q2", "sql": "SELECT missing_column FROM orders", "rationale": "invalid"}]}}
    state.update(await rt.workflow.execute(state))
    assert state["attempts"] == {"q1": 1, "q2": 1}
    state["plan"]["queries"][1]["sql"] = "SELECT SUM(order_total) AS total FROM orders"
    state.update(await rt.workflow.execute(state))
    assert state["attempts"] == {"q1": 1, "q2": 2}
    assert not state["error"]
    await rt.stop()


@pytest.mark.parametrize("scenario_id,case_index", [(s, i) for s in ("ecommerce", "saas", "retail") for i in range(2)])
async def test_each_scenario_clarification_routes_and_resumes(paths, scenario_id, case_index):
    rt = TestRuntime(settings(), paths)
    scene = rt.scenarios[scenario_id]

    class FlowFixtureModel(FakeModel):
        """Gold SQL is only an offline workflow test fixture, never given to a live model."""
        async def complete(self, schema, system, payload, budget):
            if schema is Intent:
                budget["calls"] = budget.get("calls", 0)+1
                return Intent(normalized_question=payload["question"], metric_ids=[scene["metrics"][0]["id"]],
                              clarification="请确认指标口径与时间。" if "用户补充" not in payload["question"] else None)
            if schema is Discovery:
                return Discovery(metric_ids=[scene["metrics"][0]["id"]], tables=[t["name"] for t in scene["tables"]], rationale="离线流程用例")
            if schema is SQLPlan:
                return SQLPlan(queries=[{"id": "fixture", "sql": scene["evaluation"][0]["sql"], "rationale": "离线测试固定SQL"}])
            return await super().complete(schema, system, payload, budget)
    rt.model = FlowFixtureModel()
    await rt.start()
    case = scene["clarifications"][case_index]
    run = rt.repository.create_run(scenario_id, case["question"])
    assert (await run_to_pause(rt, run))["status"] == "waiting_for_input"
    resumed = rt.repository.resume_run(run["run_id"])
    completed = await run_to_pause(rt, resumed, case["answer"])
    assert completed["status"] == "completed", completed["error"]
    assert completed["scenario_id"] == scenario_id
    assert completed["artifacts"]["queries"]
    await rt.stop()


def test_http_cancel_is_terminal_and_does_not_create_runtime_memory(paths):
    config = settings()
    rt = TestRuntime(config, paths, FakeModel(delay=0.2))
    with TestClient(create_app(config, rt)) as client:
        run = client.post("/api/v1/runs", json={"scenario_id": "ecommerce", "question": "统计2025销售额"}).json()
        cancelled = client.delete(f"/api/v1/runs/{run['run_id']}")
        assert cancelled.json()["status"] == "cancelled"
        assert client.delete(f"/api/v1/runs/{run['run_id']}").json()["status"] == "cancelled"
        assert not [m for m in client.get("/api/v1/memories?scenario_id=ecommerce").json() if m["origin"] == "runtime"]


def test_unavailable_persistence_refuses_work_without_fake_history(bi_paths):
    config = Settings(_env_file=None, postgres_uri="", postgres_host="", llm_base_url="", data_dir=bi_paths["ecommerce"].parent)
    with TestClient(create_app(config)) as client:
        assert client.get("/health").json()["mode"] == "unavailable"
        assert client.post("/api/v1/runs", json={"scenario_id": "ecommerce", "question": "查询销售额"}).status_code == 503
        # The current template has no replay answer; an unavailable backend must not invent one.
        assert client.get("/api/v1/scenarios/ecommerce/examples/profit-trend").status_code == 404


async def test_diagnostic_query_ids_and_input_contract_are_server_owned(paths):
    class RenamingModel(FakeModel):
        async def complete(self, schema, system, payload, budget):
            if issubclass(schema, SQLPlan):
                self.calls.append((schema.__name__, payload))
                return SQLPlan(queries=[{"id": "model_chose_a_new_name", "sql": payload["diagnostic_blueprint"]["sql"], "rationale": "离线蓝图契约测试"}])
            return await super().complete(schema, system, payload, budget)

    rt = TestRuntime(settings(), paths, RenamingModel())
    await rt.start()
    run = rt.repository.create_run("ecommerce", "比较9月和11月贡献利润")
    state = {**initial_state(run), "intent": {"metric_ids": ["contribution_profit"], "task_tags": ["profit"]},
             "discovery": {"metric_ids": ["contribution_profit"], "tables": [], "dimensions": []},
             "investigation": {"diagnostic_ids": ["profit-bridge"], "period_selections": {"profit-bridge": {"baseline_period": "2025-09", "current_period": "2025-11"}}}}
    state.update(await rt.workflow.sql(state))
    assert state["plan"]["queries"][0]["id"] == "q1_1"
    assert state["diagnostic_bindings"][0]["query_map"] == {"profit_input": "q1_1"}
    contracts = rt.workflow.diagnostic_contracts(state)
    assert "gross_sales" in contracts[0]["output_columns"] and "period" in contracts[0]["output_columns"]
    state.update(await rt.workflow.execute(state))
    assert not state["error"], state["error"]
    query = state["queries"][0]
    assert {r[query["columns"].index("period")] for r in query["rows"]} == {"2025-09", "2025-11"}
    await rt.workflow.review(state)
    review = next(payload for name, payload in rt.model.calls if name == "Review")
    assert review["diagnostic_contracts"] == contracts
    calculations, derived = rt.workflow.calculate(state)
    assert calculations[0]["status"] == "ok", calculations
    assert calculations[0]["evidence_ids"] == ["q1_1"] and derived
    await rt.stop()


@pytest.mark.parametrize("always_reject", [False, True])
async def test_delivery_revision_is_bounded_and_never_changes_sql(bi_paths, always_reject):
    class ReviewModel(FakeModel):
        async def complete(self, schema, system, payload, budget):
            result = await super().complete(schema, system, payload, budget)
            if schema is Review and "analysis" in payload:
                if always_reject or self.review_count == 2:
                    return Review(action="repair", feedback="不要把销售额标作利润；请修订结论与图表。")
            return result

    model = ReviewModel()
    rt = TestRuntime(settings(), bi_paths, model, profile=None)
    await rt.start()
    run = rt.repository.create_run("ecommerce", "统计销售额")
    result = await run_to_pause(rt, run)
    assert result["status"] == ("failed" if always_reject else "completed"), result["error"]
    assert result["artifacts"]["delivery_revisions"] == 1
    assert model.sql_count == 1
    assert model.review_count == 3
    assert result["artifacts"]["usage"]["calls"] <= 24
    await rt.stop()
