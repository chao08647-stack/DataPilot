"""Offline API mocks for explicit pinning; never creates a real board."""
import importlib.util
import sys
from copy import deepcopy
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("verify_history", ROOT / "scripts/verify_history.py")
verification = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verification)
sys.modules["verify_history"] = verification
SPEC = importlib.util.spec_from_file_location("pin_verified_history", ROOT / "scripts/pin_verified_history.py")
pin = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pin)


class Response:
    def __init__(self, body):
        self.body = body

    def json(self):
        return deepcopy(self.body)

    def raise_for_status(self):
        return self


class FakeAPI:
    def __init__(self):
        self.boards, self.posts, self.runs, self.pages = {}, [], {}, {}
        self.corrupt_last = self.override_chart = self.mutate_usage = False
        self.reports = []
        for question, domain, count in (("q01", "ecommerce", 12), ("q08", "saas", 72)):
            identifier = "run-" + question
            columns = ["period", "amount"] if question == "q01" else ["cohort", "feature_group", "window", "eligible", "retained"]
            rows = [[f"2025-{i+1:02}", i*10.] for i in range(count)] if question == "q01" else [
                [f"2025-{i//6+1:02}", "early" if i%6<3 else "other", f"D{(i%3+1)*30}", 40, 20] for i in range(count)]
            query = {"id": "arbitrary-result", "derived": True, "recipe_output": "series", "columns": columns,
                     "rows": rows[:50], "total_rows": count, "result_ref": {"run_id": identifier, "query_id": "arbitrary-result"}, "evidence_ids": ["input"]}
            chart = {"query_id": query["id"], "type": "combo" if question == "q01" else "heatmap", "x": columns[0],
                     "y": [columns[-1]], "title": "approved-chart"}
            calculation = {"tool": verification.TOOLS[question], "status": "ok", "evidence_ids": ["input"],
                           "recipe": {"tool": verification.TOOLS[question], "config": {"query_id": "source"}, "query_map": {"source": "input"}},
                           "output_tables": [{"id": "series", "query_id": query["id"], "row_count": count}]}
            self.runs[identifier] = {"run_id": identifier, "question_id": question, "domain_id": domain, "scenario_id": domain,
                "origin": "question_catalog", "status": "completed", "data_version": verification.VERSION, "model_version": "published-model",
                "updated_at": "2026-09-30T10:00:00Z", "created_at": "2026-09-30T09:00:00Z", "artifacts": {
                    "partial": False, "model_version": "published-model", "queries": [query], "charts": [chart], "calculations": [calculation], "usage": {"calls": 8}}}
            self.pages[identifier] = {"columns": columns, "rows": rows, "total": count, "truncated": False, "full_result_available": True, "legacy": False}
            self.reports.append({"question_id": question, "run_id": identifier, "data_version": verification.VERSION, "model_version": "published-model",
                                 "numeric_verification": "passed", "unchecked_outputs": [], "report_file": "test-only-report.json"})

    def get(self, path, params=None):
        if path == "/api/v1/runs":
            return Response({"items": list(self.runs.values()), "total": len(self.runs)})
        if path == "/api/v1/domains":
            return Response([{"id": domain, "synthetic": True, "model_version": "published-model"} for domain in ("ecommerce", "saas")])
        if path == "/api/v1/dashboards":
            return Response(list(self.boards.values()))
        if path.startswith("/api/v1/runs/"):
            identifier = path.split("/")[4]
            return Response(self.pages[identifier] if path.endswith("/rows") else self.runs[identifier])
        raise AssertionError(path)

    def post(self, path, json):
        self.posts.append((path, deepcopy(json)))
        if path == "/api/v1/dashboards":
            identifier = f"board-{len(self.boards)}"
            self.boards[identifier] = {"id": identifier, **json, "model_version": "published-model", "filters": {}, "cards": []}
            return Response(self.boards[identifier])
        board = self.boards[path.split("/")[4]]
        if path.endswith("/cards"):
            run = self.runs[json["run_id"]]
            calc = run["artifacts"]["calculations"][0]
            chart = deepcopy(run["artifacts"]["charts"][0])
            chart.pop("query_id")
            board["cards"].append({"id": "card-"+run["run_id"], "chart": chart, "title": chart["title"],
                "provenance": {"run_id": run["run_id"], "query_id": json["query_id"], "analysis_generated_at": run["updated_at"]},
                "analysis_recipe": {"tool": calc["tool"], "config": calc["recipe"]["config"], "output": "series"}})
        elif path.endswith("/refresh"):
            for card in board["cards"]:
                source = card["provenance"]["run_id"]
                page = deepcopy(self.pages[source])
                if self.corrupt_last:
                    page["rows"][-1][-1] += 1
                chart = deepcopy(card["chart"])
                if self.override_chart:
                    chart["type"] = "line"
                if self.mutate_usage:
                    self.runs[source]["artifacts"]["usage"]["calls"] += 1
                card["snapshot"] = {"status": "ready", "stale": False, "filters": {}, "query": page,
                                    "chart": chart, "analysis_generated_at": card["provenance"]["analysis_generated_at"]}
        else:
            raise AssertionError(path)
        return Response(board)


def test_create_then_reuse_without_duplicate_pins_and_full_rows_not_preview():
    api = FakeAPI()
    first = pin.pin_verified(api, ["q01", "q08"], api.reports, {})
    assert first["passed"] and first["model_usage_unchanged"] and first["run_count_unchanged"]
    assert [card["rows_checked"] for card in first["cards"]] == [12, 72]
    assert len(api.boards) == 2
    posts = len(api.posts)
    second = pin.pin_verified(api, ["q01", "q08"], api.reports, {})
    assert all(card["reused"] for card in second["cards"])
    assert len(api.boards) == 2 and len(api.posts) == posts + 2  # refresh only
    assert all(path.startswith("/api/v1/dashboards") for path, _ in api.posts)


@pytest.mark.parametrize("fault", ["missing-report", "partial", "old-version", "running"])
def test_all_preflight_failures_happen_before_any_dashboard_write(fault):
    api = FakeAPI()
    if fault == "missing-report":
        api.reports = []
    elif fault == "partial":
        api.runs["run-q08"]["artifacts"]["partial"] = True
    elif fault == "old-version":
        api.runs["run-q08"]["model_version"] = "old"
    else:
        api.runs["run-q08"]["status"] = "running"
    with pytest.raises(verification.VerificationError):
        pin.pin_verified(api, ["q01", "q08"], api.reports, {})
    assert api.posts == []


def test_mismatch_after_row_fifty_is_detected_and_retained_board_not_deleted():
    api = FakeAPI()
    api.corrupt_last = True
    with pytest.raises(verification.VerificationError, match="Numeric mismatch"):
        pin.pin_verified(api, ["q08"], api.reports, {})
    assert len(api.boards) == 1
    assert len(api.runs) == 2


def test_chart_replacement_is_not_silently_accepted():
    api = FakeAPI()
    api.override_chart = True
    with pytest.raises(verification.VerificationError, match="replaced"):
        pin.pin_verified(api, ["q01"], api.reports, {})


def test_changed_model_usage_cannot_claim_zero_calls():
    api = FakeAPI()
    api.mutate_usage = True
    report = {}
    with pytest.raises(verification.VerificationError, match="usage changed"):
        pin.pin_verified(api, ["q01"], api.reports, report)
    assert report["model_usage_unchanged"] is False


def test_no_run_or_template_write_route_exists():
    api = FakeAPI()
    for path in ("/api/v1/runs", "/api/v1/domains", "/api/v1/semantic-models/x/publish", "/api/v1/dashboards/x/cards/y/drilldown"):
        with pytest.raises(verification.VerificationError, match="Only explicit"):
            pin.dashboard_post(api, path, {})
    with pytest.raises(verification.VerificationError, match="outside"):
        pin.dashboard_post(api, "/api/v1/dashboards", {"domain_id": "ecommerce", "title": "x", "template_id": "not-permitted"})
    assert api.posts == []
