"""Default history ranks; SQL branch uses a local SQL adapter, not live PostgreSQL."""
import json
from contextlib import contextmanager

import duckdb
import pytest

from insight.repository import InMemoryRepository, Repository


@pytest.fixture(params=["memory", "sql"])
def history(request):
    memory = InMemoryRepository()
    definitions = [
        ("user_failed", "user", "failed", False, "09"),
        ("formal", "question_catalog", "completed", False, "08"),
        ("user_running", "user", "running", False, "07"),
        ("user_complete", "user", "completed", False, "06"),
        ("catalog_queued", "question_catalog", "queued", False, "12"),
        ("catalog_failed", "question_catalog", "failed", False, "11"),
        ("catalog_partial", "question_catalog", "completed", True, "10"),
        ("catalog_cancelled", "question_catalog", "cancelled", False, "09"),
        ("legacy_complete", "legacy", "completed", False, "15"),
        ("legacy_failed", "legacy", "failed", False, "14"),
    ]
    for name, origin, status, partial, day in definitions:
        run = memory.create_run("ecommerce", name, origin=origin, question_id="q01" if origin == "question_catalog" else None)
        memory.update_run(run["run_id"], status=status, artifacts={"partial": partial})
        memory._runs[run["run_id"]]["created_at"] = f"2026-09-{day}T00:00:00Z"
    for index in range(45):
        run = memory.create_run("saas", f"debug_{index}", origin="question_catalog", question_id="q06")
        memory.update_run(run["run_id"], status="failed", artifacts={"partial": False})
        memory._runs[run["run_id"]]["created_at"] = "2026-09-25T00:00:00Z"
    if request.param == "memory":
        yield memory
        return
    # Exercise the actual Repository SELECT/ORDER, translating only bind syntax.
    database = duckdb.connect(":memory:")
    database.execute("CREATE TABLE ia_runs(run_id TEXT,thread_id TEXT,scenario_id TEXT,question TEXT,status TEXT,error TEXT,created_at TEXT,updated_at TEXT,origin TEXT,question_id TEXT,data_version TEXT,model_version TEXT,artifacts JSON)")
    columns = [row[0] for row in database.execute("DESCRIBE ia_runs").fetchall()]
    for run in memory._runs.values():
        database.execute("INSERT INTO ia_runs VALUES(" + ",".join("?" for _ in columns) + ")",
                         [json.dumps(run[column]) if column == "artifacts" else run.get(column) for column in columns])

    class Connection:
        def execute(self, statement, values=()):
            if statement.startswith("SET TRANSACTION"):
                return self
            cursor = database.execute(statement.replace("%s", "?"), values)
            self.rows = [dict(zip([column[0] for column in cursor.description], row)) for row in cursor.fetchall()]
            return self

        def fetchone(self):
            return self.rows[0]

        def fetchall(self):
            return self.rows

    class SQLRepository(Repository):
        @contextmanager
        def connect(self):
            yield Connection()

    yield SQLRepository("unused-test-only")
    database.close()


def test_current_user_failure_and_formal_results_stay_ahead_of_many_debug_attempts(history):
    result = history.list_runs(limit=4)
    assert result["total"] == 55
    assert [row["question"] for row in result["items"]] == ["user_failed", "formal", "user_running", "user_complete"]


def test_domain_filter_preserves_all_three_groups_and_partial_is_not_promoted(history):
    result = history.list_runs("ecommerce", limit=200)
    assert [row["question"] for row in result["items"]] == [
        "user_failed", "formal", "user_running", "user_complete", "catalog_queued", "catalog_failed",
        "catalog_partial", "catalog_cancelled", "legacy_complete", "legacy_failed"]
    assert result["total"] == 10


def test_explicit_status_and_text_filters_still_include_failed_and_partial_catalog_records(history):
    failed = history.list_runs(status="failed", limit=200)
    assert failed["total"] == 48
    assert failed["items"][0]["question"] == "user_failed"
    assert failed["items"][-1]["question"] == "legacy_failed"
    completed = history.list_runs(status="completed", limit=200)
    assert {row["question"] for row in completed["items"]} == {"formal", "user_complete", "catalog_partial", "legacy_complete"}
    partial = history.list_runs(q="catalog_partial", status="completed")
    assert partial["total"] == 1 and partial["items"][0]["partial"] is True


def test_paging_does_not_hide_or_duplicate_lower_priority_history(history):
    all_rows = history.list_runs(limit=200)["items"]
    paged = [row for offset in range(0, 55, 7) for row in history.list_runs(limit=7, offset=offset)["items"]]
    assert [row["run_id"] for row in paged] == [row["run_id"] for row in all_rows]
    assert len(paged) == len({row["run_id"] for row in paged}) == 55
    assert all(row["origin"] == "legacy" for row in paged[-2:])
