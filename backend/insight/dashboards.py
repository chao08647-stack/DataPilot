"""Persisted query definitions, not screenshots. Refresh never calls a language model."""
from __future__ import annotations

import calendar
import math
from copy import deepcopy
from datetime import date
from threading import Lock
from uuid import uuid4

import sqlglot
from sqlglot import exp

from insight.charts import KINDS, validate_charts
from insight.enterprise import now


def filtered_query(sql, bindings, filters, dialect):
    """Bind values, quote identifiers with AST; never interpolate user filter text."""
    base = sqlglot.parse_one(sql, read=dialect)
    query = exp.select("*").from_(base.subquery("board_data"))
    params, ignored = [], []
    for name, value in filters.items():
        if value is None or value == "" or value == []:
            continue
        column = bindings.get(name)
        if not column:
            ignored.append(name)
            continue
        if not isinstance(column, str) or column not in base.named_selects:
            raise ValueError("筛选绑定必须引用查询的明确输出列。")
        field = exp.column(column, quoted=True)
        if name in {"date_from", "date_to"}:
            date.fromisoformat(str(value))
            predicate = exp.GTE if name == "date_from" else exp.LTE
            condition = predicate(this=exp.cast(field, "DATE"), expression=exp.cast(exp.Placeholder(), "DATE"))
            params.append(value)
        elif isinstance(value, list):
            if len(value) > 100 or any(not isinstance(x, (str, int, float, bool)) for x in value):
                raise ValueError("筛选值超过上限或类型不支持。")
            condition = exp.In(this=field, expressions=[exp.Placeholder() for _ in value])
            params.extend(value)
        else:
            if not isinstance(value, (str, int, float, bool)):
                raise ValueError("筛选值必须为标量或数组。")
            condition = exp.EQ(this=field, expression=exp.Placeholder())
            params.append(value)
        query = query.where(condition)
    return query.sql(dialect=dialect), params, ignored


class Dashboards:
    def __init__(self, enterprise):
        self.enterprise, self.store = enterprise, enterprise.store
        self.locks = {}

    def list(self, domain_id=None):
        return [b for b in self.store.list("dashboard") if domain_id is None or b["domain_id"] == domain_id]

    def get(self, board_id):
        return self.store.get("dashboard", board_id)

    def create(self, body):
        if set(body) - {"title", "domain_id", "template_id"}:
            raise ValueError("未知看板创建字段。")
        domain = self.enterprise.domain(body["domain_id"])
        if not body.get("title") or len(body["title"]) > 160:
            raise ValueError("看板需要有效标题。")
        if not domain.get("model_version"):
            raise ValueError("请先发布业务域语义模型。")
        catalog = self.enterprise.catalog(domain["id"])
        templates = catalog.get("dashboard_templates", [])
        cards = []
        if body.get("template_id"):
            template = next((v for v in templates if v["id"] == body["template_id"]), None)
            if not template:
                raise ValueError("看板模板不属于此业务域。")
            cards = [self.validate_card(c, catalog, index) for index, c in enumerate(template["cards"])]
        return self.store.put("dashboard", {"id": str(uuid4()), "title": body["title"], "domain_id": domain["id"], "model_version": domain["model_version"],
            "filters": {}, "cards": cards, "created_at": now(), "last_refresh_at": None}, create=True)

    def validate_card(self, card, catalog, order=0):
        card = deepcopy(card)
        if set(card) - {"id", "title", "chart", "query", "queries", "analysis_recipe", "layout", "snapshot", "provenance", "filter_bindings", "drilldown"}:
            raise ValueError("卡片包含不支持字段。")
        if not card.get("title") or not isinstance(card.get("query", {}).get("sql"), str):
            raise ValueError("卡片需要名称和查询定义。")
        if len(card["query"]["sql"]) > 40000:
            raise ValueError("查询过长。")
        chart = card.get("chart", {})
        if chart.get("type") not in KINDS:
            raise ValueError("不支持的图表类型。")
        metrics = {m["id"] for m in catalog["metrics"]}
        if not set(card["query"].get("metric_ids", [])).issubset(metrics):
            raise ValueError("卡片指标不属于当前模型。")
        from insight.sql import validate_sql
        dialect = catalog.get("dialect", "duckdb")
        if dialect == "postgres":
            dialect = "postgres"
        # Final execution revalidates with the connector's schema/table scope.
        schemas = sorted({t["name"].split(".", 1)[0] for t in catalog["tables"] if "." in t["name"]})
        validate_sql(card["query"]["sql"], catalog, [t["name"] for t in catalog["tables"]], dialect=dialect, schemas=schemas or None, allow_subqueries=True)
        bindings = card["query"].setdefault("filter_bindings", card.pop("filter_bindings", {}))
        allowed_filters = {"date_from", "date_to"} | {d["id"] for d in catalog.get("dimensions", [])}
        if set(bindings) - allowed_filters:
            raise ValueError("筛选项未在业务域维度中定义。")
        for name, column in bindings.items():
            filtered_query(card["query"]["sql"], {name: column}, {name: "2025-01-01" if name.startswith("date_") else "__validate__"}, dialect)
        layout = card.get("layout", {})
        if layout.get("width", 6) not in {6, 12} or not isinstance(layout.get("order", order), int):
            raise ValueError("卡片宽度只能为 6 或 12，顺序必须是整数。")
        card.update(id=card.get("id") or str(uuid4()), layout={"width": layout.get("width", 6), "order": layout.get("order", order)})
        return card

    def update(self, board_id, body):
        with self.locks.setdefault(board_id, Lock()):
            board = self.get(board_id)
            if set(body) - {"title", "filters", "cards"}:
                raise ValueError("只能修改名称、筛选和卡片布局。")
            if "title" in body and (not body["title"] or len(body["title"]) > 160):
                raise ValueError("看板名称无效。")
            catalog = self.enterprise.catalog(board["domain_id"], board["model_version"])
            value = {**board, **deepcopy(body)}
            if "cards" in body:
                if len(body["cards"]) > 24:
                    raise ValueError("单个看板最多 24 张卡片。")
                old = {c["id"]: c for c in board["cards"]}
                cards = []
                for index, incoming in enumerate(body["cards"]):
                    if incoming.get("id") not in old:
                        raise ValueError("请通过添加卡片接口创建卡片。")
                    original = old[incoming["id"]]
                    if set(incoming) <= {"id", "title", "layout"}:
                        incoming = {**original, **incoming}
                    validated = self.validate_card(incoming, catalog, index)
                    # Never accept client-supplied cached evidence or provenance.
                    validated["snapshot"] = original.get("snapshot")
                    validated["provenance"] = original.get("provenance")
                    if any(validated.get(k) != original.get(k) for k in ("query", "queries", "analysis_recipe", "chart")):
                        validated["snapshot"] = None
                    cards.append(validated)
                if len({c["id"] for c in cards}) != len(cards):
                    raise ValueError("卡片 ID 重复。")
                value["cards"] = cards
            self.validate_filters(value["filters"], catalog)
            return self.store.put("dashboard", value)

    def pin(self, board_id, body):
        with self.locks.setdefault(board_id, Lock()):
            board = self.get(board_id)
            if len(board["cards"]) >= 24:
                raise ValueError("单个看板最多 24 张卡片。")
            catalog = self.enterprise.catalog(board["domain_id"], board["model_version"])
            if "run_id" in body:
                run = self.enterprise.repository.get_run(body["run_id"])
                if run["status"] != "completed" or run["scenario_id"] != board["domain_id"]:
                    raise ValueError("只能添加同一业务域已完成任务的证据。")
                artifact = run["artifacts"]
                if artifact.get("partial"):
                    raise ValueError("未完成最终复核的部分结果不能加入看板。")
                if not artifact.get("model_version") or artifact["model_version"] != board["model_version"]:
                    raise ValueError("分析与看板使用不同模型版本，请创建新版本看板。")
                query = next((q for q in artifact.get("queries", []) if q["id"] == body["query_id"]), None)
                if not query:
                    raise ValueError("查询证据不存在。")
                charts = [c for c in artifact.get("charts", []) if c["query_id"] == query["id"]]
                chart = charts[min(body.get("chart_index", 0), len(charts)-1)] if charts else {"type": "table", "title": "查询明细"}
                chart = {k: v for k, v in chart.items() if k != "query_id"}
                columns = set(query["columns"])
                def pinned_sql(source):
                    # Expand a trusted executed artifact's columns, including server-added SELECT * wrappers.
                    tree = sqlglot.parse_one(source.get("source_sql", source["sql"]), read=catalog.get("dialect", "duckdb"))
                    return exp.select(*[exp.column(c, table="pinned_data", quoted=True) for c in source["columns"]]).from_(tree.subquery("pinned_data")).sql(dialect=catalog.get("dialect", "duckdb"))
                bindings = {d["id"]: d["column"] for d in catalog.get("dimensions", []) if d.get("column") in columns}
                card = {"title": chart.get("title", "分析结果"), "chart": chart,
                    "query": {"sql": "" if query.get("derived") else pinned_sql(query), "metric_ids": query.get("metric_ids", []), "filter_bindings": bindings,
                              "non_additive_columns": query.get("non_additive_columns", [])},
                    "provenance": {"run_id": run["run_id"], "query_id": query["id"], "analysis_generated_at": run["updated_at"]}}
                if query.get("derived"):
                    calculation = next((c for c in artifact.get("calculations", []) if c.get("derived_query_id") == query["id"] or any(t.get("query_id") == query["id"] for t in c.get("output_tables", []))), None)
                    if not calculation or not calculation.get("recipe"):
                        raise ValueError("缺少可重新执行的计算定义。")
                    recipe = calculation["recipe"]
                    by_id = {q["id"]: q for q in artifact["queries"]}
                    definitions = []
                    for alias, source_id in recipe["query_map"].items():
                        source = by_id[source_id]
                        approved_bindings = {d["id"]: d["column"] for d in catalog.get("dimensions", []) if d.get("column") in source["columns"]}
                        definitions.append({"id": alias, "sql": pinned_sql(source), "metric_ids": source.get("metric_ids", []), "filter_bindings": approved_bindings,
                                            "non_additive_columns": source.get("non_additive_columns", [])})
                    card.update(query=definitions[0], queries=definitions, analysis_recipe={"tool": recipe["tool"], "config": recipe["config"], "output": query.get("recipe_output", "series")})
            else:
                card = {k: v for k, v in body.items() if k not in {"snapshot", "provenance"}}
            card = self.validate_card(card, catalog, len(board["cards"]))
            return self.store.put("dashboard", {**board, "cards": board["cards"] + [card]})

    def validate_filters(self, filters, catalog):
        if not isinstance(filters, dict):
            raise ValueError("filters 必须为对象。")
        if set(filters) - ({"date_from", "date_to"} | {d["id"] for d in catalog.get("dimensions", [])}):
            raise ValueError("筛选维度未在语义模型中批准。")
        for field in ("date_from", "date_to"):
            if filters.get(field):
                date.fromisoformat(filters[field])
        if filters.get("date_from") and filters.get("date_to") and filters["date_from"] > filters["date_to"]:
            raise ValueError("开始日期不能晚于结束日期。")

    def refresh(self, board_id, filters=None):
        lock = self.locks.setdefault(board_id, Lock())
        if not lock.acquire(blocking=False):
            raise ValueError("看板正在刷新或编辑，请稍后再试。")
        try:
            board = self.get(board_id)
            current = self.enterprise.domain(board["domain_id"])
            if current["model_version"] != board["model_version"]:
                raise ValueError("语义模型版本已变化；原看板保留历史结果，请用新模型重新创建或重新校验卡片。")
            catalog = self.enterprise.catalog(board["domain_id"], board["model_version"])
            filters = deepcopy(board["filters"] if filters is None else filters)
            self.validate_filters(filters, catalog)
            refreshed_at, refresh_id = now(), str(uuid4())
            cards = []
            for original in board["cards"]:
                card = deepcopy(original)
                try:
                    definitions = card.get("queries") or [card["query"]]
                    queries, ignored = [], set()
                    for index, definition in enumerate(definitions):
                        if definition.get("filter_grain") in {"month", "month_end"}:
                            if filters.get("date_from") and date.fromisoformat(filters["date_from"]).day != 1:
                                raise ValueError("月度卡片的开始日期必须是月初；不支持从月汇总推断任意日区间。")
                            if filters.get("date_to"):
                                end = date.fromisoformat(filters["date_to"])
                                if end.day != calendar.monthrange(end.year, end.month)[1]:
                                    raise ValueError("月度卡片的结束日期必须是月末。")
                        sql, parameters, skipped = filtered_query(definition["sql"], definition.get("filter_bindings", {}), filters, catalog.get("dialect", "duckdb"))
                        if skipped:
                            raise ValueError("该卡片不适用筛选：" + "、".join(skipped) + "；未执行未筛选的替代查询。")
                        query = self.enterprise.execute(board["domain_id"], sql, [t["name"] for t in catalog["tables"]], f"board:{refresh_id}:{card['id']}:{index}",
                            version=board["model_version"], parameters=parameters)
                        query["id"] = definition.get("id", f"{card['id']}-{index}")
                        query["metric_ids"] = definition.get("metric_ids", [])
                        query["non_additive_columns"] = definition.get("non_additive_columns", [])
                        query["source_sql"] = definition["sql"]
                        queries.append(query)
                        ignored.update(skipped)
                    query = queries[0]
                    calculation = None
                    if card.get("analysis_recipe"):
                        from insight.analytics import run_analysis
                        recipe = card["analysis_recipe"]
                        calculation = run_analysis(recipe["tool"], queries, recipe.get("config", {}))
                        if calculation.get("status") == "insufficient_data":
                            raise ValueError("诊断证据不足：" + "；".join(calculation.get("limitations", [])))
                        output_id = recipe.get("output", "series")
                        rows = calculation.get("series", []) if output_id == "series" else next((t["rows"] for t in calculation.get("tables", []) if t["id"] == output_id), [])
                        if not rows:
                            raise ValueError("计算结果为空或所选输出已不存在，保留最近成功快照。")
                        if isinstance(rows, list) and rows and isinstance(rows[0], dict):
                            columns = list(rows[0])
                            query = {"id": f"{card['id']}-calculation", "columns": columns, "rows": [[row.get(c) for c in columns] for row in rows], "derived": True, "truncated": False}
                    # Refresh data, not the user's selected visualization. A tool's main
                    # chart may describe a different output (e.g. bridge vs channel table).
                    defaults = (calculation or {}).get("chart") or {}
                    if calculation and card.get("analysis_recipe", {}).get("output", "series") != "series":
                        defaults = next((t.get("chart", {}) for t in calculation.get("tables", []) if t["id"] == card["analysis_recipe"]["output"]), {})
                    chart = {**defaults, **card["chart"], "title": card["title"], "query_id": query["id"]}
                    if chart.get("type") == "waterfall" and defaults.get("type") == "waterfall" and "start_value" in defaults:
                        chart["start_value"] = defaults["start_value"]
                    chart = validate_charts([chart], [query])[0]
                    card["snapshot"] = {"query": query, "queries": queries, "chart": chart, "calculation": calculation,
                        "status": "ready", "stale": False, "queried_at": refreshed_at, "filters": filters, "unapplied_filters": sorted(ignored),
                        "data_range": catalog.get("date_range", {}), "data_updated_at": None, "analysis_generated_at": (card.get("provenance") or {}).get("analysis_generated_at")}
                except Exception as exc:
                    # Driver diagnostics can contain hosts or rows; do not expose arbitrary exception text.
                    card["snapshot"] = {**(card.get("snapshot") or {}), "status": "failed", "stale": True, "attempted_at": refreshed_at,
                        "requested_filters": filters, "error": str(exc)[:400] if isinstance(exc, ValueError) else f"查询失败（{type(exc).__name__}），请检查连接、口径或字段。"}
                cards.append(card)
            return self.store.put("dashboard", {**board, "cards": cards, "filters": filters, "last_refresh_at": refreshed_at})
        finally:
            lock.release()

    def drilldown(self, board_id, card_id, dimension, value):
        board = self.get(board_id)
        card = next((c for c in board["cards"] if c["id"] == card_id), None)
        if not card:
            raise KeyError(card_id)
        catalog = self.enterprise.catalog(board["domain_id"], board["model_version"])
        approved = {d["id"]: d for d in catalog.get("dimensions", [])}
        if dimension not in approved or dimension not in card["query"].get("filter_bindings", {}):
            raise ValueError("该卡片没有批准的下钻维度。")
        if not isinstance(value, (str, int, float)) or isinstance(value, float) and not math.isfinite(value):
            raise ValueError("下钻值无效。")
        filters = {**board["filters"], dimension: value}
        return {"domain_id": board["domain_id"], "question": f"进一步分析「{card['title']}」，筛选条件：{filters}。请基于同一指标口径定位差异并列出证据。",
            "context": {"dashboard_id": board_id, "card_id": card_id, "filters": filters}}
