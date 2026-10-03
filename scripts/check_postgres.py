"""Explicit, isolated PostgreSQL integration checks; models are test doubles.

Run: python scripts/check_postgres.py --integration --local-postgres
Only the project's local cluster is accepted. Each invocation creates a new schema
inside insight_agents_checks; existing application data and schemas are untouched.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from hashlib import sha256
from uuid import uuid4

from dev import ROOT, configure, runner

CHECK_DATABASE = "insight_agents_checks"


class FixtureEmbedder:
    """Deterministic test vectors, NOT BGE-M3 and NOT a semantic-quality benchmark."""

    model = "integration-fixture-hash-1024-v1"

    def __init__(self):
        self.calls = 0

    async def embed(self, texts):
        self.calls += 1
        vectors = []
        for text in texts:
            vector = [0.0] * 1024
            # Hash every character: repeatable, nonzero and free of network requests.
            for char in text or "empty":
                index = int.from_bytes(sha256(char.encode("utf-8")).digest()[:2], "big") % 1024
                vector[index] += 1.0
            vectors.append(vector)
        return vectors


def isolated_database() -> tuple[str, str]:
    """Create only an explicitly named test DB and an invocation-private schema."""
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    previous_uri = os.environ.get("INSIGHT_POSTGRES_URI")
    try:
        configure(argparse.Namespace(model_env=None, local_postgres=True))
        options = conninfo_to_dict(os.environ["INSIGHT_POSTGRES_URI"])
    finally:
        # Running as a pytest test must not change later tests' application config.
        if previous_uri is None:
            os.environ.pop("INSIGHT_POSTGRES_URI", None)
        else:
            os.environ["INSIGHT_POSTGRES_URI"] = previous_uri
    if (options.get("host"), options.get("port"), options.get("user"), options.get("dbname")) != (
        "127.0.0.1", "15432", "insight", "insight_agents"
    ):
        raise RuntimeError("Refusing non-project PostgreSQL configuration")
    maintenance = make_conninfo(**{**options, "dbname": "postgres"})
    with psycopg.connect(maintenance, autocommit=True, connect_timeout=5) as conn:
        if conn.execute("SELECT 1 FROM pg_database WHERE datname=%s", (CHECK_DATABASE,)).fetchone() is None:
            conn.execute(sql.SQL("CREATE DATABASE {} OWNER insight").format(sql.Identifier(CHECK_DATABASE)))
    schema = "check_" + uuid4().hex
    check_options = {**options, "dbname": CHECK_DATABASE}
    with psycopg.connect(make_conninfo(**check_options), autocommit=True, connect_timeout=5) as conn:
        assert conn.execute("SELECT current_database()").fetchone()[0] == CHECK_DATABASE
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public")
        conn.execute(sql.SQL("CREATE SCHEMA {} AUTHORIZATION insight").format(sql.Identifier(schema)))
    # Both framework and application tables resolve to the invocation-private schema.
    dsn = make_conninfo(**{**check_options, "options": f"-c search_path={schema},public"})
    return dsn, schema


async def integration_checks(dsn: str, schema: str) -> dict:
    from insight.config import Settings
    from insight.runtime import Runtime

    sys.path.insert(0, str(ROOT / "backend" / "tests"))
    from test_workflow import FakeModel

    # Explicit overrides prevent accidentally calling configured real services.
    settings = Settings(
        _env_file=None,
        postgres_uri=dsn,
        data_dir=ROOT / "data",
        llm_base_url="https://offline-model.invalid",
        llm_api_key="",
        llm_model="integration-fake-model",
        embedding_base_url="",
        embedding_api_key="",
        max_run_seconds=120,
    )
    passed = []
    runtimes = []

    async def start(model):
        runtime = Runtime(settings)
        runtime.model = model
        runtimes.append(runtime)
        await runtime.start()
        assert runtime.ready, runtime.startup_error
        with runtime.repository.connect() as conn:
            location = conn.execute("SELECT current_database() AS db,current_schema() AS schema").fetchone()
        assert location == {"db": CHECK_DATABASE, "schema": schema}, "Test writes escaped their isolated schema"
        embedder = FixtureEmbedder()
        runtime.memory.embedder = embedder
        for scene in runtime.scenarios.values():
            await runtime.memory.seed(scene)
        return runtime, embedder

    try:
        first, _ = await start(FakeModel(clarify=True))
        repo = first.repository
        run = repo.create_run("ecommerce", "销售额如何？需要先确认统计年份。")
        await first.drive(run)
        assert repo.get_run(run["run_id"])["status"] == "waiting_for_input"
        config = {"configurable": {"thread_id": run["thread_id"]}}
        snapshot = await first.graph.aget_state(config)
        assert snapshot.next and snapshot.values["run_id"] == run["run_id"]
        checkpoint_id = snapshot.config["configurable"]["checkpoint_id"]
        with repo.connect() as conn:
            checkpoints = conn.execute("SELECT count(*) AS count FROM checkpoints WHERE thread_id=%s", (run["thread_id"],)).fetchone()["count"]
        assert checkpoints > 0 and checkpoint_id
        cursor = repo.events(run["run_id"])[-1]["event_id"]
        try:
            repo.create_run("ecommerce", "不应同时启动", run["thread_id"])
        except ValueError:
            pass
        else:
            raise AssertionError("Waiting thread accepted a competing run")
        passed.append("real_checkpoint_written_and_waiting_thread_excluded")
        await first.stop()

        # Fresh Runtime, graph, AsyncPostgresSaver and AsyncPostgresStore connections.
        second, restarted_embedder = await start(FakeModel(clarify=True, execution_error=True))
        assert restarted_embedder.calls == 0, "Unchanged seed vectors were unexpectedly regenerated"
        assert second.graph is not first.graph
        restored = await second.graph.aget_state(config)
        assert restored.config["configurable"]["checkpoint_id"] == checkpoint_id
        assert second.repository.get_run(run["run_id"])["status"] == "waiting_for_input"
        passed.append("fresh_connections_reload_same_checkpoint_and_cached_vectors")

        resumed = second.repository.resume_run(run["run_id"])
        answer = "按2025年已完成订单统计，本次只要KPI，不按渠道分组。"
        message_key = f"resume:{run['run_id']}:{restored.values.get('clarification_rounds', 0)}"
        once = second.repository.add_message(run["thread_id"], "user", answer, run["run_id"], key=message_key)
        twice = second.repository.add_message(run["thread_id"], "user", answer, run["run_id"], key=message_key)
        assert once["message_id"] == twice["message_id"]
        try:
            second.repository.resume_run(run["run_id"])
        except ValueError:
            pass
        else:
            raise AssertionError("Duplicate resume was not rejected")
        await second.drive(resumed, answer)
        result = second.repository.get_run(run["run_id"])
        assert result["status"] == "completed", result["error"]
        assert result["thread_id"] == run["thread_id"]
        assert result["artifacts"]["repair_rounds"] == 1
        assert answer in result["question"]
        assert len([m for m in second.repository.messages(run["thread_id"]) if m["message_key"] == message_key]) == 1
        events = second.repository.events(run["run_id"])
        assert sum(e["type"] == "run.input_required" for e in events) == 1
        tail = second.repository.events(run["run_id"], cursor)
        assert all(e["event_id"] > cursor for e in tail)
        assert len({e["event_id"] for e in tail}) == len(tail)
        assert any(e["type"] == "run.completed" for e in tail)
        duplicate = second.repository.event(run["run_id"], "test.idempotent", {"value": 1}, key="test:cursor")
        duplicate_again = second.repository.event(run["run_id"], "test.idempotent", {"value": 999}, key="test:cursor")
        assert duplicate["event_id"] == duplicate_again["event_id"]
        assert duplicate_again["payload"] == {"value": 1}
        passed.append("hitl_resume_completed_with_idempotent_messages_and_event_cursor")

        memory_id = f"sql_experience:run:{run['run_id']}"
        canonical = await second.memory.store.aget(("insight_agents", "ecommerce", "sql_experience"), memory_id)
        assert canonical and canonical.value["metadata"]["verified"]
        assert canonical.value["origin"] == "runtime"
        with second.repository.connect() as conn:
            projection = conn.execute("SELECT document_id,vector_dims(embedding) AS dimensions,embedding_model FROM ia_retrieval WHERE scenario_id=%s AND document_id=%s", ("ecommerce", memory_id)).fetchone()
        assert projection and projection["dimensions"] == 1024
        assert projection["embedding_model"] == FixtureEmbedder.model
        model_version = second.enterprise.domain("ecommerce")["model_version"]
        recalled = await second.memory.search("ecommerce", model_version, "missing_column 已完成订单 修复字段", kind="sql_experience", error_category="schema", tables=["orders"], limit=2)
        assert recalled["mode"] == "hybrid"
        assert memory_id in {item["id"] for item in recalled["items"]}
        passed.append("verified_runtime_experience_store_projection_pgvector_and_hybrid_recall")

        stale = await second.memory.search("ecommerce", "obsolete-schema", "missing_column", kind="sql_experience", tables=["orders"])
        other = await second.memory.search("saas", "1", "missing_column", kind="sql_experience")
        assert stale["items"] == []
        assert memory_id not in {item["id"] for item in other["items"]}
        passed.append("schema_version_and_scenario_isolation")

        await second.memory.update("ecommerce", memory_id, active=False)
        disabled = await second.memory.search("ecommerce", model_version, "missing_column", kind="sql_experience", error_category="schema", tables=["orders"])
        assert memory_id not in {item["id"] for item in disabled["items"]}
        await second.memory.delete("ecommerce", memory_id)
        retry = await second.memory.remember_repair("ecommerce", model_version, run["run_id"], "must not resurrect", "schema", ["orders"])
        assert retry["metadata"]["deleted"] and not retry["active"]
        with second.repository.connect() as conn:
            assert conn.execute("SELECT count(*) AS count FROM ia_retrieval WHERE scenario_id=%s AND document_id=%s", ("ecommerce", memory_id)).fetchone()["count"] == 0
        builtin_id = "sql_experience:ec-refund-status"
        await second.memory.delete("ecommerce", builtin_id)
        await second.memory.seed(second.scenarios["ecommerce"])
        assert builtin_id not in {m["id"] for m in await second.memory.list("ecommerce")}
        passed.append("disable_delete_and_builtin_tombstone_prevent_recall_or_resurrection")

        assert await second.memory.preferences("ecommerce") == {}
        preference_id = "preference:channel-view"
        await second.memory.update("ecommerce", preference_id, active=True)
        assert await second.memory.preferences("ecommerce") == {"default_dimension": "channel"}
        assert await second.memory.preferences("saas") == {}
        await second.stop()

        final, _ = await start(FakeModel())
        assert await final.memory.preferences("ecommerce") == {"default_dimension": "channel"}
        assert builtin_id not in {m["id"] for m in await final.memory.list("ecommerce")}
        explicit_question = "本次只要2025年销售额KPI，不分渠道，也不需要渠道图。"
        preference_run = final.repository.create_run("ecommerce", explicit_question)
        await final.drive(preference_run)
        preference_result = final.repository.get_run(preference_run["run_id"])
        assert preference_result["status"] == "completed", preference_result["error"]
        intent_payload = next(payload for name, payload in final.model.calls if name == "Intent")
        assert intent_payload["question"] == explicit_question
        assert intent_payload["preferences"] == {"default_dimension": "channel"}
        assert preference_result["artifacts"]["queries"][0]["columns"] == ["sales"]
        assert preference_result["artifacts"]["charts"][0]["type"] == "kpi"
        passed.append("persistent_preference_enabled_current_request_preserved_and_scenario_scoped")

        return {
            "status": "passed",
            "database": CHECK_DATABASE,
            "schema": schema,
            "schema_retained": True,
            "checks": passed,
            "check_count": len(passed),
            "postgres_components": ["Repository", "AsyncPostgresSaver", "AsyncPostgresStore", "GIN full text", "pgvector", "RRF"],
            "model_mode": "FakeModel plus deterministic FixtureEmbedder; no LLM/BGE requests",
            "limitations": ["Not a real-model quality or concurrency benchmark", "Does not simulate OS power loss", "SSE cursor persistence checked at repository layer; HTTP SSE has separate offline tests"],
        }
    finally:
        for runtime in reversed(runtimes):
            await runtime.stop()


def execute_checks() -> dict:
    started = time.perf_counter()
    dsn, schema = isolated_database()
    report = runner(integration_checks(dsn, schema))
    report["elapsed_seconds"] = round(time.perf_counter() - started, 3)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--integration", action="store_true", required=True, help="Authorize isolated test database creation and real PostgreSQL integration checks")
    parser.add_argument("--local-postgres", action="store_true", required=True, help="Read only this project's local PostgreSQL credentials")
    parser.parse_args()
    try:
        print(json.dumps(execute_checks(), ensure_ascii=False, indent=2))
    except Exception as exc:
        # Never print connection strings, server addresses, passwords or raw tracebacks.
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__, "check": str(exc) if isinstance(exc, AssertionError) else "PostgreSQL integration check failed; credentials redacted"}, ensure_ascii=False))
        raise SystemExit(1) from None
