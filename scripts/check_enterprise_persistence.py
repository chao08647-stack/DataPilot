"""Opt-in real PostgreSQL catalog/dashboard persistence probe; no model calls.

All application tables are created in a fresh schema in insight_agents_checks.
Only that schema is removed. The synthetic DuckDB fixture stays under .runtime.
"""
import argparse
import gc
import json
import sys
import time
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

import duckdb
import psycopg
from psycopg import sql as psql
from psycopg.conninfo import make_conninfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from insight.config import Settings  # noqa: E402 -- explicit standalone project import
from insight.dashboards import Dashboards  # noqa: E402
from insight.enterprise import Enterprise  # noqa: E402
from insight.repository import SCHEMA, Repository  # noqa: E402


def probe():
    credentials = json.loads((ROOT / ".runtime" / "local-postgres.json").read_text(encoding="utf-8"))
    admin = {"host": "127.0.0.1", "port": 15432, "user": "insight", "password": credentials["password"], "connect_timeout": 5}
    with psycopg.connect(**admin, dbname="postgres", autocommit=True) as conn:
        if Path(conn.execute("SHOW data_directory").fetchone()[0]).resolve() != (ROOT / ".runtime" / "postgres").resolve():
            raise RuntimeError("Refusing a cluster outside the independent project runtime")
        if not conn.execute("SELECT 1 FROM pg_database WHERE datname=%s", ("insight_agents_checks",)).fetchone():
            conn.execute("CREATE DATABASE insight_agents_checks")
    suffix = uuid4().hex[:12]
    schema = f"enterprise_probe_{suffix}"
    run_root = ROOT / ".runtime" / f"enterprise-persistence-{suffix}"
    run_root.mkdir()
    fixture = run_root / "synthetic.duckdb"
    with duckdb.connect(str(fixture)) as conn:
        conn.execute("CREATE TABLE orders(day DATE,region VARCHAR,amount DOUBLE)")
        conn.execute("INSERT INTO orders VALUES('2025-01-01','east',10),('2025-02-01','west',20),('2025-02-02','east',5)")
    with psycopg.connect(**admin, dbname="insight_agents_checks", autocommit=True) as conn:
        conn.execute(psql.SQL("CREATE SCHEMA {}").format(psql.Identifier(schema)))
    dsn = make_conninfo(**admin, dbname="insight_agents_checks", options=f"-c search_path={schema},pg_catalog")
    settings = Settings(_env_file=None, data_dir=run_root, postgres_uri=dsn)
    started = time.perf_counter()
    report = {}
    try:
        repo = Repository(dsn)
        with repo.connect() as conn:
            assert conn.execute("SELECT current_schema() AS value").fetchone()["value"] == schema
            # Use the actual application run/message/event DDL, without installing
            # an unrelated vector extension or retrieval indexes for this probe.
            conn.execute(SCHEMA[SCHEMA.index("CREATE TABLE IF NOT EXISTS ia_threads"):SCHEMA.index("CREATE TABLE IF NOT EXISTS ia_retrieval")])
        enterprise = Enterprise(repo, {}, {}, settings)
        enterprise.setup()
        assert enterprise.store.memory is None and not repo.is_memory
        source = enterprise.create_source({"id": "probe-source", "name": "隔离合成数据", "kind": "duckdb", "config": {"path": str(fixture)},
            "model_access": {"metadata": False, "results": False}})
        enterprise.create_domain({"id": "probe-domain", "title": "真实持久化验证域", "data_source_id": source["id"]})
        definition = {"tables": [{"name": "orders", "columns": {"day": "DATE", "region": "VARCHAR", "amount": "DOUBLE"}, "grain": "一行订单"}],
            "relations": [], "metrics": [{"id": "amount", "name": "金额", "formula": "SUM(amount)", "tables": ["orders"], "grain": "订单",
                "time_column": "orders.day", "unit": "元", "filters": [], "additivity": "additive"}],
            "dimensions": [{"id": "region", "table": "orders", "column": "region", "type": "string"}],
            "date_range": {"start": "2025-01-01", "end": "2025-02-02"}}
        published = enterprise.publish(enterprise.create_model("probe-domain", definition)["id"])
        version1 = published["version"]
        boards = Dashboards(enterprise)
        board = boards.create({"title": "版本一经营看板", "domain_id": "probe-domain"})
        card = {"title": "日期区域金额", "query": {
            "sql": "SELECT day,region,SUM(amount) AS amount FROM orders GROUP BY day,region ORDER BY day",
            "metric_ids": ["amount"], "filter_bindings": {"date_from": "day", "date_to": "day", "region": "region"}},
            "chart": {"type": "bar", "x": "day", "y": ["amount"]}}
        boards.pin(board["id"], card)
        successful = boards.refresh(board["id"], {"date_from": "2025-02-01", "region": "east"})
        snapshot = successful["cards"][0]["snapshot"]
        assert snapshot["status"] == "ready" and snapshot["query"]["rows"] == [["2025-02-02", "east", 5.0]]
        assert not snapshot["stale"] and snapshot["query"]["queried_at"]
        resource_kinds = ("source", "domain", "model", "dashboard")
        saved = {kind: enterprise.store.list(kind) for kind in resource_kinds}
        with repo.connect() as conn:
            assert conn.execute("SELECT count(*) AS n FROM ia_resources").fetchone()["n"] == 4
        report["real_postgresql_jsonb_store"] = True
        report["published_model_and_successful_snapshot_saved"] = True
        # Discard all service/repository/connector objects. New instances have no
        # shared in-memory ResourceStore or connector cache to satisfy these reads.
        del boards, enterprise, repo
        gc.collect()
        reopened = Enterprise(Repository(dsn), {}, {}, settings)
        reopened.setup()
        restored_boards = Dashboards(reopened)
        assert {kind: reopened.store.list(kind) for kind in resource_kinds} == saved
        assert restored_boards.get(board["id"]) == successful
        assert reopened.catalog("probe-domain", version1) == published["definition"]
        report["fresh_objects_restore_identical_resources"] = True
        # A real missing-file query failure must preserve the previous successful
        # evidence and mark it stale; it must survive another object recreation.
        unavailable = run_root / "synthetic-unavailable.duckdb"
        if not fixture.resolve().is_relative_to(run_root.resolve()) or not unavailable.resolve().is_relative_to(run_root.resolve()):
            raise RuntimeError("Unsafe synthetic fixture move")
        fixture.rename(unavailable)
        try:
            failed = restored_boards.refresh(board["id"], {"region": "west"})
        finally:
            unavailable.rename(fixture)
        failure = failed["cards"][0]["snapshot"]
        assert failure["status"] == "failed" and failure["stale"]
        assert failure["query"] == snapshot["query"] and failure["queried_at"] == snapshot["queried_at"]
        del restored_boards, reopened
        gc.collect()
        upgraded = Enterprise(Repository(dsn), {}, {}, settings)
        upgraded.setup()
        upgraded_boards = Dashboards(upgraded)
        assert upgraded_boards.get(board["id"]) == failed
        report["failed_refresh_retains_last_success_after_reopen"] = True
        new_definition = deepcopy(upgraded.catalog("probe-domain", version1))
        new_definition["metrics"][0]["formula"] = "SUM(amount)*2"
        version2 = upgraded.publish(upgraded.create_model("probe-domain", new_definition)["id"])["version"]
        assert version1 != version2
        assert upgraded.catalog("probe-domain", version1)["metrics"][0]["formula"] == "SUM(amount)"
        assert upgraded.catalog("probe-domain")["metrics"][0]["formula"] == "SUM(amount)*2"
        try:
            upgraded_boards.refresh(board["id"])
        except ValueError as exc:
            assert "版本" in str(exc)
        else:
            raise AssertionError("Old dashboard silently adopted the new model version")
        assert upgraded_boards.get(board["id"]) == failed
        report["old_version_immutable_and_refresh_rejected"] = True
        next_board = upgraded_boards.create({"title": "版本二经营看板", "domain_id": "probe-domain"})
        next_card = deepcopy(card)
        next_card["query"]["sql"] = "SELECT day,region,SUM(amount)*2 AS amount FROM orders GROUP BY day,region ORDER BY day"
        upgraded_boards.pin(next_board["id"], next_card)
        next_snapshot = upgraded_boards.refresh(next_board["id"], {"date_from": "2025-02-01", "region": "east"})
        assert next_snapshot["cards"][0]["snapshot"]["query"]["rows"] == [["2025-02-02", "east", 10.0]]
        del upgraded, upgraded_boards
        gc.collect()
        final = Enterprise(Repository(dsn), {}, {}, settings)
        final.setup()
        final_boards = Dashboards(final)
        assert final.domain("probe-domain")["model_version"] == version2
        assert len(final.store.list("model")) == 2
        assert final_boards.get(board["id"]) == failed
        assert final_boards.get(next_board["id"]) == next_snapshot
        assert final.store.get("source", source["id"])["model_access"] == {"metadata": False, "results": False}
        report["both_versions_and_boards_survive_reopen"] = True
        report["model_requests"] = 0
        report["status"] = "passed"
        report["elapsed_seconds"] = round(time.perf_counter() - started, 3)
    finally:
        # Unique identifier created by this invocation; never delete any existing
        # schema, checks database, application tables or original project data.
        with psycopg.connect(**admin, dbname="insight_agents_checks", autocommit=True) as conn:
            conn.execute(psql.SQL("DROP SCHEMA {} CASCADE").format(psql.Identifier(schema)))
            assert not conn.execute("SELECT 1 FROM pg_namespace WHERE nspname=%s", (schema,)).fetchone()
    report["isolated_schema_removed"] = True
    report["synthetic_fixture"] = str(fixture.relative_to(ROOT))
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", required=True)
    parser.parse_args()
    try:
        result = probe()
    except Exception as exc:  # noqa: BLE001 -- redact connection strings and credentials
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__}))
        raise SystemExit(1)
    print(json.dumps(result, ensure_ascii=False, indent=2))
