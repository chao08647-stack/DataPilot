"""Opt-in isolated PostgreSQL saved-result probe. No business/model calls."""
import argparse
import gc
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import psycopg
from dev import configure
from psycopg import sql
from psycopg.conninfo import make_conninfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from insight.config import Settings  # noqa: E402 -- standalone project-local imports
from insight.query_results import MAX_RESULT_BYTES  # noqa: E402
from insight.repository import LIGHTBI_SCHEMA, SCHEMA, Repository  # noqa: E402


def probe():
    configure(SimpleNamespace(local_postgres=True, model_env=None))
    dsn = Settings(_env_file=None).postgres_uri.get_secret_value()
    schema = "lightbi_probe_" + uuid4().hex
    started = time.perf_counter()
    report = {"model_calls": 0, "business_queries": 0}
    with psycopg.connect(dsn, autocommit=True) as admin:
        if Path(admin.execute("SHOW data_directory").fetchone()[0]).resolve() != (ROOT / ".runtime" / "postgres").resolve():
            raise RuntimeError("Refusing a PostgreSQL cluster outside this project")
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        scoped_dsn = make_conninfo(dsn, options=f"-c search_path={schema},pg_catalog")
        try:
            repository = Repository(scoped_dsn)
            with repository.connect() as conn:
                assert conn.execute("SELECT current_schema() AS name").fetchone()["name"] == schema
                # Exact production run/event/result DDL, no unrelated vector extension.
                conn.execute(SCHEMA[SCHEMA.index("CREATE TABLE IF NOT EXISTS ia_threads"):SCHEMA.index("CREATE TABLE IF NOT EXISTS ia_retrieval")])
                # An existing legacy row proves the additive migration preserves history.
                conn.execute("INSERT INTO ia_threads(thread_id,scenario_id) VALUES('old-thread','test-domain')")
                conn.execute("INSERT INTO ia_runs(run_id,thread_id,scenario_id,question,status) VALUES('old-run','old-thread','test-domain','preserved history','completed')")
                conn.execute(LIGHTBI_SCHEMA)
                conn.execute(LIGHTBI_SCHEMA)  # idempotent DDL
            assert repository.get_run("old-run")["origin"] == "legacy"
            run = repository.create_run("test-domain", "full result", data_version="data-v1", model_version="model-v1")
            query = {"id": "q1", "columns": ["sequence", "amount"], "rows": [[i, i * 2] for i in range(20000)], "truncated": False, "sql": "synthetic fixture only"}
            preview = repository.save_query_result(run["run_id"], query)
            assert len(preview["rows"]) == 50 and preview["total_rows"] == 20000
            repository.update_run(run["run_id"], status="completed", artifacts={"queries": [preview], "partial": False})
            repository.event(run["run_id"], "artifact.created", {"artifact": "queries", "value": [preview]})
            run_id = run["run_id"]
            del repository
            gc.collect()
            reopened = Repository(scoped_dsn)
            restored = reopened.get_run(run_id)
            assert len(restored["artifacts"]["queries"][0]["rows"]) == 50
            assert len(reopened.events(run_id)[0]["payload"]["value"][0]["rows"]) == 50
            assert reopened.get_query_result(run_id, "q1")["rows"] == query["rows"]
            assert reopened.query_rows(run_id, "q1", offset=19998, limit=2)["rows"] == [[19998, 39996], [19999, 39998]]
            assert reopened.query_rows(run_id, "q1", limit=2, sort_by="amount", descending=True)["rows"] == [[19999, 39998], [19998, 39996]]
            assert len(reopened.hydrate_queries(run_id, restored["artifacts"]["queries"])[0]["rows"]) == 20000
            report["complete_result_survives_reinstantiation"] = True
            report["preview_only_artifacts_and_events"] = True
            report["saved_pagination_sort_and_hydration"] = True
            history = reopened.list_runs("test-domain", status="completed", q="full")
            assert history["total"] == 1 and history["items"][0]["data_version"] == "data-v1"
            assert history["items"][0]["model_version"] == "model-v1" and history["items"][0]["completed_at"]
            assert reopened.list_runs()["items"][-1]["run_id"] == "old-run"
            report["legacy_history_and_versioned_metadata"] = True
            bounded = reopened.save_query_result(run_id, {"id": "large", "columns": ["text"], "rows": [["x" * 600000] for _ in range(20)], "truncated": False})
            assert bounded["truncated"] and bounded["result_bytes"] <= MAX_RESULT_BYTES
            assert reopened.query_rows(run_id, "large")["truncation_reason"] == "byte_limit"
            report["real_jsonb_byte_bound"] = True
            options = {"origin": "question_catalog", "question_id": "q01", "data_version": "d1", "model_version": "m1", "idempotency_key": "isolated-catalog-probe"}
            with ThreadPoolExecutor(max_workers=4) as executor:
                attempts = list(executor.map(lambda _: reopened.create_run("ecommerce", "catalog", **options), range(8)))
            assert len({r["run_id"] for r in attempts}) == 1
            first = attempts[0]
            reopened.update_run(first["run_id"], status="failed")
            retry = reopened.create_run("ecommerce", "catalog", **options, retry_failed=True)
            assert retry["run_id"] != first["run_id"] and reopened.get_run(first["run_id"])["status"] == "failed"
            report["postgres_atomic_reservation_and_retry"] = True
        finally:
            # Only the random schema created above is a deletion target.
            assert schema.startswith("lightbi_probe_") and len(schema) == len("lightbi_probe_") + 32
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
            assert not admin.execute("SELECT 1 FROM pg_namespace WHERE nspname=%s", (schema,)).fetchone()
            report["temporary_schema_removed"] = True
    report["elapsed_seconds"] = round(time.perf_counter() - started, 3)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="Explicitly use project-local PostgreSQL only")
    arguments = parser.parse_args()
    if not arguments.live:
        parser.error("This real PostgreSQL probe requires --live")
    print(json.dumps(probe(), ensure_ascii=False, indent=2))
