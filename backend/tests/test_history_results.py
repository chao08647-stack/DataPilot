"""Saved bounded evidence, atomic catalog reservations and offline history HTTP."""
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import duckdb
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from insight.connectors import ConnectorError, DuckDBConnector, connector_for
from insight.history_api import router
from insight.query_results import MAX_RESULT_BYTES, bounded_rows, json_size
from insight.question_catalog import catalog_run_key, get_question, get_questions
from insight.repository import InMemoryRepository


def query(count=120, query_id="q1"):
    return {"id": query_id, "sql": "SELECT amount FROM sales", "columns": ["amount"],
            "rows": [[i] for i in range(count)], "truncated": False, "metric_ids": ["sales"]}


def test_saved_preview_full_hydration_and_sort_are_separate():
    repo = InMemoryRepository()
    run = repo.create_run("ecommerce", "经营数据")
    preview = repo.save_query_result(run["run_id"], query())
    assert len(preview["rows"]) == 50 and preview["total_rows"] == 120
    assert preview["preview_truncated"] and not preview["truncated"]
    assert len(repo.hydrate_queries(run["run_id"], [preview])[0]["rows"]) == 120
    # Saving the preview during an unrelated state update cannot erase 70 rows.
    repo.save_query_result(run["run_id"], preview)
    assert len(repo.get_query_result(run["run_id"], "q1")["rows"]) == 120
    page = repo.query_rows(run["run_id"], "q1", offset=3, limit=2, sort_by="amount", descending=True)
    assert page["rows"] == [[116], [115]] and page["total"] == 120
    assert repo.query_rows(run["run_id"], "q1", limit=20000)["rows"] == query()["rows"]
    assert repo.get_run(run["run_id"])["artifacts"] == {}  # no checkpoint copy
    assert repo.events(run["run_id"]) == []  # no SSE payload copy


def test_results_are_scoped_and_pagination_cannot_be_sql():
    repo = InMemoryRepository()
    first = repo.create_run("ecommerce", "first")
    second = repo.create_run("saas", "second")
    preview = repo.save_query_result(first["run_id"], query())
    with pytest.raises(KeyError):
        repo.query_rows(second["run_id"], "q1")
    with pytest.raises(ValueError, match="reference"):
        repo.hydrate_queries(second["run_id"], [preview])
    for options in [{"sort_by": "amount; DROP TABLE sales"}, {"limit": 20001}, {"offset": -1}]:
        with pytest.raises(ValueError):
            repo.query_rows(first["run_id"], "q1", **options)


def test_legacy_inline_rows_are_honest_readonly_fallback():
    repo = InMemoryRepository()
    run = repo.create_run("ecommerce", "historical")
    repo.update_run(run["run_id"], artifacts={"queries": [{**query(5), "truncated": True}]})
    page = repo.query_rows(run["run_id"], "q1")
    assert page["legacy"] and not page["full_result_available"] and page["truncated"]
    with pytest.raises(KeyError):
        repo.query_rows(run["run_id"], "q2")


@pytest.mark.parametrize("count,truncated", [(20000, False), (20001, True)])
def test_storage_hard_row_cap(count, truncated):
    repo = InMemoryRepository()
    run = repo.create_run("retail", "detail")
    preview = repo.save_query_result(run["run_id"], query(count))
    full = repo.get_query_result(run["run_id"], "q1")
    assert preview["total_rows"] == len(full["rows"]) == 20000
    assert full["truncated"] is truncated


def test_utf8_result_bytes_cap_and_oversized_single_cell():
    rows = [["中" * 200000] for _ in range(20)]
    table = bounded_rows(["text"], rows)
    assert table["truncated"] and table["truncation_reason"] == "byte_limit"
    assert table["result_bytes"] == json_size({"columns": ["text"], "rows": table["rows"]}) <= MAX_RESULT_BYTES
    assert bounded_rows(["text"], [["x" * (MAX_RESULT_BYTES + 1)]])["rows"] == []


def test_atomic_catalog_reservations_preserve_failures_and_versions():
    repo = InMemoryRepository()
    key = catalog_run_key("q01", "ecommerce", "data1", "model1")
    options = {"origin": "question_catalog", "question_id": "q01", "data_version": "data1", "model_version": "model1", "idempotency_key": key}
    with ThreadPoolExecutor(max_workers=8) as executor:
        runs = list(executor.map(lambda _: repo.create_run("ecommerce", "question", **options), range(20)))
    assert len({r["run_id"] for r in runs}) == 1
    first = runs[0]
    repo.update_run(first["run_id"], status="failed")
    assert repo.create_run("ecommerce", "question", **options)["reused"]
    retry = repo.create_run("ecommerce", "question", **options, retry_failed=True)
    assert retry["run_id"] != first["run_id"]
    assert repo.get_run(first["run_id"])["status"] == "failed"
    repo.update_run(retry["run_id"], status="completed", artifacts={"partial": False})
    assert repo.create_run("ecommerce", "question", **options, retry_failed=True)["reused"]
    with pytest.raises(ValueError, match="another domain"):
        repo.create_run("saas", "other", **options)
    assert catalog_run_key("q01", "ecommerce", "data2", "model1") != key


def test_partial_catalog_completion_requires_explicit_retry():
    repo = InMemoryRepository()
    options = {"origin": "question_catalog", "question_id": "q01", "idempotency_key": "partial"}
    run = repo.create_run("ecommerce", "question", **options)
    repo.update_run(run["run_id"], status="completed", artifacts={"partial": True})
    assert repo.create_run("ecommerce", "question", **options)["run_id"] == run["run_id"]
    assert repo.create_run("ecommerce", "question", **options, retry_failed=True)["run_id"] != run["run_id"]


def test_history_is_per_run_filtered_and_versions_preserved():
    repo = InMemoryRepository()
    first = repo.create_run("ecommerce", "Sales 2025", model_version="m1", data_version="d1")
    repo.update_run(first["run_id"], status="completed", artifacts={"queries": [query()]})
    second = repo.create_run("ecommerce", "Sales followup", first["thread_id"], model_version="m1")
    repo.create_run("saas", "Sales legacy", origin="legacy")
    all_runs = repo.list_runs()
    assert all_runs["total"] == 3 and all_runs["items"][-1]["origin"] == "legacy"
    page = repo.list_runs("ecommerce", q="sales", limit=1, offset=1)
    assert page["total"] == 2 and len(page["items"]) == 1
    assert {r["run_id"] for r in repo.list_runs("ecommerce")["items"]} == {first["run_id"], second["run_id"]}
    item = repo.list_runs("ecommerce", status="completed")["items"][0]
    assert item["model_version"] == "m1" and item["data_version"] == "d1" and "artifacts" not in item


def test_history_api_never_needs_model_or_connector():
    repo = InMemoryRepository()
    run = repo.create_run("ecommerce", "example")
    repo.save_query_result(run["run_id"], query())
    app = FastAPI()
    app.include_router(router(SimpleNamespace(ready=True, repository=repo, startup_error=None)))
    with TestClient(app) as client:
        questions = client.get("/api/v1/questions").json()
        assert len(questions) == 10 and all(set(q) == {"id", "title", "question", "domain_id"} for q in questions)
        assert len(client.get("/api/v1/questions?domain_id=ecommerce").json()) == 5
        assert client.get("/api/v1/runs").json()["total"] == 1
        path = f"/api/v1/runs/{run['run_id']}/queries/q1/rows"
        assert client.get(path + "?offset=100&limit=20").json()["rows"] == [[i] for i in range(100, 120)]
        assert client.get(path + "?limit=20001").status_code == 422
        assert client.get(path.replace("q1/rows", "missing/rows")).status_code == 404
    external = get_questions()
    external[0]["question"] = "mutated"
    assert get_question("q01")["question"] != "mutated"


def test_duckdb_complete_cap_and_byte_limit(tmp_path):
    path = tmp_path / "bounds.duckdb"
    with duckdb.connect(str(path)) as connection:
        connection.execute("CREATE TABLE numbers AS SELECT range AS value FROM range(20001)")
        connection.execute("CREATE TABLE big_text(value TEXT)")
        connection.executemany("INSERT INTO big_text VALUES(?)", [("x" * 600000,)] * 20)
    connector = DuckDBConnector({"kind": "duckdb", "config": {"path": str(path)}})
    scene = {"tables": [{"name": "numbers", "columns": {"value": "INTEGER"}}, {"name": "big_text", "columns": {"value": "TEXT"}}], "relations": []}
    many = connector.execute("SELECT value FROM numbers ORDER BY value", scene, ["numbers"])
    assert many["total_rows"] == 20000 and many["truncated"] and many["rows"][-1] == [19999]
    exact = connector.execute("SELECT value FROM numbers WHERE value<20000", scene, ["numbers"])
    assert len(exact["rows"]) == 20000 and not exact["truncated"]
    large = connector.execute("SELECT value FROM big_text", scene, ["big_text"])
    assert large["truncated"] and large["truncation_reason"] == "byte_limit" and large["result_bytes"] <= MAX_RESULT_BYTES


def test_mysql_factory_is_dormant():
    with pytest.raises(ConnectorError, match="suspended"):
        connector_for({"kind": "mysql", "config": {}})
