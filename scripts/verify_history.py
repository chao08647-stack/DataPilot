"""Explicit GET-only numeric acceptance of saved synthetic catalog runs.

Gold is computed exclusively by test-owned raw-table helpers, never by runtime
SQL or analysis tools. This command cannot submit/retry/update a task. It writes
only a timestamped, credential-free local verification report.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import math
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote, urlsplit
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parents[1]
VERSION = "light-bi-3.1"
TOOLS = {"q01": "period_overview", "q02": "grouped_profit_change", "q03": "customer_value_90d",
         "q04": "refund_diagnosis", "q05": "customer_repeat_cohort", "q06": "conversion_funnel",
         "q07": "mrr_monthly_bridge", "q08": "account_retention", "q09": "store_driver_comparison",
         "q10": "inventory_asof"}
DOMAINS = {q: "ecommerce" if q <= "q05" else "saas" if q <= "q08" else "retail" for q in TOOLS}
COMPONENTS = [("gross_sales", "标价成交额", 1), ("discounts", "折扣", -1), ("refunds", "成功退款", -1),
              ("cogs", "成交成本", -1), ("fulfillment", "履约费用", -1),
              ("payment_fees", "支付费用", -1), ("marketing", "营销支出", -1)]
MRR = [("new_mrr", "新增"), ("expansion", "扩张"), ("contraction", "收缩"), ("churn", "流失"), ("reactivation", "恢复")]
STAGES = [("registered", "注册账户"), ("activated", "完成激活"), ("trial_started", "开始试用"), ("paid", "付费账户")]


class VerificationError(ValueError):
    """Safe local error text: never include SQL, credentials or upstream bodies."""


def _load_helper(name):
    location = ROOT / "backend/tests" / (name + ".py")
    spec = importlib.util.spec_from_file_location("_history_" + name, location)
    if spec is None or spec.loader is None:
        raise VerificationError("Independent test oracle unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_gold(questions):
    paths = {domain: ROOT / "data" / f"{domain}-light-bi-3-1.duckdb" for domain in set(DOMAINS.values())}
    result = {}
    if any(q <= "q05" for q in questions):
        result.update(_load_helper("bi_raw_oracle").compute_gold(paths))
    if any(q >= "q06" for q in questions):
        result.update(_load_helper("bi_domain_oracle").compute_domain_gold(paths))
    return result


def _spec(identifier, keys, rows):
    return {"output": identifier, "keys": keys, "rows": rows}


def expected_outputs(question_id, gold):
    """Adapt independent numbers to documented output columns, not runtime IDs."""
    value = gold[question_id]
    if question_id == "q01":
        return [_spec("series", ["period"], value["series"]), _spec("annual_summary", [], [value["annual_summary"]])]
    if question_id == "q02":
        before, after = value["periods"]["2025-Q3"], value["periods"]["2025-Q4"]
        specs = [_spec("series", ["component"], [{"component": label, "delta": (after[key]-before[key])*sign}
                                                for key, label, sign in COMPONENTS])]
        for dimension in ("channel", "category"):
            groups = defaultdict(lambda: defaultdict(lambda: defaultdict(float)))
            for row in value["detail"]:
                for key, _, _ in COMPONENTS:
                    groups[row[dimension]][row["period"]][key] += row[key]
            rows = []
            for segment, periods in sorted(groups.items()):
                a, b = periods["2025-Q3"], periods["2025-Q4"]
                row = {"segment": segment, "sales_before": a["gross_sales"]-a["discounts"],
                       "sales_after": b["gross_sales"]-b["discounts"],
                       "profit_before": sum(a[key]*sign for key, _, sign in COMPONENTS),
                       "profit_after": sum(b[key]*sign for key, _, sign in COMPONENTS)}
                row.update(sales_delta=row["sales_after"]-row["sales_before"], profit_delta=row["profit_after"]-row["profit_before"])
                row.update({key+"_effect": a[key]-b[key] for key, _, sign in COMPONENTS if sign < 0})
                rows.append(row)
            specs.append(_spec(dimension+"_contribution", ["segment"], rows))
        return specs
    if question_id == "q03":
        return [_spec("series", ["channel"], value["series"])]
    if question_id == "q04":
        rows = []
        for row in value["series"]:
            n, refunded, late, late_refunded = (row[key] for key in ("orders", "refunded_orders", "late_orders", "late_refunded_orders"))
            late_rate = late_refunded*100/late if late else None
            timely_rate = (refunded-late_refunded)*100/(n-late) if n > late else None
            rows.append({"group": row["group"], "orders": n, "refunded_orders": refunded, "late_orders": late,
                         "refund_pct": round(refunded*100/n, 4),
                         "late_refund_pct": round(late_rate, 4) if late_rate is not None else None,
                         "on_time_refund_pct": round(timely_rate, 4) if timely_rate is not None else None,
                         "gap_pp": round(late_rate-timely_rate, 4) if late_rate is not None and timely_rate is not None else None})
        specs = [_spec("series", ["group"], rows)]
        for dimension in ("product_name", "supplier_name", "reason"):
            groups = defaultdict(float)
            for row in value["refund_reasons"]:
                groups[row[dimension]] += row["refunded_amount"]
            total = sum(groups.values())
            specs.append(_spec("refund_"+dimension, [dimension], [{dimension: label, "refunded_amount": amount,
                                "amount_share_pct": amount*100/total} for label, amount in sorted(groups.items())]))
        return specs
    if question_id == "q05":
        return [_spec("series", ["cohort"], value["series"]), _spec("new_existing", ["customer_kind"], value["new_existing"])]
    if question_id == "q06":
        a, b = value["periods"]["2025-Q3"], value["periods"]["2025-Q4"]
        specs = [_spec("series", ["stage"], [{"stage": label, "accounts": b[key]} for key, label in STAGES]),
                 _spec("funnel_comparison", ["stage"], [{"stage": label, "baseline": a[key], "current": b[key], "delta": b[key]-a[key]} for key, label in STAGES])]
        for dimension in ("channel", "company_size"):
            groups = defaultdict(lambda: defaultdict(int))
            for row in value["groups"]:
                for key, _ in STAGES:
                    groups[(row["period"], row[dimension])][key] += row[key]
            rows = []
            for (period, segment), numbers in sorted(groups.items()):
                rows.append({"period": period, dimension: segment, **numbers,
                             "paid_pct": numbers["paid"]*100/numbers["registered"] if numbers["registered"] else None,
                             "activation_loss": numbers["registered"]-numbers["activated"],
                             "trial_loss": numbers["activated"]-numbers["trial_started"],
                             "payment_loss": numbers["trial_started"]-numbers["paid"]})
            specs.append(_spec("funnel_"+dimension, ["period", dimension], rows))
        return specs
    if question_id == "q07":
        specs = [_spec("series", ["period"], value["series"]),
                 _spec("annual_mrr_bridge", ["component"], [{"component": label, "delta": value["annual_components"][key]} for key, label in MRR]),
                 _spec("annual_account_changes", ["account_id"], value["annual_account_changes"])]
        if "account_mrr_changes" in value:
            specs.append(_spec("account_mrr_changes", ["period", "account_id"], value["account_mrr_changes"]))
        return specs
    if question_id == "q08":
        return [_spec("series", ["cohort", "feature_group", "window"], value["series"]),
                _spec("retention_summary", ["feature_group", "window"], value["retention_summary"])]
    if question_id == "q09":
        specs = [_spec("series", ["store_id"], value["series"])]
        for identifier, keys in (("declining_store_categories", ["category"]), ("declining_store_dates", ["date"])):
            if identifier in value:
                specs.append(_spec(identifier, keys, value[identifier]))
        return specs
    if question_id == "q10":
        return [_spec("series", ["store_id", "product_id"], value["series"]),
                _spec("inventory_attention", ["store_id", "product_id"], [row for row in value["series"] if row["risk"] != "normal"])]
    raise VerificationError("Unsupported question ID")


def compare_rows(expected, actual, keys):
    def index(rows):
        indexed = {}
        for row in rows:
            try:
                key = tuple(str(row[column]) for column in keys)
            except KeyError as exc:
                raise VerificationError("Missing row identity column") from exc
            if key in indexed:
                raise VerificationError("Duplicate business row identity")
            indexed[key] = row
        return indexed
    before, after = index(expected), index(actual)
    if before.keys() != after.keys():
        raise VerificationError(f"Business row set differs: expected {len(before)}, received {len(after)}")
    checked = 0
    for identity, reference in before.items():
        row = after[identity]
        for column, number in reference.items():
            if column not in row:
                raise VerificationError("Missing expected result column: " + column)
            received = row[column]
            if column in keys:
                continue
            if isinstance(number, (float, int)) and not isinstance(number, bool):
                if isinstance(received, bool) or not isinstance(received, (float, int)) or not math.isfinite(received):
                    raise VerificationError("Non-finite or nonnumeric field: " + column)
                if not math.isclose(number, received, abs_tol=0.00011, rel_tol=1e-12):
                    raise VerificationError(f"Numeric mismatch {column}: expected {number}, received {received}")
                checked += 1
            elif received != number:
                raise VerificationError("Categorical/null mismatch: " + column)
    return {"rows_checked": len(expected), "numeric_cells_checked": checked}


def _saved_rows(client, run_id, query):
    ref = query.get("result_ref", {})
    if ref != {"run_id": run_id, "query_id": query["id"]}:
        raise VerificationError("Saved full-result reference missing or crosses run boundary")
    response = client.get(f"/api/v1/runs/{quote(run_id, safe='')}/queries/{quote(query['id'], safe='')}/rows", params={"offset": 0, "limit": 20000})
    response.raise_for_status()
    page = response.json()
    if page.get("truncated") or page.get("legacy") or not page.get("full_result_available"):
        raise VerificationError("Result truncated or lacks independently persisted full rows")
    if page.get("total") != len(page.get("rows", [])) or page["total"] != query.get("total_rows"):
        raise VerificationError("Saved full result row count does not match metadata")
    columns = page.get("columns", [])
    if columns != query.get("columns") or len(columns) != len(set(columns)):
        raise VerificationError("Saved full result column metadata mismatch")
    if any(len(row) != len(columns) for row in page["rows"]):
        raise VerificationError("Malformed saved result row")
    return [dict(zip(columns, row)) for row in page["rows"]]


def verify_run(client, run, question_id, gold, *, current_model_version=VERSION):
    if run.get("question_id") != question_id or run.get("origin") != "question_catalog":
        raise VerificationError("Run does not identify the selected catalog question")
    if run.get("domain_id", run.get("scenario_id")) != DOMAINS[question_id]:
        raise VerificationError("Wrong question business domain")
    if run.get("data_version") != VERSION or run.get("model_version") != current_model_version:
        raise VerificationError("Run version does not match independent synthetic corpus")
    artifact = run.get("artifacts", {})
    if artifact.get("model_version", current_model_version) != current_model_version:
        raise VerificationError("Artifact semantic version differs from selected current domain")
    if run.get("status") != "completed" or artifact.get("partial"):
        raise VerificationError("Run is not a completed, nonpartial result")
    calculations = [row for row in artifact.get("calculations", []) if row.get("tool") == TOOLS[question_id]]
    if len(calculations) != 1 or calculations[0].get("status") != "ok":
        raise VerificationError("Required deterministic tool missing, repeated or unsuccessful")
    calculation = calculations[0]
    queries = {row["id"]: row for row in artifact.get("queries", [])}
    if len(queries) != len(artifact.get("queries", [])):
        raise VerificationError("Duplicate saved query IDs")
    outputs = {row["id"]: row for row in calculation.get("output_tables", [])}
    if len(outputs) != len(calculation.get("output_tables", [])):
        raise VerificationError("Duplicate calculation output roles")
    checks = []
    specs = expected_outputs(question_id, gold)
    for spec in specs:
        output = outputs.get(spec["output"])
        if not output or output.get("query_id") not in queries:
            raise VerificationError("Required saved output missing: " + spec["output"])
        query = queries[output["query_id"]]
        if not query.get("derived") or query.get("recipe_output") != spec["output"]:
            raise VerificationError("Saved derived query output role mismatch")
        if set(query.get("evidence_ids", [])) != set(calculation.get("evidence_ids", [])):
            raise VerificationError("Derived query points to different input evidence")
        rows = _saved_rows(client, run["run_id"], query)
        if output.get("row_count") != len(rows):
            raise VerificationError("Calculation output count disagrees with saved result")
        if question_id in {"q01", "q07"} and spec["output"] == "series":
            if [row.get("period") for row in rows] != [f"2025-{month:02}" for month in range(1, 13)]:
                raise VerificationError("Required twelve chronological months missing")
        checks.append({"output": spec["output"], "query_id": query["id"], **compare_rows(spec["rows"], rows, spec["keys"])})
    return {"question_id": question_id, "run_id": run["run_id"], "domain_id": DOMAINS[question_id],
            "data_version": run["data_version"], "model_version": run["model_version"],
            "numeric_verification": "passed", "outputs": checks,
            "text_semantic_verification": "not_performed",
            "unchecked_outputs": sorted(set(outputs)-{spec["output"] for spec in specs}),
            "limits": ["Numeric structured-field verification only at check time; prose formulas, metric wording, causal claims and UI readability require separate review."]}


def completed_candidates(client, questions, current_versions):
    selected, offset = {}, 0
    while len(selected) < len(questions):
        page = client.get("/api/v1/runs", params={"status": "completed", "offset": offset, "limit": 200}).raise_for_status().json()
        for row in page["items"]:
            key = row.get("question_id")
            if (key in questions and key not in selected and row.get("origin") == "question_catalog"
                    and row.get("data_version") == VERSION and row.get("model_version") == current_versions.get(DOMAINS[key])
                    and not row.get("partial")):
                selected[key] = row["run_id"]
        offset += len(page["items"])
        if not page["items"] or offset >= page["total"]:
            break
        if offset >= 10000:
            raise VerificationError("History scan exceeds explicit 10,000 run cap")
    return selected


def loopback_url(value):
    parsed = urlsplit(value)
    if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.username or parsed.password or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
        raise argparse.ArgumentTypeError("Verification uses only credential-free local HTTP")
    return value.rstrip("/")


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", required=True, help="Explicitly read saved local history; never calls models")
    parser.add_argument("--questions", nargs="+", choices=list(TOOLS), default=list(TOOLS))
    parser.add_argument("--base-url", type=loopback_url, default="http://127.0.0.1:8010")
    args = parser.parse_args()
    questions = list(dict.fromkeys(args.questions))
    report = {"kind": "independent_saved_history_numeric_verification", "started_at": datetime.now(UTC).isoformat(),
              "data_version": VERSION, "model_calls": 0, "business_sql_reexecution": 0,
              "raw_oracle_reads": True, "history_mutations": 0, "text_semantic_verification": "not_performed", "cases": []}
    destination = ROOT / ".runtime" / ("verify-history-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + uuid4().hex[:8] + ".json")
    try:
        gold = load_gold(questions)
        with httpx.Client(base_url=args.base_url, timeout=60, trust_env=False, follow_redirects=False) as client:
            domains = client.get("/api/v1/domains").raise_for_status().json()
            versions = {domain["id"]: domain["model_version"] for domain in domains if domain.get("synthetic")}
            if not all(DOMAINS[question] in versions for question in questions):
                raise VerificationError("Selected current synthetic business domain unavailable")
            selected = completed_candidates(client, questions, versions)
            for question in questions:
                run_id = selected.get(question)
                try:
                    if not run_id:
                        raise VerificationError("No completed, nonpartial run for the current data/model version")
                    run = client.get(f"/api/v1/runs/{quote(run_id, safe='')}").raise_for_status().json()
                    record = verify_run(client, run, question, gold, current_model_version=versions[DOMAINS[question]])
                except VerificationError as exc:
                    record = {"question_id": question, "run_id": run_id, "numeric_verification": "failed", "reason": str(exc)}
                report["cases"].append(record)
                print(json.dumps(record, ensure_ascii=False), flush=True)
    except Exception as exc:
        # Never save raw network errors, database paths/credentials or server bodies.
        report["fatal_error"] = type(exc).__name__
        if isinstance(exc, VerificationError):
            report["reason"] = str(exc)
    report["passed"] = (not report.get("fatal_error") and len(report["cases"]) == len(questions)
                         and all(row["numeric_verification"] == "passed" for row in report["cases"]))
    destination.parent.mkdir(exist_ok=True)
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("Verification report:", destination.relative_to(ROOT), flush=True)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
