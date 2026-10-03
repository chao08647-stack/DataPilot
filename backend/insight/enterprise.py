"""Versioned, local-single-user data catalog. No credentials or business rows are stored here."""
from __future__ import annotations

import json
import re
from copy import deepcopy
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from threading import RLock
from uuid import uuid4

from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, model_validator


def now():
    return datetime.now(UTC).isoformat()


def identifier(value):
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,95}", value):
        raise ValueError("资源 ID 只能包含字母、数字、下划线、短横线和点。")
    return value


class SourceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(default_factory=lambda: str(uuid4()))
    name: str = Field(min_length=1, max_length=120)
    kind: str
    config: dict
    model_access: dict[str, bool] = Field(default_factory=lambda: {"metadata": False, "results": False})

    @model_validator(mode="after")
    def validate_source(self):
        identifier(self.id)
        if self.kind not in {"duckdb", "postgres"}:
            raise ValueError("当前仅启用 DuckDB 与 PostgreSQL；MySQL 接入已暂停。")
        allowed = {"path"} if self.kind == "duckdb" else {"host", "port", "database", "user", "password_env", "schemas", "allowed_tables"}
        if set(self.config) - allowed:
            raise ValueError("连接配置包含不支持的字段；密码只能通过 password_env 引用服务端环境变量。")
        required = {"path"} if self.kind == "duckdb" else {"host", "database", "user", "password_env", "allowed_tables"}
        if not required.issubset(self.config):
            raise ValueError("缺少连接配置或授权表清单。")
        if self.kind != "duckdb":
            if not re.fullmatch(r"INSIGHT_SOURCE_[A-Z0-9_]+", str(self.config["password_env"])):
                raise ValueError("密码变量必须使用 INSIGHT_SOURCE_ 前缀，不能引用应用或模型密钥。")
            for key in ("schemas", "allowed_tables"):
                if key in self.config and (not isinstance(self.config[key], list) or not all(isinstance(v, str) for v in self.config[key])):
                    raise ValueError("schemas 和 allowed_tables 必须为字符串数组。")
            if not self.config["allowed_tables"]:
                raise ValueError("必须显式选择授权表。")
            if not isinstance(self.config.get("port", 5432 if self.kind == "postgres" else 3306), int):
                raise ValueError("端口必须为整数。")
        if set(self.model_access) - {"metadata", "results"}:
            raise ValueError("模型授权仅支持 metadata 与 results。")
        self.model_access = {k: self.model_access.get(k, False) for k in ("metadata", "results")}
        return self


class ResourceStore:
    """A separate table; existing run/message/checkpoint tables remain intact."""
    def __init__(self, repository):
        self.repository = repository
        self.lock = RLock()
        self.memory = {} if repository.is_memory else None

    def setup(self, *, migration=False):
        if self.memory is not None:
            return
        with self.repository.connect() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(194801001)")
            if conn.execute("SELECT to_regclass('ia_resources') AS name").fetchone()["name"]:
                return
            active = conn.execute("SELECT count(*) AS n FROM ia_runs WHERE status IN ('queued','running','waiting_for_input')").fetchone()["n"]
            if active:
                raise ValueError("存在未结束任务；完成或取消后才能升级。")
            if not migration and conn.execute("SELECT count(*) AS n FROM ia_runs").fetchone()["n"]:
                raise ValueError("旧应用库需要先运行 migrate_enterprise.py 备份并升级。")
            conn.execute("""CREATE TABLE ia_resources (
                kind TEXT NOT NULL, id TEXT NOT NULL, payload JSONB NOT NULL,
                revision BIGINT NOT NULL DEFAULT 1, updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY(kind,id));""")

    def list(self, kind):
        if self.memory is not None:
            with self.lock:
                return [deepcopy(v) for (k, _), v in self.memory.items() if k == kind]
        with self.repository.connect() as conn:
            return [r["payload"] for r in conn.execute("SELECT payload FROM ia_resources WHERE kind=%s ORDER BY id", (kind,)).fetchall()]

    def get(self, kind, resource_id):
        if self.memory is not None:
            with self.lock:
                return deepcopy(self.memory[(kind, resource_id)])
        with self.repository.connect() as conn:
            row = conn.execute("SELECT payload FROM ia_resources WHERE kind=%s AND id=%s", (kind, resource_id)).fetchone()
        if not row:
            raise KeyError(resource_id)
        return row["payload"]

    def put(self, kind, value, *, create=False):
        value = {**deepcopy(value), "updated_at": now()}
        if self.memory is not None:
            with self.lock:
                if create and (kind, value["id"]) in self.memory:
                    raise ValueError("资源已存在。")
                self.memory[(kind, value["id"])] = value
        else:
            with self.repository.connect() as conn:
                if create:
                    row = conn.execute("INSERT INTO ia_resources(kind,id,payload) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING RETURNING id", (kind, value["id"], Jsonb(value))).fetchone()
                    if not row:
                        raise ValueError("资源已存在。")
                else:
                    conn.execute("INSERT INTO ia_resources(kind,id,payload) VALUES (%s,%s,%s) ON CONFLICT(kind,id) DO UPDATE SET payload=excluded.payload,revision=ia_resources.revision+1,updated_at=now()", (kind, value["id"], Jsonb(value)))
        return value

    def delete(self, kind, resource_id):
        self.get(kind, resource_id)
        if self.memory is not None:
            with self.lock:
                del self.memory[(kind, resource_id)]
        else:
            with self.repository.connect() as conn:
                conn.execute("DELETE FROM ia_resources WHERE kind=%s AND id=%s", (kind, resource_id))


CATALOG_KEYS = {"id", "title", "description", "schema_version", "date_range", "reporting_range", "observation_end", "data_version", "tables", "relations", "metrics", "dimensions", "capabilities", "diagnostics", "dashboard_templates", "synthetic", "dialect", "template_id"}


class Enterprise:
    def __init__(self, repository, templates, paths, settings):
        self.repository, self.templates, self.paths, self.settings = repository, templates, paths, settings
        self.store = ResourceStore(repository)
        self.connectors = {}
        self.lock = RLock()

    def setup(self):
        self.store.setup()
        for template_id in self.templates:
            self.install(template_id)

    def install(self, template_id, *, upgrade=False):
        template = self.templates[template_id]
        domain_id, version = template_id, template["schema_version"]
        definition = {k: deepcopy(v) for k, v in template.items() if k in CATALOG_KEYS}
        definition.update(id=domain_id, template_id=template_id, synthetic=True, dialect="duckdb")
        existing = {r["id"] for r in self.store.list("domain")}
        if domain_id in existing:
            if not upgrade:
                return self.domain(domain_id)
            if self.has_active(domain_id):
                raise ValueError("存在未结束任务，不能升级业务模板。")
            current = self.catalog(domain_id)
            old_content = {k: v for k, v in current.items() if k != "schema_version"}
            new_content = {k: v for k, v in definition.items() if k != "schema_version"}
            if old_content == new_content:
                return self.domain(domain_id)
            version += "-" + sha256(json.dumps(new_content, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:8]
            definition["schema_version"] = version
        model_id = f"{domain_id}:{version}"
        source_id = f"sample-{domain_id}-{version}"
        self.store.put("source", {"id": source_id, "name": f"{template['title']} · 合成数据", "kind": "duckdb", "config": {"path": str(self.paths[template_id])}, "model_access": {"metadata": True, "results": True}, "builtin": True})
        self.store.put("model", {"id": model_id, "domain_id": domain_id, "version": version, "status": "published", "definition": definition})
        self.store.put("domain", {"id": domain_id, "title": template["title"], "description": template["description"], "data_source_id": source_id, "model_version": version, "model_id": model_id, "synthetic": True, "template_id": template_id}, create=domain_id not in existing)
        return self.domain(domain_id)

    def sources(self):
        return self.store.list("source")

    def create_source(self, body):
        value = SourceInput.model_validate(body).model_dump()
        if value["kind"] == "duckdb":
            path = Path(value["config"]["path"]).resolve()
            if not path.is_relative_to(self.settings.data_dir.resolve()) or path.suffix.lower() != ".duckdb" or not path.is_file():
                raise ValueError("DuckDB 只能连接项目 data 目录内已存在的 .duckdb 文件。")
            value["config"]["path"] = str(path)
        return self.store.put("source", {**value, "builtin": False}, create=True)

    def update_source(self, source_id, body):
        old = self.store.get("source", source_id)
        if set(body) - {"name", "model_access"}:
            raise ValueError("连接范围不可原位修改；请新建数据源。仅允许更新名称和模型授权。")
        value = {**old, **body}
        access = value["model_access"]
        if set(access) - {"metadata", "results"} or not all(isinstance(v, bool) for v in access.values()):
            raise ValueError("无效的模型授权范围。")
        value["model_access"] = {k: access.get(k, False) for k in ("metadata", "results")}
        return self.store.put("source", value)

    def connector(self, source_id):
        from insight.connectors import connector_for
        if source_id not in self.connectors:
            self.connectors[source_id] = connector_for(self.store.get("source", source_id))
        return self.connectors[source_id]

    def domain(self, domain_id):
        domain = self.store.get("domain", domain_id)
        definition = self.catalog(domain_id) if domain.get("model_id") else {}
        template = self.templates.get(domain.get("template_id"), {})
        return {**domain, "date_range": definition.get("date_range", {}), "reporting_range": definition.get("reporting_range"),
                "observation_end": definition.get("observation_end"), "data_version": definition.get("data_version"),
                "metrics": definition.get("metrics", []), "tables": definition.get("tables", []),
                "dimensions": definition.get("dimensions", []), "capabilities": definition.get("capabilities", []),
                "dashboard_templates": definition.get("dashboard_templates", []),
                "examples": [{k: v for k, v in e.items() if k != "sql"} for e in template.get("examples", [])]}

    def domains(self):
        return [self.domain(r["id"]) for r in self.store.list("domain")]

    def create_domain(self, body):
        if set(body) - {"id", "title", "description", "data_source_id"}:
            raise ValueError("未知业务域字段。")
        domain_id = identifier(body.get("id") or str(uuid4()))
        source = self.store.get("source", body["data_source_id"])
        if not str(body.get("title", "")).strip():
            raise ValueError("业务域需要名称。")
        return self.store.put("domain", {"id": domain_id, "title": body["title"], "description": body.get("description", ""), "data_source_id": source["id"], "synthetic": False, "model_version": None, "model_id": None}, create=True)

    def catalog(self, domain_id, version=None):
        domain = self.store.get("domain", domain_id)
        model_id = f"{domain_id}:{version}" if version else domain.get("model_id")
        if not model_id:
            raise ValueError("请先发布该业务域的语义模型。")
        model = self.store.get("model", model_id)
        if model["status"] != "published":
            raise ValueError("语义模型尚未发布。")
        return deepcopy(model["definition"])

    def create_model(self, domain_id, definition):
        domain = self.store.get("domain", domain_id)
        if not isinstance(definition, dict) or set(definition) - CATALOG_KEYS:
            raise ValueError("模型只接受表、指标、关联、维度和批准的分析能力。")
        version = uuid4().hex[:12]
        source = self.store.get("source", domain["data_source_id"])
        definition = {**deepcopy(definition), "id": domain_id, "title": domain["title"], "schema_version": version, "dialect": source["kind"], "synthetic": domain["synthetic"]}
        return self.store.put("model", {"id": f"{domain_id}:{version}", "domain_id": domain_id, "version": version, "status": "draft", "definition": definition}, create=True)

    def has_active(self, domain_id):
        for thread in self.repository.list_threads(domain_id):
            if any(r["status"] in {"queued", "running", "waiting_for_input"} for r in self.repository.thread_detail(thread["thread_id"])["runs"]):
                return True
        return False

    def publish(self, model_id):
        with self.lock:
            model = self.store.get("model", model_id)
            if self.has_active(model["domain_id"]):
                raise ValueError("业务域存在未结束任务，不能切换模型版本。")
            domain = self.store.get("domain", model["domain_id"])
            definition = model["definition"]
            validate_definition(definition)
            actual = self.connector(domain["data_source_id"]).introspect()
            by_name = {t["name"]: set(t["columns"]) for t in actual}
            for table in definition["tables"]:
                if table["name"] not in by_name or not set(table["columns"]).issubset(by_name[table["name"]]):
                    raise ValueError("模型包含授权元数据中不存在的表或字段。")
            published = self.store.put("model", {**model, "status": "published"})
            self.store.put("domain", {**domain, "model_version": model["version"], "model_id": model_id})
            return published

    def require_model_access(self, domain_id):
        domain = self.store.get("domain", domain_id)
        source = self.store.get("source", domain["data_source_id"])
        if not all(source.get("model_access", {}).get(k) for k in ("metadata", "results")):
            raise ValueError("此数据源未授权向模型发送元数据和查询结果；请先确认授权范围。")
        return domain

    def create_run(self, domain_id, question, thread_id, expected_version, context, *, question_id=None, retry_failed=False):
        with self.lock:
            domain = self.require_model_access(domain_id)
            if domain["model_version"] != expected_version:
                raise ValueError("模型刚刚发生变更，请重新确认分析范围。")
            catalog = self.catalog(domain_id, expected_version)
            data_version = catalog.get("data_version") or catalog["schema_version"]
            from insight.question_catalog import catalog_run_key
            key = catalog_run_key(question_id, domain_id, data_version, expected_version) if question_id else None
            run = self.repository.create_run(domain_id, question, thread_id, origin="question_catalog" if question_id else "user",
                question_id=question_id, data_version=data_version, model_version=expected_version, idempotency_key=key, retry_failed=retry_failed)
            if run.get("reused"):
                return run
            return self.repository.update_run(run["run_id"], artifacts={"domain_id": domain_id, "model_version": expected_version, "source_context": context})

    def execute(self, domain_id, sql, allowed, query_id, *, version=None, parameters=None, limit=None):
        domain = self.store.get("domain", domain_id)
        catalog = self.catalog(domain_id, version)
        return self.connector(domain["data_source_id"]).execute(sql, catalog, allowed, query_id, parameters=parameters,
                timeout=self.settings.sql_timeout, limit=limit or self.settings.sql_preview_rows)


def validate_definition(definition):
    from insight.analytics import TOOLS

    def text(value):
        return isinstance(value, str) and bool(value.strip())

    def records(value, label):
        if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
            raise ValueError(f"{label} 必须为对象数组。")
        return value

    def strings(value, label):
        if not isinstance(value, list) or any(not text(item) for item in value):
            raise ValueError(f"{label} 必须为非空字符串数组。")
        return value

    def unique_id(record, seen, label, *, required=True):
        value = record.get("id")
        if "id" not in record and not required:
            return
        if not text(value):
            raise ValueError(f"{label} 需要非空字符串 ID。")
        if value in seen:
            raise ValueError(f"{label} ID 重复。")
        seen.add(value)

    if not isinstance(definition, dict):
        raise ValueError("语义模型必须为对象。")
    tables = records(definition.get("tables", []), "tables")
    metrics = records(definition.get("metrics", []), "metrics")
    if not tables or not metrics:
        raise ValueError("发布需要至少一张表和一个正式指标。")
    by_table = {}
    for table in tables:
        name, columns = table.get("name"), table.get("columns")
        if not text(name) or name in by_table:
            raise ValueError("表名缺失或重复。")
        if not isinstance(columns, dict) or not columns or any(not text(key) for key in columns) or not table.get("grain"):
            raise ValueError("每张表需要字段定义与事实粒度 grain。")
        by_table[name] = table

    def column_reference(table, column, label):
        if not text(table) or not text(column) or table not in by_table or column not in by_table[table]["columns"]:
            raise ValueError(f"{label} 不在批准的表和字段中。")

    metric_ids = set()
    for metric in metrics:
        unique_id(metric, metric_ids, "指标")
        if not all(metric.get(k) for k in ("id", "name", "tables", "grain")) or not (metric.get("formula") or metric.get("expression")) or not (metric.get("time_column") or metric.get("time_field")):
            raise ValueError("指标必须包含 id、name、formula/expression、tables、grain、time_column。")
        if not set(strings(metric["tables"], "指标 tables")).issubset(by_table):
            raise ValueError("指标引用了未定义表。")
        if not all(k in metric for k in ("filters", "unit", "additivity")):
            raise ValueError("请明确指标 filters、unit、additivity；比率需注明 denominator。")
        if metric["additivity"] == "ratio" and not metric.get("denominator"):
            raise ValueError("比率指标必须明确分母。")

    dimension_ids = set()
    for dimension in records(definition.get("dimensions", []), "dimensions"):
        unique_id(dimension, dimension_ids, "维度")
        column_reference(dimension.get("table"), dimension.get("column"), "维度映射")

    for relation in records(definition.get("relations", []), "relations"):
        if not text(relation.get("cardinality")) or relation["cardinality"] not in {"one_to_one", "one_to_many", "many_to_one", "many_to_many"}:
            raise ValueError("关联需要明确 cardinality。")
        for side in ("left", "right"):
            column_reference(relation.get(f"{side}_table"), relation.get(f"{side}_column"), "关联键")
        for key in records(relation.get("additional_keys", []), "additional_keys"):
            for side in ("left", "right"):
                column_reference(relation[f"{side}_table"], key.get(f"{side}_column"), "复合关联键")
        if relation.get("type", relation.get("kind")) == "temporal":
            column_reference(relation["right_table"], relation.get("end_column"), "时间有效期结束字段")
            for flag in ("end_inclusive", "allow_open_end"):
                if flag in relation and not isinstance(relation[flag], bool):
                    raise ValueError(f"时间有效期 {flag} 必须为布尔值。")
        if relation["cardinality"] == "many_to_many" and not relation.get("preaggregation"):
            raise ValueError("多对多关系必须声明预聚合规则。")

    # Capabilities are extensible Skill tags, not a registry of executable tools.
    strings(definition.get("capabilities", []), "capabilities")

    def query_definitions(queries, label, *, require_ids):
        queries = records(queries, label)
        if not queries:
            raise ValueError(f"{label} 至少需要一条输入查询。")
        ids = set()
        for query in queries:
            unique_id(query, ids, f"{label} 查询", required=require_ids)
            if not text(query.get("sql")):
                raise ValueError(f"{label} 查询需要 SQL 文本。")
            referenced_metrics = strings(query.get("metric_ids", []), f"{label} metric_ids")
            if not set(referenced_metrics).issubset(metric_ids):
                raise ValueError(f"{label} 查询引用了未定义指标。")
        return ids

    def calculation_recipe(recipe, query_ids, label):
        if not isinstance(recipe, dict) or not text(recipe.get("tool")) or recipe["tool"] not in TOOLS:
            raise ValueError(f"{label} tool 未注册。")
        config = recipe.get("config", {})
        if not isinstance(config, dict):
            raise ValueError(f"{label} config 必须为对象。")
        # Config is tool-specific. Validate only the shared query selector;
        # complete_snapshot, period selections and future options remain intact.
        for holder in (recipe, config):
            if holder.get("query_id") is not None:
                query_id = holder["query_id"]
                if not text(query_id) or query_id not in query_ids:
                    raise ValueError(f"{label} 引用了不存在的查询 query_id。")
            if "query_map" in holder:
                mapping = holder["query_map"]
                if not isinstance(mapping, dict) or any(not text(k) or not text(v) or v not in query_ids for k, v in mapping.items()):
                    raise ValueError(f"{label} query_map 引用了不存在的查询。")

    diagnostic_ids = set()
    for diagnostic in records(definition.get("diagnostics", []), "diagnostics"):
        unique_id(diagnostic, diagnostic_ids, "诊断")
        ids = query_definitions(diagnostic.get("queries", []), "诊断", require_ids=True)
        calculation_recipe(diagnostic, ids, "诊断")

    template_ids = set()
    for template in records(definition.get("dashboard_templates", []), "dashboard_templates"):
        unique_id(template, template_ids, "看板模板")
        card_ids = set()
        for card in records(template.get("cards", []), "看板模板 cards"):
            unique_id(card, card_ids, "卡片", required=False)
            # A single ordinary card may omit its query ID: refresh supplies one.
            # A pinned calculation may also repeat its first query in `query`;
            # uniqueness applies to the actually executed `queries` list only.
            primary_ids = query_definitions([card.get("query")], "卡片", require_ids=False)
            if "queries" in card:
                records(card["queries"], "卡片 queries")
            ids = query_definitions(card["queries"], "卡片", require_ids=False) if card.get("queries") else primary_ids
            if card.get("analysis_recipe") is not None:
                calculation_recipe(card["analysis_recipe"], ids, "卡片计算定义")
