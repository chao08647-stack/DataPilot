"""Explicitly pin verified synthetic history; never submits runs or calls models.

Requires existing successful independent numeric reports, current nonpartial
catalog runs, and a quiescent runtime. Only dashboard creation/pin/refresh POSTs
are allowed. Each question contributes one primary non-table chart. No template
is installed or upgraded, and no existing card is replaced or deleted.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

import httpx
import verify_history as verification

ROOT = Path(__file__).resolve().parents[1]
TITLE = "2025 经营分析"
CHART_FIELDS = ("type", "x", "y", "series", "group_by", "size", "unit", "x_unit", "value_divisor", "total_column")


def get(client, path, params=None):
    return client.get(path, params=params).raise_for_status().json()


def dashboard_post(client, path, body):
    """Defense in depth: this program has no generic write API."""
    if not re.fullmatch(r"/api/v1/dashboards(?:/[a-zA-Z0-9_-]+/(?:cards|refresh))?", path):
        raise verification.VerificationError("Only explicit dashboard create/pin/refresh POSTs are permitted")
    allowed = {"domain_id", "title"} if path == "/api/v1/dashboards" else {"run_id", "query_id", "chart_index"} if path.endswith("/cards") else set()
    if set(body) - allowed:
        raise verification.VerificationError("Dashboard request contains fields outside the pin workflow")
    return client.post(path, json=body).raise_for_status().json()


def read_numeric_reports():
    records = []
    for path in sorted((ROOT / ".runtime").glob("verify-history-*.json")):
        if path.stat().st_size > 4 * 1024 * 1024:
            continue
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        if report.get("kind") != "independent_saved_history_numeric_verification" or report.get("fatal_error"):
            continue
        for case in report.get("cases", []):
            if case.get("numeric_verification") == "passed" and not case.get("unchecked_outputs"):
                records.append({**case, "report_file": path.name})
    return records


def usage_snapshot(client):
    records, offset = [], 0
    while True:
        page = get(client, "/api/v1/runs", {"offset": offset, "limit": 200})
        records.extend(page["items"])
        offset += len(page["items"])
        if offset >= page["total"]:
            break
        if not page["items"] or offset >= 10000:
            raise verification.VerificationError("Cannot take a bounded consistent history snapshot")
    if len(records) != page["total"] or len({r["run_id"] for r in records}) != len(records):
        raise verification.VerificationError("History changed while collecting run identities")
    if any(row["status"] in {"queued", "running"} for row in records):
        raise verification.VerificationError("Wait for running/queued analyses to finish before pin verification")
    usage = {}
    for row in records:
        run = get(client, "/api/v1/runs/" + quote(row["run_id"], safe=""))
        if run["status"] in {"queued", "running"}:
            raise verification.VerificationError("A concurrent analysis started during the read-only preflight")
        usage[run["run_id"]] = run.get("artifacts", {}).get("usage", {})
    digest = hashlib.sha256(json.dumps(usage, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return {"run_count": len(records), "run_ids": sorted(usage), "usage_hash": digest,
            "persisted_model_calls": sum(value.get("calls", 0) for value in usage.values())}


def select_plans(client, questions, numeric_reports):
    domains = {domain["id"]: domain for domain in get(client, "/api/v1/domains") if domain.get("synthetic")}
    plans = []
    for question in questions:
        domain_id = verification.DOMAINS[question]
        if domain_id not in domains:
            raise verification.VerificationError("Selected synthetic domain unavailable")
        version = domains[domain_id]["model_version"]
        candidates = {}
        for case in numeric_reports:
            if (case.get("question_id") != question or case.get("data_version") != verification.VERSION
                    or case.get("model_version") != version or case.get("numeric_verification") != "passed"
                    or case.get("unchecked_outputs")):
                continue
            if case["run_id"] not in candidates:
                run = get(client, "/api/v1/runs/" + quote(case["run_id"], safe=""))
                artifact = run.get("artifacts", {})
                if (run.get("question_id") == question and run.get("origin") == "question_catalog"
                        and run.get("domain_id", run.get("scenario_id")) == domain_id
                        and run.get("data_version") == verification.VERSION and run.get("model_version") == version
                        and artifact.get("model_version") == version and run.get("status") == "completed"
                        and not artifact.get("partial")):
                    candidates[run["run_id"]] = (run, case["report_file"])
        if not candidates:
            raise verification.VerificationError("No current successful numerically verified run for " + question)
        run, report_file = max(candidates.values(), key=lambda pair: (pair[0].get("created_at", ""), pair[0]["run_id"]))
        artifact = run["artifacts"]
        calculations = [row for row in artifact.get("calculations", []) if row.get("tool") == verification.TOOLS[question] and row.get("status") == "ok"]
        if len(calculations) != 1:
            raise verification.VerificationError("Selected run lacks one successful required calculation")
        calculation = calculations[0]
        output = next((row for row in calculation.get("output_tables", []) if row["id"] == "series"), None)
        query = next((row for row in artifact.get("queries", []) if output and row["id"] == output["query_id"]), None)
        if not query or not query.get("derived") or query.get("recipe_output") != "series":
            raise verification.VerificationError("Primary derived output is unavailable")
        if (not calculation.get("recipe") or calculation["recipe"].get("tool") != calculation["tool"]
                or set(query.get("evidence_ids", [])) != set(calculation.get("evidence_ids", []))):
            raise verification.VerificationError("Stored calculation recipe or input provenance is inconsistent")
        charts = [chart for chart in artifact.get("charts", []) if chart.get("query_id") == query["id"]]
        eligible = [(index, chart) for index, chart in enumerate(charts) if chart.get("type") != "table"]
        if not eligible:
            raise verification.VerificationError("Primary result has no non-table chart")
        chart_index, chart = eligible[0]
        rows = verification._saved_rows(client, run["run_id"], query)
        plans.append({"question_id": question, "run": run, "query": query, "chart": chart,
                      "chart_index": chart_index, "calculation": calculation, "saved_rows": rows,
                      "domain_id": domain_id, "model_version": version, "report_file": report_file})
    return plans


def chart_equal(before, after):
    def canonical(chart):
        return {key: chart.get(key, [] if key in {"y", "series"} else 1 if key == "value_divisor" else None)
                for key in CHART_FIELDS}
    return canonical(before) == canonical(after)


def check_card(plan, card):
    run, query = plan["run"], plan["query"]
    provenance = card.get("provenance", {})
    if (provenance.get("run_id") != run["run_id"] or provenance.get("query_id") != query["id"]
            or provenance.get("analysis_generated_at") != run["updated_at"]):
        raise verification.VerificationError("Pinned card changed source run/query/analysis time")
    recipe = card.get("analysis_recipe", {})
    original = plan["calculation"]["recipe"]
    if recipe.get("tool") != original["tool"] or recipe.get("output") != query["recipe_output"] or recipe.get("config") != original["config"]:
        raise verification.VerificationError("Pinned calculation output or recipe changed")
    if not chart_equal(plan["chart"], card.get("chart", {})):
        raise verification.VerificationError("Existing/pinned chart differs from selected history chart")


def _row_keys(question):
    return {"q01": ["period"], "q02": ["component"], "q03": ["channel"], "q04": ["group"],
            "q05": ["cohort"], "q06": ["stage"], "q07": ["period"],
            "q08": ["cohort", "feature_group", "window"], "q09": ["store_id"], "q10": ["store_id", "product_id"]}[question]


def check_refreshed(plan, card):
    check_card(plan, card)
    snapshot = card.get("snapshot") or {}
    if snapshot.get("status") != "ready" or snapshot.get("stale") or snapshot.get("filters"):
        raise verification.VerificationError("Pinned card did not produce a fresh unfiltered snapshot")
    query = snapshot.get("query", {})
    if query.get("truncated") or query.get("columns") != plan["query"]["columns"]:
        raise verification.VerificationError("Refresh returned truncated/different result columns")
    actual = [dict(zip(query["columns"], row)) for row in query.get("rows", [])]
    compared = verification.compare_rows(plan["saved_rows"], actual, _row_keys(plan["question_id"]))
    if not chart_equal(plan["chart"], snapshot.get("chart", {})):
        raise verification.VerificationError("Refresh silently replaced the selected chart configuration")
    if snapshot.get("analysis_generated_at") != plan["run"]["updated_at"]:
        raise verification.VerificationError("Refresh overwrote the original analysis timestamp")
    if plan["chart"].get("type") == "waterfall":
        a, b = plan["chart"].get("start_value"), snapshot["chart"].get("start_value")
        verification.compare_rows([{"start_value": a}], [{"start_value": b}], [])
    return compared


def pin_verified(client, questions, numeric_reports, report):
    before = usage_snapshot(client)
    plans = select_plans(client, questions, numeric_reports)
    report["before"] = {key: value for key, value in before.items() if key != "run_ids"}
    report["cards"] = []
    report["boards"] = []
    existing = get(client, "/api/v1/dashboards")
    boards = {}
    # Resolve every board before any write. Preserve old-version and unrelated boards.
    for plan in plans:
        domain = plan["domain_id"]
        choices = [board for board in existing if board.get("domain_id") == domain and board.get("title") == TITLE
                   and board.get("model_version") == plan["model_version"]]
        if len(choices) > 1:
            raise verification.VerificationError("Multiple current boards have the reserved title; choose manually")
        if choices and choices[0].get("filters"):
            raise verification.VerificationError("Existing board has filters; refusing to overwrite user scope")
        boards[domain] = choices[0] if choices else None
    for domain, board in boards.items():
        if board is None:
            continue
        new_cards = 0
        for plan in [p for p in plans if p["domain_id"] == domain]:
            matches = [card for card in board["cards"] if card.get("provenance", {}).get("run_id") == plan["run"]["run_id"]
                       and card.get("provenance", {}).get("query_id") == plan["query"]["id"]]
            if len(matches) > 1:
                raise verification.VerificationError("Duplicate source cards already exist; no automatic deletion")
            if matches:
                check_card(plan, matches[0])
            else:
                new_cards += 1
        if len(board["cards"]) + new_cards > 24:
            raise verification.VerificationError("Existing board lacks capacity; no partial pin attempted")
        report["boards"].append({"domain_id": domain, "board_id": board["id"], "created": False})
    try:
        for plan in plans:
            domain = plan["domain_id"]
            if boards[domain] is None:
                boards[domain] = dashboard_post(client, "/api/v1/dashboards", {"domain_id": domain, "title": TITLE})
                report["boards"].append({"domain_id": domain, "board_id": boards[domain]["id"], "created": True})
            board = boards[domain]
            matches = [card for card in board["cards"] if card.get("provenance", {}).get("run_id") == plan["run"]["run_id"]
                       and card.get("provenance", {}).get("query_id") == plan["query"]["id"]]
            if len(matches) > 1:
                raise verification.VerificationError("Duplicate source cards already exist; no automatic deletion")
            reused = bool(matches)
            if not reused:
                board = dashboard_post(client, f"/api/v1/dashboards/{board['id']}/cards", {
                    "run_id": plan["run"]["run_id"], "query_id": plan["query"]["id"], "chart_index": plan["chart_index"]})
                boards[domain] = board
                matches = [card for card in board["cards"] if card.get("provenance", {}).get("run_id") == plan["run"]["run_id"]
                           and card.get("provenance", {}).get("query_id") == plan["query"]["id"]]
                if len(matches) != 1:
                    raise verification.VerificationError("Pin response did not contain one identifiable source card")
            check_card(plan, matches[0])
            plan["card_id"] = matches[0]["id"]
            report["cards"].append({"question_id": plan["question_id"], "domain_id": domain, "board_id": board["id"],
                                    "card_id": matches[0]["id"], "run_id": plan["run"]["run_id"], "query_id": plan["query"]["id"],
                                    "tool": plan["calculation"]["tool"], "recipe_output": plan["query"]["recipe_output"],
                                    "numeric_report": plan["report_file"], "reused": reused, "refresh_verified": False})
        for domain, board in boards.items():
            refreshed = dashboard_post(client, f"/api/v1/dashboards/{board['id']}/refresh", {})
            cards = {card["id"]: card for card in refreshed["cards"]}
            for plan in [p for p in plans if p["domain_id"] == domain]:
                if plan["card_id"] not in cards:
                    raise verification.VerificationError("Refreshed board lost a selected card")
                result = check_refreshed(plan, cards[plan["card_id"]])
                record = next(row for row in report["cards"] if row["card_id"] == plan["card_id"])
                record.update(refresh_verified=True, **result)
    finally:
        after = usage_snapshot(client)
        report["after"] = {key: value for key, value in after.items() if key != "run_ids"}
        report["run_count_unchanged"] = before["run_ids"] == after["run_ids"]
        report["model_usage_unchanged"] = before["usage_hash"] == after["usage_hash"]
        report["persisted_model_calls_delta"] = after["persisted_model_calls"] - before["persisted_model_calls"]
    if not report["run_count_unchanged"] or not report["model_usage_unchanged"]:
        raise verification.VerificationError("Concurrent run/model usage changed; no zero-model-call claim")
    report["passed"] = True
    return report


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", required=True, action="store_true")
    parser.add_argument("--questions", nargs="+", choices=list(verification.TOOLS), default=list(verification.TOOLS))
    parser.add_argument("--base-url", type=verification.loopback_url, default="http://127.0.0.1:8010")
    args = parser.parse_args()
    report = {"kind": "verified_history_dashboard_pin", "started_at": datetime.now(UTC).isoformat(), "passed": False,
              "questions": list(dict.fromkeys(args.questions)), "text_semantic_verification": "not_performed_by_this_script"}
    directory = ROOT / ".runtime"
    directory.mkdir(exist_ok=True)
    destination = directory / ("pin-verified-history-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + uuid4().hex[:8] + ".json")
    lock = directory / "pin-verified-history.lock"
    owned_lock = False
    try:
        with lock.open("x", encoding="utf-8") as handle:
            handle.write(report["started_at"])
        owned_lock = True
        with httpx.Client(base_url=args.base_url, timeout=90, trust_env=False, follow_redirects=False) as client:
            pin_verified(client, report["questions"], read_numeric_reports(), report)
    except Exception as exc:
        report["error_type"] = type(exc).__name__
        if isinstance(exc, verification.VerificationError):
            report["reason"] = str(exc)
        if isinstance(exc, httpx.HTTPStatusError):
            report["http_status"] = exc.response.status_code
        report["passed"] = False
    finally:
        if owned_lock:
            lock.unlink()
        destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False), flush=True)
    print("Pin report:", destination.relative_to(ROOT), flush=True)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
