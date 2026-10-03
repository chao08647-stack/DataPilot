"""Enterprise catalog and dashboard HTTP resources; all DB/model access remains server-side."""
import asyncio

from fastapi import APIRouter, HTTPException


def router(rt):
    api = APIRouter(prefix="/api/v1")

    def service():
        if not rt.ready or rt.enterprise is None:
            raise HTTPException(503, rt.startup_error or "需要启动项目专用 PostgreSQL 后管理业务资产。")
        return rt.enterprise

    @api.get("/templates")
    async def templates():
        return [{"id": s["id"], "title": s["title"], "description": s["description"], "schema_version": s["schema_version"], "dashboard_templates": s.get("dashboard_templates", []), "synthetic": True} for s in rt.scenarios.values()]

    @api.post("/templates/{template_id}/install")
    async def install(template_id: str, body: dict | None = None):
        domain = await asyncio.to_thread(service().install, template_id, upgrade=bool((body or {}).get("upgrade", False)))
        await rt.seed_template(template_id)
        return domain

    @api.get("/data-sources")
    async def sources():
        return service().sources()

    @api.post("/data-sources", status_code=201)
    async def create_source(body: dict):
        return service().create_source(body)

    @api.patch("/data-sources/{source_id}")
    async def update_source(source_id: str, body: dict):
        return service().update_source(source_id, body)

    @api.post("/data-sources/{source_id}/test")
    async def test_source(source_id: str):
        try:
            return await asyncio.to_thread(service().connector(source_id).test)
        except Exception as exc:
            return {"status": "failed", "error": f"连接检查失败（{type(exc).__name__}）；请检查授权配置和服务端环境变量。"}

    @api.post("/data-sources/{source_id}/introspect")
    async def introspect(source_id: str):
        try:
            return await asyncio.to_thread(service().connector(source_id).introspect)
        except Exception as exc:
            raise HTTPException(422, f"元数据读取失败（{type(exc).__name__}）；不会扫描业务行。") from None

    @api.get("/domains")
    async def domains():
        if rt.ready and rt.enterprise:
            return rt.enterprise.domains()
        return [{"id": s["id"], "title": s["title"], "description": s["description"], "model_version": s["schema_version"], "synthetic": True,
                 "date_range": s["date_range"], "metrics": s["metrics"], "examples": [{k: v for k, v in e.items() if k != "sql"} for e in s["examples"]], "dashboard_templates": s.get("dashboard_templates", [])} for s in rt.scenarios.values()]

    @api.get("/domains/{domain_id}")
    async def domain(domain_id: str):
        if rt.ready and rt.enterprise:
            value = service().domain(domain_id)
            return {**value, "definition": service().catalog(domain_id) if value.get("model_version") else None}
        return next(v for v in await domains() if v["id"] == domain_id)

    @api.post("/domains", status_code=201)
    async def create_domain(body: dict):
        return service().create_domain(body)

    @api.get("/semantic-models")
    async def semantic_models(domain_id: str):
        return [m for m in service().store.list("model") if m["domain_id"] == domain_id]

    @api.post("/semantic-models", status_code=201)
    async def create_model(body: dict):
        return service().create_model(body["domain_id"], body["definition"])

    @api.post("/semantic-models/{model_id}/publish")
    async def publish(model_id: str):
        return await asyncio.to_thread(service().publish, model_id)

    @api.get("/dashboards")
    async def dashboards(domain_id: str | None = None):
        service()
        return rt.dashboards.list(domain_id)

    @api.post("/dashboards", status_code=201)
    async def create_dashboard(body: dict):
        service()
        return rt.dashboards.create(body)

    @api.get("/dashboards/{board_id}")
    async def dashboard(board_id: str):
        service()
        return rt.dashboards.get(board_id)

    @api.patch("/dashboards/{board_id}")
    async def update_dashboard(board_id: str, body: dict):
        service()
        return await asyncio.to_thread(rt.dashboards.update, board_id, body)

    @api.delete("/dashboards/{board_id}")
    async def delete_dashboard(board_id: str):
        service().store.delete("dashboard", board_id)
        return {"deleted": True}

    @api.post("/dashboards/{board_id}/cards", status_code=201)
    async def pin(board_id: str, body: dict):
        service()
        return await asyncio.to_thread(rt.dashboards.pin, board_id, body)

    @api.post("/dashboards/{board_id}/refresh")
    async def refresh(board_id: str, body: dict | None = None):
        service()
        return await asyncio.to_thread(rt.dashboards.refresh, board_id, (body or {}).get("filters"))

    @api.post("/dashboards/{board_id}/cards/{card_id}/drilldown")
    async def drilldown(board_id: str, card_id: str, body: dict):
        service()
        return rt.dashboards.drilldown(board_id, card_id, body["dimension"], body["value"])

    return api
