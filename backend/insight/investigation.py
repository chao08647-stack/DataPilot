"""Bounded planning and deterministic analysis nodes, shared by every business domain."""
from insight.models import Investigation, SingleQueryPlan, SQLPlan
from insight.providers import BudgetExhausted, ModelError


class InvestigationNodes:
    def bind_diagnostic_periods(self, state, queries):
        """Apply the structured plan's period scope to input queries, including repairs."""
        import sqlglot
        from sqlglot import exp

        contracts = {c["query_id"]: c for c in self.diagnostic_contracts(state)}
        dialect = self.scene(state).get("dialect", "duckdb")
        for query in queries:
            contract = contracts.get(query.id)
            if not contract or contract.get("period_policy") == "all" or "period" not in contract["output_columns"]:
                continue
            periods = sorted({v for k, v in contract["config"].items() if k in {"baseline_period", "current_period"}})
            if not periods:
                continue
            tree = sqlglot.parse_one(query.sql, read=dialect)
            predicate = exp.In(this=exp.column("period", table="diagnostic_input"), expressions=[exp.Literal.string(p) for p in periods])
            query.sql = exp.select("*").from_(tree.subquery("diagnostic_input")).where(predicate).sql(dialect=dialect)

    def diagnostic_contracts(self, state):
        import sqlglot

        catalog = self.scene(state)
        known = {d["id"]: d for d in catalog.get("diagnostics", [])}
        contracts = []
        for binding in state.get("diagnostic_bindings", []):
            diagnostic = known.get(binding["id"], {})
            for draft in diagnostic.get("queries", []):
                if draft["id"] in binding["query_map"]:
                    tree = sqlglot.parse_one(draft["sql"], read=catalog.get("dialect", "duckdb"))
                    contracts.append({"query_id": binding["query_map"][draft["id"]], "tool": binding["tool"], "period_policy": diagnostic.get("period_policy", "pair"),
                        "output_columns": tree.named_selects, "config": binding.get("config", {}), "reference_input_query": draft["sql"],
                        "non_additive_columns": draft.get("non_additive_columns", [])})
        return contracts

    async def investigate(self, state):
        diagnostics = [d for d in self.scene(state).get("diagnostics", [])
                       if not d.get("supported_query_types") or state.get("intent", {}).get("query_type") in d["supported_query_types"]]
        if state.get("supplementary_count", 0):
            completed_tools = {c["tool"] for c in state.get("calculations", []) if c.get("status") == "ok"}
            diagnostics = [d for d in diagnostics if d["tool"] not in completed_tools]
        value = await self.ask(state, Investigation, "discovery", "你是 Supervisor 分析规划环节。依据已定位指标，制定1至6个可核验步骤，明确目标、指标和事实粒度。选择能完整回答当前问题的最少diagnostic_ids，最多两个。年度各月变化/关注月份属于趋势总览，不等于另做季度利润归因；只有用户要求解释驱动因素时才增加分解工具。不要扩大问题范围。补查时只弥补当前缺口，不重复已有完整证据。不能选择当前数据范围不适用的工具。", {
            "discovery": state["discovery"], "supplement_rule": "已成功的工具不再列为可选；若缺少其输出中没有的对象明细/日期等证据，diagnostic_ids留空，生成针对缺口的独立只读SQL，不重跑已有完整诊断。",
            "existing_evidence": [{k: q.get(k) for k in ("id", "title", "columns", "total_rows", "truncated")} for q in state.get("previous_queries", [])],
            "available_diagnostics": [{k: v for k, v in d.items() if k not in {"queries", "config"}} for d in diagnostics]})
        known = {m["id"] for m in self.scene(state)["metrics"]}
        if any(not set(step.metric_ids).issubset(known) for step in value.steps):
            raise ModelError("分析计划引用了未定义指标。")
        if not set(value.diagnostic_ids).issubset({d["id"] for d in diagnostics}):
            raise ModelError("分析计划选择了未批准的诊断工具。")
        if not set(value.period_selections).issubset(value.diagnostic_ids):
            raise ModelError("对比期引用了未选择的诊断工具。")
        # A continuous trend never becomes a pair of endpoint snapshots. Tool capabilities
        # are published semantics, not model guesses or question-ID routing.
        for diagnostic in diagnostics:
            if diagnostic.get("period_policy") == "all" and diagnostic["id"] in value.period_selections:
                value.period_selections.pop(diagnostic["id"])
                self.emit(state, "plan.scope_adjusted", {"diagnostic_id": diagnostic["id"], "reason": "连续期间工具保留完整报告范围，不按首尾两期裁剪。"}, diagnostic["id"])
        self.emit(state, "artifact.created", {"artifact": "investigation", "value": value.model_dump()})
        return {"investigation": value.model_dump()}

    async def sql(self, state):
        catalog = self.scene(state)
        selected_ids = set(state.get("investigation", {}).get("diagnostic_ids", []))
        blueprints, bindings = [], []
        for diagnostic in catalog.get("diagnostics", []):
            if diagnostic["id"] not in selected_ids:
                continue
            query_map = {}
            for draft in diagnostic["queries"]:
                query_id = f"q{state['supplementary_count']+1}_{len(blueprints)+1}"
                query_map[draft["id"]] = query_id
                blueprints.append({**draft, "id": query_id, "input_id": draft["id"], "diagnostic_id": diagnostic["id"]})
            config = {k: v for k, v in diagnostic.get("config", {}).items() if k not in {"baseline_period", "current_period"}}
            config.update(state.get("investigation", {}).get("period_selections", {}).get(diagnostic["id"], {}))
            bindings.append({"id": diagnostic["id"], "tool": diagnostic["tool"], "query_map": query_map, "config": config})
        drafts = []
        for blueprint in blueprints or [None]:
            import sqlglot
            output_columns = sqlglot.parse_one(blueprint["sql"], read=catalog.get("dialect", "duckdb")).named_selects if blueprint else []
            value = await self.ask(state, SingleQueryPlan if blueprint else SQLPlan, "sql", "按分析步骤生成只读查询，使用catalog.dialect方言。不同事实粒度可分别查询；经批准关系预聚合对齐时可联合查询。每条查询明确metric_ids，保持正式时间、状态、分母和单位。输出列唯一命名，时间升序。若有diagnostic_blueprint，本次只返回该一个输入查询、严格保留蓝图输出列名及事实粒度；其他输入由其他子任务处理。不得为整份问题另加查询、把输出改成其他诊断粒度。蓝图是计算骨架，必须依当次问题及source_context调整时间与维度过滤，遵守period_selections。不扩大范围或偷取基期；基期不足交给工具说明不足。无蓝图则设计最少必要查询。", {
                "intent": state["intent"], "discovery": state["discovery"], "investigation": state.get("investigation", {}), "diagnostic_blueprint": blueprint, "required_output_columns": output_columns,
                "previous_queries": state.get("previous_queries", []), "remaining_query_budget": 12-len(state.get("attempts", {}))-len(drafts)})
            self.emit(state, "sql.draft_created", {"validated": False, "queries": value.model_dump()["queries"]}, blueprint["id"] if blueprint else "general")
            if blueprint:
                if len(value.queries) != 1:
                    raise ModelError("单个诊断输入只能生成一条查询。")
                # Query identifiers are orchestration metadata, never delegated to model naming.
                value.queries[0].id = blueprint["id"]
            drafts.extend(value.queries)
        value = SQLPlan(queries=drafts)
        known_metrics = {m["id"] for m in catalog["metrics"]}
        required = set(state["discovery"]["tables"])
        for index, draft in enumerate(value.queries, 1):
            if not blueprints:
                draft.id = f"q{state['supplementary_count']+1}_{index}"
            if not set(draft.metric_ids).issubset(known_metrics):
                raise ModelError("SQL 引用了未批准指标。")
            if not draft.metric_ids:
                draft.metric_ids = state["discovery"]["metric_ids"]
            for metric in catalog["metrics"]:
                if metric["id"] in draft.metric_ids:
                    required.update(metric["tables"])
        if len(set(state.get("attempts", {})) | {q.id for q in value.queries}) > 12:
            raise BudgetExhausted("已达到十二个 SQL 子任务上限。")
        if blueprints:
            import sqlglot
            from sqlglot import exp
            known_tables = {t["name"] for t in catalog["tables"]}
            for blueprint in blueprints:
                for table in sqlglot.parse_one(blueprint["sql"], read=catalog.get("dialect", "duckdb")).find_all(exp.Table):
                    full = f"{table.db}.{table.name}" if table.db else table.name
                    if full in known_tables:
                        required.add(full)
        self.bind_diagnostic_periods({**state, "diagnostic_bindings": bindings}, value.queries)
        self.emit(state, "artifact.created", {"artifact": "sql_plan", "value": value.model_dump()})
        return {"plan": value.model_dump(), "queries": [], "error": {}, "diagnostic_bindings": bindings,
                "discovery": {**state["discovery"], "tables": sorted(required)}}

    def calculate(self, state):
        from insight.analytics import run_analysis
        calculations, derived = [], []
        current = {q["id"]: q for q in self.full_queries(state, state["queries"])}
        for binding in state.get("diagnostic_bindings", []):
            inputs = [{**current[new], "id": old} for old, new in binding["query_map"].items() if new in current]
            result = run_analysis(binding["tool"], inputs, binding.get("config", {}))
            result["evidence_ids"] = [binding["query_map"].get(q, q) for q in result.get("evidence_ids", [])]
            for fact in result.get("facts", []):
                fact["evidence_ids"] = [binding["query_map"].get(q, q) for q in fact.get("evidence_ids", [])]
            result["diagnostic_id"] = binding["id"]
            result["recipe"] = {"tool": binding["tool"], "config": binding.get("config", {}), "query_map": binding["query_map"]}
            rows = result.get("series", [])
            outputs = [{"id": "series", "rows": rows}, *result.get("tables", [])]
            table_refs = []
            for output in outputs:
                table_rows = output.get("rows", [])
                if not table_rows or not isinstance(table_rows[0], dict):
                    continue
                suffix = "" if output["id"] == "series" else "_" + output["id"]
                query_id = f"calc{state['supplementary_count']}_{binding['id']}{suffix}"
                columns = output.get("columns") or list(table_rows[0])
                derived.append({"id": query_id, "title": output.get("title", binding["id"]), "columns": columns,
                                "rows": [[row.get(c) for c in columns] for row in table_rows], "derived": True, "truncated": False, "metric_ids": [],
                                "evidence_ids": result["evidence_ids"], "method": result.get("method"), "sql": "", "recipe_output": output["id"]})
                derived[-1]["non_additive_columns"] = output.get("non_additive_columns", [])
                table_refs.append({"id": output["id"], "query_id": query_id, "title": output.get("title"), "row_count": len(table_rows)})
                if output["id"] == "series":
                    result["derived_query_id"] = query_id
            # Never checkpoint full tool tables or duplicate them into every model request.
            result.pop("series", None)
            result.pop("tables", None)
            result["output_tables"] = table_refs
            calculations.append(result)
            self.emit(state, "analysis.tool_used", {"tool": binding["tool"], "diagnostic_id": binding["id"], "status": result.get("status"), "evidence_ids": result["evidence_ids"]}, binding["id"])
        return calculations, derived
