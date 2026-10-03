"""Opt-in pin/refresh verification against an existing successful synthetic task; no model calls."""
import argparse
import json

import httpx
from dev import ROOT
from pin_verified_history import usage_snapshot


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--integration", required=True, action="store_true")
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    with httpx.Client(base_url="http://127.0.0.1:8010", timeout=60, trust_env=False) as client:
        response = client.get(f"/api/v1/runs/{args.run_id}")
        response.raise_for_status()
        run = response.json()
        domain = client.get(f"/api/v1/domains/{run['scenario_id']}").json()
        assert domain.get("synthetic") and run["status"] == "completed" and not run["artifacts"].get("partial")
        query = next(q for q in run["artifacts"]["queries"] if q.get("derived"))
        original = client.get(f"/api/v1/runs/{run['run_id']}/queries/{query['id']}/rows", params={"limit": 20000}).raise_for_status().json()
        assert not original["truncated"] and original["total"] == len(original["rows"])
        before = usage_snapshot(client)
        created = client.post("/api/v1/dashboards", json={"domain_id": domain["id"], "title": "验收 · 实时分析结果固定"})
        created.raise_for_status()
        board_id = created.json()["id"]
        # Retain this explicitly named board to inspect its provenance and repeat refresh.
        pinned = client.post(f"/api/v1/dashboards/{board_id}/cards", json={"run_id": run["run_id"], "query_id": query["id"]})
        pinned.raise_for_status()
        refreshed = client.post(f"/api/v1/dashboards/{board_id}/refresh", json={})
        refreshed.raise_for_status()
        card = refreshed.json()["cards"][0]
        assert card["snapshot"]["status"] == "ready", card["snapshot"].get("error")
        assert card["snapshot"]["query"]["columns"] == original["columns"]
        assert card["snapshot"]["query"]["rows"] == original["rows"]
        assert card["snapshot"]["analysis_generated_at"] == run["updated_at"]
        assert before == usage_snapshot(client)
        report = {"passed": True, "run_id": run["run_id"], "board_id": board_id, "full_rows_match": True, "run_count_and_usage_unchanged": True,
                  "query_count": len(card["snapshot"]["queries"]), "model_calls": 0, "original_analysis_time_preserved": True}
        (ROOT / ".runtime" / "enterprise-pin-check.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report))


if __name__ == "__main__":
    main()
