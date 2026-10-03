"""Independent saved-history verifier boundaries; no service or model calls."""
import importlib.util
import json
from copy import deepcopy
from pathlib import Path

import pytest

from insight.analytics import run_analysis

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("verify_history", ROOT / "scripts/verify_history.py")
verifier = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verifier)


class Response:
    def __init__(self, body):
        self.body = body

    def raise_for_status(self):
        return self

    def json(self):
        return deepcopy(self.body)


class ReadOnlyClient:
    def __init__(self, pages):
        self.pages, self.requests = pages, []

    def get(self, path, *, params=None):
        self.requests.append((path, params))
        return Response(self.pages[path])


def saved_fixture(question, gold):
    run_id = "synthetic-test-run"
    specs = verifier.expected_outputs(question, gold)
    queries, output_tables, pages = [], [], {}
    for index, spec in enumerate(specs):
        identifier = f"arbitrary-output-{index}"
        columns = list(spec["rows"][0])
        values = [[row[column] for column in columns] for row in spec["rows"]]
        queries.append({"id": identifier, "derived": True, "recipe_output": spec["output"],
                        "columns": columns, "rows": values[:50], "total_rows": len(values),
                        "result_ref": {"run_id": run_id, "query_id": identifier}, "evidence_ids": ["raw-evidence"]})
        output_tables.append({"id": spec["output"], "query_id": identifier, "row_count": len(values)})
        pages[f"/api/v1/runs/{run_id}/queries/{identifier}/rows"] = {
            "columns": columns, "rows": values, "total": len(values), "truncated": False,
            "legacy": False, "full_result_available": True}
    run = {"run_id": run_id, "question_id": question, "origin": "question_catalog", "status": "completed",
           "domain_id": verifier.DOMAINS[question], "data_version": verifier.VERSION, "model_version": verifier.VERSION,
           "artifacts": {"partial": False, "queries": queries, "calculations": [{"tool": verifier.TOOLS[question],
                "status": "ok", "evidence_ids": ["raw-evidence"], "output_tables": output_tables}]}}
    return run, ReadOnlyClient(pages)


@pytest.fixture
def monthly():
    rows = [{"period": f"2025-{month:02}", "orders": month, "sales": month*10., "contribution_profit": month*2.}
            for month in range(1, 13)]
    gold = {"q01": {"series": rows, "annual_summary": {"orders": 78, "sales": 780., "contribution_profit": 156.}}}
    run, client = saved_fixture("q01", gold)
    return run, client, gold


def test_renamed_query_ids_resolve_via_output_role_and_only_get_is_used(monthly):
    run, client, gold = monthly
    result = verifier.verify_run(client, run, "q01", gold)
    assert result["numeric_verification"] == "passed"
    assert result["unchecked_outputs"] == []
    assert [item["output"] for item in result["outputs"]] == ["series", "annual_summary"]
    assert len(client.requests) == 2
    assert all(params == {"offset": 0, "limit": 20000} for _, params in client.requests)


def test_balanced_metadata_does_not_hide_wrong_saved_number(monthly):
    run, client, gold = monthly
    run["artifacts"]["calculations"][0]["reconciliation"] = {"balanced": True}
    first = next(iter(client.pages.values()))
    first["rows"][-1][first["columns"].index("sales")] += 1
    with pytest.raises(verifier.VerificationError, match="Numeric mismatch sales"):
        verifier.verify_run(client, run, "q01", gold)


def test_published_semantic_hash_matches_current_domain_not_data_version(monthly):
    run, client, gold = monthly
    run["model_version"] = "light-bi-3.1-published-hash"
    run["artifacts"]["model_version"] = run["model_version"]
    result = verifier.verify_run(client, run, "q01", gold, current_model_version=run["model_version"])
    assert result["numeric_verification"] == "passed"
    with pytest.raises(verifier.VerificationError, match="version"):
        verifier.verify_run(client, run, "q01", gold, current_model_version="light-bi-3.1-newer-hash")


def test_validation_reads_beyond_preview_row_fifty():
    from bi_domain_oracle import retention_summary_gold
    rows = [{"cohort": f"2025-{month:02}", "feature_group": group, "window": window,
             "eligible": 40, "retained": 20, "retention_pct": 50.}
            for month in range(1, 13) for group in ("early", "other") for window in ("D30", "D60", "D90")]
    gold = {"q08": {"series": rows,"retention_summary":retention_summary_gold(rows)}}
    run, client = saved_fixture("q08", gold)
    assert len(run["artifacts"]["queries"][0]["rows"]) == 50
    assert verifier.verify_run(client, run, "q08", gold)["outputs"][0]["rows_checked"] == 72
    first = next(iter(client.pages.values()))
    first["rows"][-1][first["columns"].index("retained")] = 21
    with pytest.raises(verifier.VerificationError, match="retained"):
        verifier.verify_run(client, run, "q08", gold)


@pytest.mark.parametrize('fault',['wrong-summary-number','missing-summary'])
def test_retention_summary_must_be_verified_not_ignored(fault):
    from bi_domain_oracle import retention_summary_gold
    rows=[{'cohort':'2025-01','feature_group':'early','window':w,'eligible':40,'retained':20,'retention_pct':50.}
          for w in ['D30','D60','D90']]
    gold={'q08':{'series':rows,'retention_summary':retention_summary_gold(rows)}}
    run,client=saved_fixture('q08',gold)
    assert verifier.verify_run(client,run,'q08',gold)['unchecked_outputs']==[]
    summary=next(q for q in run['artifacts']['queries'] if q['recipe_output']=='retention_summary')
    if fault=='wrong-summary-number':
        page=client.pages[f"/api/v1/runs/{run['run_id']}/queries/{summary['id']}/rows"]
        page['rows'][0][page['columns'].index('eligible')]=41
    else:
        run['artifacts']['queries'].remove(summary)
    with pytest.raises(verifier.VerificationError):
        verifier.verify_run(client,run,'q08',gold)


@pytest.mark.parametrize("fault", ["wrong-version", "partial", "wrong-ref", "truncated", "legacy", "wrong-role", "wrong-evidence"])
def test_rejects_untrustworthy_or_incomplete_evidence(monthly, fault):
    run, client, gold = monthly
    query = run["artifacts"]["queries"][0]
    page = next(iter(client.pages.values()))
    if fault == "wrong-version":
        run["data_version"] = "old-version"
    elif fault == "partial":
        run["artifacts"]["partial"] = True
    elif fault == "wrong-ref":
        query["result_ref"]["run_id"] = "another-run"
    elif fault == "truncated":
        page["truncated"] = True
    elif fault == "legacy":
        page["legacy"] = True
    elif fault == "wrong-role":
        query["recipe_output"] = "incorrect-role"
    else:
        query["evidence_ids"] = ["different-raw-evidence"]
    with pytest.raises(verifier.VerificationError):
        verifier.verify_run(client, run, "q01", gold)


def test_same_count_wrong_month_identity_is_rejected(monthly):
    run, client, gold = monthly
    next(iter(client.pages.values()))["rows"][0][0] = "2024-12"
    with pytest.raises(verifier.VerificationError, match="chronological"):
        verifier.verify_run(client, run, "q01", gold)


def test_external_server_credentials_or_redirect_targets_not_allowed():
    for address in ("https://example.com", "http://user:pass@localhost:8010", "http://localhost:8010/path", "http://localhost/?secret=a"):
        with pytest.raises(Exception):
            verifier.loopback_url(address)
    assert verifier.loopback_url("http://127.0.0.1:8010/") == "http://127.0.0.1:8010"


@pytest.fixture(scope="module")
def corpus_gold():
    if not all((ROOT / "data" / f"{domain}-light-bi-3-1.duckdb").exists() for domain in set(verifier.DOMAINS.values())):
        pytest.skip("Generate the synthetic corpus explicitly before integration acceptance")
    return verifier.load_gold(list(verifier.TOOLS))


@pytest.mark.parametrize("question", list(verifier.TOOLS))
def test_verifier_contract_matches_actual_tool_outputs_on_raw_corpus(question, corpus_gold):
    import duckdb
    domain = verifier.DOMAINS[question]
    scene = json.loads((ROOT / "scenarios" / domain / "light_bi.json").read_text(encoding="utf-8"))
    recipe = next(row for row in scene["diagnostics"] if row["tool"] == verifier.TOOLS[question])
    inputs = []
    with duckdb.connect(str(ROOT / "data" / f"{domain}-light-bi-3-1.duckdb"), read_only=True) as conn:
        for draft in recipe["queries"]:
            cursor = conn.execute(draft["sql"])
            inputs.append({"id": draft["id"], "columns": [column[0] for column in cursor.description],
                           "rows": cursor.fetchall(), "truncated": False})
    result = run_analysis(recipe["tool"], inputs, recipe["config"])
    assert result["status"] == "ok", result
    run, client = saved_fixture(question, corpus_gold)
    actual_outputs = {"series": result["series"], **{table["id"]: table["rows"] for table in result.get("tables", [])}}
    for query in run["artifacts"]["queries"]:
        actual = actual_outputs[query["recipe_output"]]
        columns = list(actual[0])
        rows = [[row[column] for column in columns] for row in actual]
        query.update(columns=columns, rows=rows[:50], total_rows=len(rows))
        client.pages[f"/api/v1/runs/{run['run_id']}/queries/{query['id']}/rows"].update(columns=columns, rows=rows, total=len(rows))
    checked = verifier.verify_run(client, run, question, corpus_gold)
    assert checked["numeric_verification"] == "passed"
    assert checked["unchecked_outputs"] == []
