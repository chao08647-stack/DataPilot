import asyncio
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import ValidationError

from insight.config import Settings
from insight.enterprise_api import router as enterprise_router
from insight.history_api import router as history_router
from insight.models import MemoryUpdate, ResumeRequest, RunCreate
from insight.question_catalog import get_question
from insight.runtime import TERMINAL, Runtime
from insight.sql import execute_query, validate_charts


def create_app(settings=None, runtime=None):
    settings = settings or Settings()
    rt = runtime or Runtime(settings)

    @asynccontextmanager
    async def lifespan(app):
        await rt.start()
        app.state.runtime = rt
        yield
        await rt.stop()

    app = FastAPI(title="Insight Agents", version="0.3.0", lifespan=lifespan)
    app.include_router(enterprise_router(rt))
    app.include_router(history_router(rt))
    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_methods=["GET", "POST", "PATCH", "DELETE"], allow_headers=["Content-Type", "Last-Event-ID"])

    @app.middleware("http")
    async def local_write_guard(request: Request, call_next):
        # Loopback deployment is not authentication; reject cross-origin browser writes.
        origin = request.headers.get("origin")
        if request.method in {"POST", "PATCH", "DELETE"} and origin and origin not in settings.cors_origins:
            return JSONResponse(status_code=403, content={"detail": "未批准的浏览器来源。"})
        return await call_next(request)

    @app.exception_handler(KeyError)
    async def not_found(request, exc):
        return JSONResponse(status_code=404, content={"detail": "资源不存在。"})

    @app.exception_handler(ValueError)
    async def conflict(request, exc):
        return JSONResponse(status_code=409, content={"detail": str(exc)[:500]})

    @app.exception_handler(ValidationError)
    async def invalid_resource(request, exc):
        return JSONResponse(status_code=422, content={"detail": "资源字段校验失败；请检查字段类型、授权范围与环境变量引用，不要填写密码原文。"})

    def need_runtime():
        if not rt.ready:
            raise HTTPException(503, rt.startup_error or "持久化服务未就绪；请先启动本项目独立 PostgreSQL。")
        return rt.repository

    def scene(scenario_id):
        return rt.scenarios[scenario_id]

    @app.get("/health")
    @app.get("/api/v1/health")
    async def health():
        return {"status": "ok" if rt.ready else "unavailable", "mode": "live" if rt.ready else "unavailable",
                "postgres_ready": rt.ready, "llm_configured": settings.llm_configured,
                "embedding_configured": bool(settings.embedding_base_url), "model_connectivity": "not_checked",
                "startup_error": rt.startup_error, "seed_embedding_failures": sum(x.get("embedding_failures", 0) for x in rt.seed_report)}

    @app.get("/api/v1/scenarios")
    async def scenarios():
        return [{"id": s["id"], "title": s["title"], "description": s["description"], "date_range": s["date_range"],
                 "examples": [{k: v for k, v in e.items() if k != "sql"} for e in s["examples"]],
                 "metrics": s["metrics"], "table_count": len(s["tables"])} for s in rt.scenarios.values()]

    @app.get("/api/v1/scenarios/{scenario_id}")
    async def scenario_detail(scenario_id: str):
        return {k: v for k, v in scene(scenario_id).items() if k not in {"repair_experiences", "evaluation", "clarifications", "business_knowledge", "preferences"}}

    @app.get("/api/v1/scenarios/{scenario_id}/examples/{example_id}")
    async def replay(scenario_id: str, example_id: str):
        s = scene(scenario_id)
        example = next((e for e in s["examples"] if e["id"] == example_id), None)
        if not example:
            raise HTTPException(404, "示例不存在。")
        query = await asyncio.to_thread(execute_query, rt.paths[scenario_id], example["sql"], s, [t["name"] for t in s["tables"]], "replay-q1")
        charts = validate_charts([{"type": example["chart_type"], "title": example["title"], "query_id": query["id"], "x": example.get("x"), "y": example.get("y", [])}], [query])
        return {"run_id": f"replay:{scenario_id}:{example_id}", "thread_id": None, "scenario_id": scenario_id,
                "question": example["question"], "status": "completed", "mode": "replay", "created_at": datetime.now(UTC).isoformat(), "error": None,
                "artifacts": {"scenario_id": scenario_id, "discovery": {"metric_ids": [], "tables": [t["name"] for t in s["tables"]], "rationale": "示例预设查询，不代表模型数据发现结果。"},
                    "queries": [query], "analysis": {"summary": f"示例回放：{example['title']}。固定 SQL 在合成数据上返回 {len(query['rows'])} 行；未调用模型。",
                    "findings": [], "recommendations": [], "limitations": ["此页为示例回放，不是实时模型分析；不写入会话、经验或偏好。", "数据为合成数据，非任何真实企业经营数据。"]}, "charts": charts}, "events": []}

    @app.get("/api/v1/skills")
    async def skills(scenario_id: str | None = None, domain_id: str | None = None):
        scenario_id = domain_id or scenario_id
        if scenario_id:
            if rt.enterprise:
                catalog = rt.enterprise.catalog(scenario_id)
            else:
                catalog = scene(scenario_id)
            return rt.skills.list(scenario_id, capabilities=catalog.get("capabilities", []))
        return rt.skills.list(scenario_id)

    @app.post("/api/v1/runs", status_code=202)
    async def create_run(body: RunCreate):
        need_runtime()
        if not settings.llm_configured:
            raise HTTPException(503, "未配置 LLM，实时分析不可用。")
        domain = rt.enterprise.require_model_access(body.domain_id)
        catalog = rt.enterprise.catalog(body.domain_id)
        if body.question_id:
            question = get_question(body.question_id)
            if question["domain_id"] != body.domain_id or question["question"] != body.question or body.thread_id or body.context:
                raise HTTPException(422, "清单任务必须使用原问题、对应业务域和独立线程。")
        context = {}
        if body.context:
            if not body.context.get("dashboard_id") or not body.context.get("card_id"):
                raise HTTPException(422, "上下文需要看板与卡片引用。")
            board = rt.dashboards.get(body.context["dashboard_id"])
            if board["domain_id"] != body.domain_id or board["model_version"] != domain["model_version"]:
                raise HTTPException(409, "来源看板与当前业务域或模型版本不一致。")
            card = next((c for c in board["cards"] if c["id"] == body.context["card_id"]), None)
            if not card:
                raise HTTPException(404, "来源卡片不存在。")
            rt.dashboards.validate_filters(body.context.get("filters", {}), catalog)
            context = {"dashboard_id": board["id"], "card_id": card["id"], "title": card["title"], "filters": body.context.get("filters", {}), "metric_ids": card["query"].get("metric_ids", [])}
        run = rt.enterprise.create_run(body.domain_id, body.question, body.thread_id, domain["model_version"], context,
                                      question_id=body.question_id, retry_failed=body.retry_failed)
        run = {**run, "domain_id": body.domain_id, "model_version": domain["model_version"]}
        if not run.get("reused"):
            rt.launch(run)
        return run

    @app.get("/api/v1/runs/{run_id}")
    async def get_run(run_id: str):
        repo = need_runtime()
        run = repo.get_run(run_id)
        return {**run, "domain_id": run["scenario_id"], "model_version": run.get("model_version") or run["artifacts"].get("model_version"), "mode": "live", "events": repo.events(run_id)}

    @app.delete("/api/v1/runs/{run_id}")
    async def cancel_run(run_id: str):
        repo = need_runtime()
        run = repo.get_run(run_id)
        if run["status"] in TERMINAL:
            return run
        run = repo.update_run(run_id, status="cancelled")
        repo.event(run_id, "run.cancelled", {}, key="cancelled")
        task = rt.tasks.get(run_id)
        if task:
            task.cancel()
        return run

    @app.post("/api/v1/runs/{run_id}/resume", status_code=202)
    async def resume_run(run_id: str, body: ResumeRequest):
        repo = need_runtime()
        existing = repo.get_run(run_id)
        rt.enterprise.require_model_access(existing["scenario_id"])
        snapshot = await rt.graph.aget_state({"configurable": {"thread_id": existing["thread_id"]}})
        if not snapshot.next or snapshot.values.get("run_id") != run_id:
            raise HTTPException(409, "未找到该任务的可恢复检查点。")
        run = repo.resume_run(run_id)
        repo.add_message(run["thread_id"], "user", body.answer, run_id, key=f"resume:{run_id}:{snapshot.values.get('clarification_rounds', 0)}")
        rt.launch(run, body.answer)
        return run

    @app.get("/api/v1/runs/{run_id}/events")
    async def stream(run_id: str, request: Request, after: int = Query(0, ge=0)):
        repo = need_runtime()
        repo.get_run(run_id)
        try:
            cursor = max(after, int(request.headers.get("last-event-id", "0")))
        except ValueError:
            raise HTTPException(400, "事件游标必须是整数。") from None

        async def events():
            nonlocal cursor
            ticks = 0
            while not await request.is_disconnected():
                for event in repo.events(run_id, cursor):
                    cursor = event["event_id"]
                    yield f"id: {cursor}\nevent: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                status = repo.get_run(run_id)["status"]
                # Drain after status read, since terminal writes and events are separate transactions.
                if status in TERMINAL | {"waiting_for_input"} and run_id not in rt.tasks:
                    for event in repo.events(run_id, cursor):
                        cursor = event["event_id"]
                        yield f"id: {cursor}\nevent: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                    yield f"event: stream.closed\ndata: {json.dumps({'status': status})}\n\n"
                    return
                ticks += 1
                if ticks % 60 == 0:
                    yield ": heartbeat\n\n"
                await asyncio.sleep(0.25)
        return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/api/v1/threads")
    async def threads(scenario_id: str | None = None, domain_id: str | None = None):
        return [{**t, "domain_id": t["scenario_id"]} for t in need_runtime().list_threads(domain_id or scenario_id)]

    @app.get("/api/v1/threads/{thread_id}")
    async def thread(thread_id: str):
        return need_runtime().thread_detail(thread_id)

    @app.get("/api/v1/memories")
    async def memories(scenario_id: str | None = None, domain_id: str | None = None):
        need_runtime()
        scenario_id = domain_id or scenario_id
        rt.enterprise.domain(scenario_id)
        return await rt.memory.list(scenario_id)

    @app.patch("/api/v1/memories/{memory_id}")
    async def update_memory(memory_id: str, body: MemoryUpdate, scenario_id: str | None = None, domain_id: str | None = None):
        need_runtime()
        scenario_id = domain_id or scenario_id
        rt.enterprise.domain(scenario_id)
        if (body.content is not None or body.active is True) and rt.memory.embedder is not None:
            record = await rt.memory._get(scenario_id, memory_id)
            if record and record["kind"] != "preference":
                rt.enterprise.require_model_access(scenario_id)
        return await rt.memory.update(scenario_id, memory_id, **body.model_dump(exclude_unset=True))

    @app.delete("/api/v1/memories/{memory_id}")
    async def delete_memory(memory_id: str, scenario_id: str | None = None, domain_id: str | None = None):
        need_runtime()
        scenario_id = domain_id or scenario_id
        rt.enterprise.domain(scenario_id)
        await rt.memory.delete(scenario_id, memory_id)
        return {"deleted": True}

    return app


app = create_app()
