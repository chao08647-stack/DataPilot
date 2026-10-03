import asyncio
import json
from contextlib import AsyncExitStack

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.store.postgres.aio import AsyncPostgresStore
from langgraph.types import Command

from insight.dashboards import Dashboards
from insight.enterprise import CATALOG_KEYS, Enterprise
from insight.memory import MemoryService
from insight.providers import BudgetExhausted, Embedder, ModelClient, ModelError
from insight.repository import Repository
from insight.scenarios import generate_all, load_scenarios
from insight.skills import SkillRegistry
from insight.workflow import Workflow, artifacts, initial_state

TERMINAL = {"completed", "failed", "cancelled", "interrupted"}


class Runtime:
    def __init__(self, settings):
        self.settings = settings
        self.scenarios = load_scenarios(settings.scenario_root)
        self.skills = SkillRegistry(settings.skill_root)
        self.paths = {}
        self.repository = self.memory = self.graph = None
        self.model = ModelClient(settings)
        self.tasks = {}
        self.stack = AsyncExitStack()
        self.startup_error = None
        self.seed_report = []
        self.enterprise = self.dashboards = None

    @property
    def ready(self):
        return self.graph is not None

    async def start(self):
        self.paths = await asyncio.to_thread(generate_all, self.settings.scenario_root, self.settings.data_dir)
        if not self.settings.postgres_uri.get_secret_value():
            return  # History/analysis unavailable; never silently substitute replay.
        try:
            dsn = self.settings.postgres_uri.get_secret_value()
            repo = Repository(dsn)
            await asyncio.to_thread(repo.setup)
            saver = await self.stack.enter_async_context(AsyncPostgresSaver.from_conn_string(dsn))
            store = await self.stack.enter_async_context(AsyncPostgresStore.from_conn_string(dsn))
            await saver.setup()
            await store.setup()
            embedder = Embedder(self.settings) if self.settings.embedding_base_url else None
            self.memory = MemoryService(store, repo, embedder)
            repo.recover_interrupted()
            self.repository = repo
            self.bind(saver, store)
            # Seeding may call Embedding; only explicit initialization may do so.
        except Exception as exc:
            self.startup_error = str(exc) if isinstance(exc, ValueError) else f"持久化初始化失败（{type(exc).__name__}）；实时分析已禁用。请检查独立数据库及 vector 扩展。"
            self.graph = self.repository = self.memory = None
            await self.stack.aclose()

    def bind(self, saver, store):
        self.enterprise = Enterprise(self.repository, self.scenarios, self.paths, self.settings)
        self.enterprise.setup()
        self.dashboards = Dashboards(self.enterprise)
        self.workflow = Workflow(self.settings, self.scenarios, self.paths, self.repository, self.memory, self.model, self.skills, self.enterprise)
        self.graph = self.workflow.compile(saver, store)

    async def seed_template(self, template_id):
        scene = self.scenarios[template_id]
        published = self.enterprise.catalog(template_id)
        expected = {k: v for k, v in scene.items() if k in CATALOG_KEYS and k != "schema_version"}
        expected.update(id=template_id, template_id=template_id, synthetic=True, dialect="duckdb")
        # User-edited definitions must not inherit builtin experiences under a new version label.
        if expected != {k: v for k, v in published.items() if k != "schema_version"}:
            return {"skipped": "published_model_differs_from_template"}
        try:
            self.enterprise.require_model_access(template_id)
        except ValueError:
            return {"skipped": "model_access_not_authorized"}
        report = await self.memory.seed({**scene, "schema_version": published["schema_version"]})
        self.seed_report.append(report)
        return report

    def launch(self, run: dict, answer=None):
        if run["run_id"] in self.tasks and not self.tasks[run["run_id"]].done():
            raise ValueError("任务正在执行。")
        task = asyncio.create_task(self.drive(run, answer))
        self.tasks[run["run_id"]] = task
        task.add_done_callback(lambda done: self.tasks.pop(run["run_id"], None) if self.tasks.get(run["run_id"]) is done else None)

    async def drive(self, run, answer=None):
        repo, run_id = self.repository, run["run_id"]
        config = {"configurable": {"thread_id": run["thread_id"]}, "recursion_limit": 90}
        try:
            if repo.get_run(run_id)["status"] == "cancelled":
                return
            repo.update_run(run_id, status="running")
            if self.enterprise:
                domain = self.enterprise.require_model_access(run["scenario_id"])
                if not run.get("model_version"):
                    run = {**run, "model_version": run.get("artifacts", {}).get("model_version", domain["model_version"])}
            repo.event(run_id, "run.started", {"resumed": answer is not None, "scenario_id": run["scenario_id"]}, key=f"start:{'initial' if answer is None else len(repo.messages(run['thread_id']))}")
            async with asyncio.timeout(self.settings.max_run_seconds):
                value = await self.graph.ainvoke(initial_state(run) if answer is None else Command(resume=answer), config=config)
            if value.get("__interrupt__"):
                snapshot = await self.graph.aget_state(config)
                pending = snapshot.values.get("clarification")
                repo.update_run(run_id, status="waiting_for_input")
                if pending:
                    repo.add_message(run["thread_id"], "assistant", pending, run_id, key=f"clarify:{snapshot.values.get('clarification_rounds', 0)}")
                return
            output = artifacts(value)
            repo.add_message(run["thread_id"], "assistant", value["analysis"]["summary"], run_id, key=f"answer:{run_id}")
            # Mark successful only after graph termination and final evidence approval.
            repo.update_run(run_id, status="completed", artifacts=output, error=None)
            if value["repair_history"] and not value.get("partial"):
                episode = {"problem": value["question"], "failures_and_strategies": value["repair_history"],
                           "final_strategy": [q["rationale"] for q in value["plan"]["queries"]],
                           "applicable_tables": value["discovery"]["tables"], "verification": "execution_and_supervisor_approved"}
                try:
                    self.enterprise.require_model_access(run["scenario_id"])
                    catalog = self.enterprise.catalog(run["scenario_id"], run["model_version"])
                    await self.memory.remember_repair(run["scenario_id"], catalog["schema_version"], run_id,
                                                     json.dumps(episode, ensure_ascii=False), value["repair_history"][-1]["category"], value["discovery"]["tables"], dialect=catalog.get("dialect", "duckdb"))
                except Exception:
                    repo.event(run_id, "memory.warning", {"message": "分析已完成，但本轮经验持久化失败；未宣称经验可复用。"})
            repo.event(run_id, "run.completed", {"scenario_id": run["scenario_id"], "usage": value["budget"]}, key="completed")
        except asyncio.CancelledError:
            if repo.get_run(run_id)["status"] not in TERMINAL:
                repo.update_run(run_id, status="interrupted", error="服务停止；执行中任务不自动恢复，请重新提交。")
                repo.event(run_id, "run.failed", {"message": "服务停止，任务已中断。"}, key="interrupted")
        except Exception as exc:
            if repo.get_run(run_id)["status"] in TERMINAL:
                return
            if isinstance(exc, BudgetExhausted):
                existing = repo.get_run(run_id)["artifacts"]
                if existing.get("queries"):
                    summary = "本轮分析预算已耗尽，已保留查询证据；尚未完成最终分析复核，不能视为完整诊断。"
                    existing.update(partial=True, analysis={"summary": summary, "findings": [], "recommendations": [], "limitations": [str(exc)], "evidence_gaps": ["需要新任务继续核验"]})
                    repo.update_run(run_id, status="completed", artifacts=existing, error=None)
                    repo.add_message(run["thread_id"], "assistant", summary, run_id, key=f"partial:{run_id}")
                    repo.event(run_id, "run.completed", {"partial": True, "message": summary}, key="completed")
                    return
            message = str(exc) if isinstance(exc, ModelError) else "任务超时。" if isinstance(exc, TimeoutError) else f"任务失败（{type(exc).__name__}）。未使用示例答案替代。"
            repo.update_run(run_id, status="failed", error=message)
            repo.event(run_id, "run.failed", {"message": message}, key="failed")

    async def stop(self):
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self.stack.aclose()
