"""Opt-in, bounded live repair reproduction; application persistence is read-only.

Run explicitly with --live --model-env PATH --local-postgres. This is a reproduction
of two published builtin fixtures, NOT an independent/generalization evaluation.
It never launches Runtime, seeds memory, changes preferences, or saves experiences.
"""

import argparse
import json
import math
import time
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from dev import ROOT, common_arguments, configure, runner

CASES = (
    {
        "seed_id": "ec-grouping",
        "kind": "execution_error",
        "metric_id": "sales",
        "question": "按渠道汇总全部已完成订单的销售额，不限制日期；输出渠道和销售额两列。",
    },
    {
        "seed_id": "ec-refund-status",
        "kind": "semantic_error",
        "metric_id": "refund_amount",
        "question": "统计全部成功退款金额，不限制日期；输出成功退款金额一列。",
        "review_feedback": "查询可以执行，但把处理中退款也计入成功退款金额；请按正式退款指标卡复核状态口径。",
    },
)

REPAIR_SYSTEM = """你是 DuckDB SQL 修复节点。本次只做一轮最小修改，返回 SQLPlan JSON，
queries 必须恰好一条，id 为 repaired，包含 sql 与简短 rationale。
根据原问题、失败 SQL、执行错误或业务复核反馈、正式指标定义及当前可用字段完成修复。
召回经验只是参考，不得覆盖正式指标定义、原问题或安全约束。
只使用提供的表和可用字段，禁止写入、外部访问、附加查询、推测数据值或硬编码结果。
保留原查询输出的业务含义和列顺序，聚合列使用唯一别名。
本题不限制日期；不要因为正式元数据列出时间字段，就编造日期筛选。
返回 JSON，不生成解释性 Markdown。"""


class ReadOnlyCanonicalStore:
    """Expose only canonical lookup without the installed Store GET's UPDATE CTE.

    The installed AsyncPostgresStore builds a TTL UPDATE CTE even for
    aget(refresh_ttl=False). PostgreSQL rejects that statement in a read-only
    transaction even if no row would be updated. Its public asearch API, however,
    generates a pure SELECT when refresh_ttl=False and query=None.
    Application records carry their stable key in value.id; filter by that id and
    verify both returned namespace and key rather than trusting prefix matching.
    This adapter is only used by this diagnostic, not the production Store.
    """

    def __init__(self, store):
        self.store = store

    async def aget(self, namespace, key):
        offset, page_size = 0, 20
        while True:
            items = await self.store.asearch(
                namespace, filter={"id": key}, query=None,
                limit=page_size, offset=offset, refresh_ttl=False,
            )
            for item in items:
                if tuple(item.namespace) == tuple(namespace) and item.key == key:
                    return item
            if len(items) < page_size:
                return None
            offset += len(items)


def equivalent_rows(actual, expected):
    """Ignore row order, preserve duplicates, tolerate numeric rounding only."""
    def same_value(left, right):
        if isinstance(left, (int, float)) and not isinstance(left, bool) and isinstance(right, (int, float)) and not isinstance(right, bool):
            return math.isclose(left, right, rel_tol=1e-9, abs_tol=1e-8)
        return left == right

    remaining = list(expected)
    for row in actual:
        match = next((index for index, wanted in enumerate(remaining)
                      if len(row) == len(wanted) and all(same_value(a, b) for a, b in zip(row, wanted))), None)
        if match is None:
            return False
        remaining.pop(match)
    return not remaining


def redacted(value, settings):
    """Do not serialize endpoint addresses, connection strings or API keys."""
    sensitive = [
        settings.llm_base_url, settings.embedding_base_url,
        settings.llm_api_key.get_secret_value(), settings.embedding_api_key.get_secret_value(),
        settings.postgres_uri.get_secret_value(), settings.postgres_password.get_secret_value(),
    ]
    if isinstance(value, str):
        for secret in sorted((item for item in sensitive if item), key=len, reverse=True):
            value = value.replace(secret, "[REDACTED]")
        return value
    if isinstance(value, list):
        return [redacted(item, settings) for item in value]
    if isinstance(value, dict):
        return {key: redacted(item, settings) for key, item in value.items()}
    return value


def safe_failure(exc):
    # Raw network/database exceptions can contain hostnames or connection strings.
    from insight.providers import ModelError
    from insight.sql import SQLRejected
    if isinstance(exc, (ModelError, SQLRejected)):
        return {"type": type(exc).__name__, "message": str(exc)[:600]}
    return {"type": type(exc).__name__, "message": "该步骤未成功；不输出可能包含连接信息的底层异常。"}


def create_fixture(path, seed, scenario):
    import duckdb

    available = []
    with duckdb.connect(str(path), config={"enable_external_access": False, "threads": 2, "memory_limit": "256MB"}) as connection:
        for statement in seed["fixture_sql"]:
            connection.execute(statement)  # Trusted repository fixtures only; never model output.
        for table in scenario["tables"]:
            if table["name"] not in seed["tables"]:
                continue
            # Table identifiers come from the trusted scenario manifest, not CLI or model.
            quoted = '"' + table["name"].replace('"', '""') + '"'
            fields = connection.execute(f"DESCRIBE SELECT * FROM {quoted}").fetchall()
            available.append({**table, "columns": {row[0]: row[1] for row in fields}})
    return {
        **scenario,
        "tables": available,
        "relations": [relation for relation in scenario["relations"]
                      if relation["left_table"] in seed["tables"] and relation["right_table"] in seed["tables"]],
    }


async def check_case(case, scenario, memory, model, runtime_dir):
    from insight.models import SQLPlan
    from insight.sql import error_category, execute_query, validate_sql

    seed = next(item for item in scenario["repair_experiences"] if item["id"] == case["seed_id"])
    budget = {}
    started = time.perf_counter()
    result = {
        "case_id": case["seed_id"], "failure_kind": case["kind"], "scenario_id": scenario["id"],
        "schema_version": str(scenario["schema_version"]), "error_category": seed["error_category"],
        "stage": "fixture", "correct": False, "passed": False, "fixture_reproduced": False,
        "recall_ids": [], "recall_mode": None, "intended_seed_recalled": False,
        "repair_rounds_requested": 0, "usage": budget,
    }
    try:
        with TemporaryDirectory(prefix="live-repair-fixture-", dir=runtime_dir) as folder:
            path = Path(folder) / "fixture.duckdb"
            fixture_schema = create_fixture(path, seed, scenario)
            if case["kind"] == "execution_error":
                try:
                    execute_query(path, seed["broken_sql"], fixture_schema, seed["tables"], timeout=5, limit=100)
                except Exception as exc:
                    feedback = str(exc)[:1200]  # Only a local synthetic query error is provided to the model.
                    result["observed_error_category"] = error_category(feedback)
                    result["fixture_reproduced"] = True
                else:
                    result["failure"] = {"type": "FixtureMismatch", "message": "预设执行错误未复现；不调用模型。"}
                    return result
            else:
                bad = execute_query(path, seed["broken_sql"], fixture_schema, seed["tables"], timeout=5, limit=100)
                result["fixture_reproduced"] = not equivalent_rows(bad["rows"], seed["expected"])
                if not result["fixture_reproduced"]:
                    result["failure"] = {"type": "FixtureMismatch", "message": "预设语义错误未复现；不调用模型。"}
                    return result
                feedback = case["review_feedback"]
                result["observed_error_category"] = "semantic"

            result["stage"] = "recall"
            search_started = time.perf_counter()
            recalled = await memory.search(
                scenario["id"], str(scenario["schema_version"]),
                f"{case['question']}\n{feedback}\n{seed['broken_sql']}",
                kind="sql_experience", error_category=seed["error_category"], tables=seed["tables"], limit=2,
            )
            result["recall_duration_ms"] = round((time.perf_counter() - search_started) * 1000)
            result["recall_mode"] = recalled["mode"]
            result["recall_ids"] = [record["id"] for record in recalled["items"]]
            expected_ids = {f"sql_experience:{seed['id']}", f"sql_experience:{seed['id']}:v:{scenario['schema_version']}"}
            result["intended_seed_recalled"] = bool(expected_ids.intersection(result["recall_ids"]))
            # Explicit projection: never send fixture INSERTs, fixed_sql, expected, or whole seeds.
            payload = {
                "question": case["question"], "dialect": "duckdb", "broken_sql": seed["broken_sql"],
                "error_category": seed["error_category"], "error_or_review_feedback": feedback,
                "formal_metric_cards": [metric for metric in scenario["metrics"] if metric["id"] == case["metric_id"]],
                "available_tables": fixture_schema["tables"], "approved_relations": fixture_schema["relations"],
                "retrieved_experiences": [
                    {"id": record["id"], "title": record["title"], "content": record["content"]}
                    for record in recalled["items"]
                ],
            }
            result["stage"] = "model_repair"
            result["repair_rounds_requested"] = 1
            # Exactly one repair invocation; ModelClient itself permits at most two HTTP attempts.
            plan = await model.complete(SQLPlan, REPAIR_SYSTEM, payload, budget)
            if len(plan.queries) != 1:
                result["failure"] = {"type": "InvalidRepairPlan", "message": "修复结果不是一条查询，不执行。"}
                return result
            result["stage"] = "validate_and_execute"
            repaired = validate_sql(plan.queries[0].sql, fixture_schema, seed["tables"])
            result["generated_sql"] = repaired
            fixed = execute_query(path, repaired, fixture_schema, seed["tables"], query_id="repaired", timeout=5, limit=100)
            result["correct"] = not fixed["truncated"] and equivalent_rows(fixed["rows"], seed["expected"])
            result["result_rows"] = fixed["rows"]
            result["sql_elapsed_ms"] = fixed["elapsed_ms"]
            result["passed"] = result["correct"] and result["intended_seed_recalled"]
            result["stage"] = "finished"
    except Exception as exc:
        result["failure"] = safe_failure(exc)
    finally:
        result["total_duration_ms"] = round((time.perf_counter() - started) * 1000)
    return result


async def run_checks(settings, runtime_dir, report):
    from langgraph.store.postgres.aio import AsyncPostgresStore
    from psycopg.conninfo import make_conninfo

    from insight.enterprise import ResourceStore
    from insight.memory import MemoryService
    from insight.providers import Embedder, ModelClient
    from insight.repository import Repository
    from insight.scenarios import load_scenarios

    if not settings.llm_configured or not settings.embedding_base_url or not settings.postgres_uri.get_secret_value():
        raise RuntimeError("Live repair checks require explicitly configured model, embedding and application database.")
    # Read-only is enforced at both retrieval and canonical Store connection level.
    dsn = make_conninfo(settings.postgres_uri.get_secret_value(), options="-c default_transaction_read_only=on")
    repository = Repository(dsn)
    scenario = load_scenarios(settings.scenario_root)["ecommerce"]
    resources = ResourceStore(repository)
    domain = resources.get("domain", "ecommerce")
    scenario = {**scenario, **resources.get("model", domain["model_id"])["definition"]}
    try:
        async with AsyncPostgresStore.from_conn_string(dsn) as store:
            # No setup(), seed(), Runtime.start(), recovery, preferences update, or experience write.
            memory = MemoryService(ReadOnlyCanonicalStore(store), repository, Embedder(settings))
            model = ModelClient(settings)
            for case in CASES:
                report["cases"].append(await check_case(case, scenario, memory, model, runtime_dir))
            report["embedding_failures"] = memory.embedding_failures
    finally:
        repository.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    common_arguments(parser)
    parser.add_argument("--live", action="store_true", required=True, help="Authorize at most two cases / four model HTTP attempts / two query embeddings")
    args = parser.parse_args()
    if args.model_env is None or not args.local_postgres:
        parser.error("必须显式提供 --model-env PATH 和 --local-postgres；仅使用本项目应用数据库。")
    if not args.model_env.is_file():
        parser.error("指定的模型配置文件不存在。")
    configure(args)
    from insight.config import Settings
    settings = Settings(_env_file=None, max_model_calls=2)
    runtime_dir = ROOT / ".runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(UTC)
    report = {
        "description": "内置用例复现，非独立泛化评测",
        "created_at": now.isoformat(), "llm_model": settings.llm_model, "embedding_model": settings.embedding_model,
        "application_database_read_only": True, "writes_runtime_experience": False,
        "canonical_read": "PostgresStore.asearch(value.id filter, refresh_ttl=False), exact namespace/key verification; avoids GET TTL UPDATE CTE",
        "limits": {"cases": 2, "repair_rounds_per_case": 1, "model_http_attempts_per_case": 2, "recall_top_k": 2},
        "comparison": "忽略结果行顺序，保留重复行，数值采用1e-9相对/1e-8绝对容差。",
        "interpretation": "passed要求目标内置经验被召回且修复结果正确；不能据此声称模型必须依赖经验才能修复。",
        "cases": [],
    }
    started = time.perf_counter()
    try:
        runner(run_checks(settings, runtime_dir, report))
    except Exception as exc:
        report["fatal_error"] = safe_failure(exc)
    report["total_duration_ms"] = round((time.perf_counter() - started) * 1000)
    report["passed"] = len(report["cases"]) == len(CASES) and all(case["passed"] for case in report["cases"])
    report = redacted(report, settings)
    name = f"live-repairs-{now.strftime('%Y%m%dT%H%M%S_%fZ')}-{uuid4().hex[:8]}.json"
    with (runtime_dir / name).open("x", encoding="utf-8") as output:
        json.dump(report, output, ensure_ascii=False, indent=2)
    print(json.dumps({"report": f".runtime/{name}", **report}, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
