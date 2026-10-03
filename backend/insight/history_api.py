"""Saved evidence browsing only: no business connector or model access."""
import asyncio

from fastapi import APIRouter, HTTPException, Query

from insight.question_catalog import get_questions


def router(rt):
    api = APIRouter(prefix="/api/v1")

    def repository():
        if not rt.ready or rt.repository is None:
            raise HTTPException(503, rt.startup_error or "历史存储尚未就绪。")
        return rt.repository

    @api.get("/questions")
    async def questions(domain_id: str | None = None):
        return get_questions(domain_id)

    @api.get("/runs")
    async def runs(domain_id: str | None = None, status: str | None = None, q: str | None = None,
                   limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
        try:
            return await asyncio.to_thread(repository().list_runs, domain_id, status, q, limit=limit, offset=offset)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @api.get("/runs/{run_id}/queries/{query_id}/rows")
    async def rows(run_id: str, query_id: str, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=20000),
                   sort_by: str | None = None, descending: bool = False):
        try:
            return await asyncio.to_thread(repository().query_rows, run_id, query_id, offset=offset,
                                           limit=limit, sort_by=sort_by, descending=descending)
        except KeyError:
            raise HTTPException(404, "找不到该任务的已保存查询结果；不会重新执行查询。") from None
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    return api
