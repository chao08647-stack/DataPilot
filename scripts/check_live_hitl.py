"""Explicit two-stage HTTP check: leave a HITL task, restart the service, then resume."""
import argparse
import json
import time
from datetime import datetime

import httpx
from dev import ROOT, runner

STATE_FILE = ROOT / ".runtime" / "live-hitl-pending.json"


async def drain(client, run_id):
    async with client.stream("GET", f"/api/v1/runs/{run_id}/events") as stream:
        stream.raise_for_status()
        async for _ in stream.aiter_lines():
            pass
    response = await client.get(f"/api/v1/runs/{run_id}")
    response.raise_for_status()
    return response.json()


async def main(args):
    async with httpx.AsyncClient(base_url="http://127.0.0.1:8010", timeout=370) as client:
        if args.stage in {"prepare", "preference-only"}:
            memories = (await client.get("/api/v1/memories", params={"scenario_id": "ecommerce"})).json()
            preference = next(m for m in memories if m["id"] == "preference:channel-view")
            saved = {"preference_active_before": preference["active"], "checks": []}
            if args.stage == "prepare":
                STATE_FILE.write_text(json.dumps(saved), encoding="utf-8")
            response = await client.patch("/api/v1/memories/preference:channel-view?scenario_id=ecommerce", json={"active": True})
            response.raise_for_status()
            started = time.perf_counter()
            created = await client.post("/api/v1/runs", json={"scenario_id": "ecommerce", "question": "分析2025年已完成订单的销售额，不需要额外补数。"})
            created.raise_for_status()
            result = await drain(client, created.json()["run_id"])
            queries = result["artifacts"].get("queries", [])
            applied = result["status"] == "completed" and any("channel" in q["sql"].lower() and len(q["rows"]) == 3 for q in queries)
            saved["checks"].append({"name": "enabled_preference_affects_grouping", "passed": applied, "run_id": result["run_id"],
                "status": result["status"], "error": result.get("error"), "usage": result["artifacts"].get("usage"), "elapsed_seconds": round(time.perf_counter()-started, 3)})
            if args.stage == "preference-only":
                response = await client.patch("/api/v1/memories/preference:channel-view?scenario_id=ecommerce", json={"active": saved["preference_active_before"]})
                response.raise_for_status()
                saved["preference_restored"] = True
                output = ROOT / ".runtime" / ("live-preference-" + datetime.now().strftime("%Y%m%dT%H%M%S") + ".json")
                output.write_text(json.dumps(saved, ensure_ascii=False, indent=2), encoding="utf-8")
                print(json.dumps(saved, ensure_ascii=False, indent=2))
                if not applied:
                    raise SystemExit("Preference check did not pass.")
                return
            pending = await client.post("/api/v1/runs", json={"scenario_id": "ecommerce", "question": "帮我看看那个指标，按之前那个时间口径统计。"})
            pending.raise_for_status()
            run = await drain(client, pending.json()["run_id"])
            saved.update(run_id=run["run_id"], thread_id=run["thread_id"], before_status=run["status"])
            saved["checks"].append({"name": "http_hitl_waiting", "passed": run["status"] == "waiting_for_input", "usage": run["artifacts"].get("usage")})
            STATE_FILE.write_text(json.dumps(saved, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps(saved, ensure_ascii=False, indent=2))
        else:
            saved = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            try:
                run = (await client.get(f"/api/v1/runs/{saved['run_id']}")).json()
                if run["status"] != "waiting_for_input":
                    raise RuntimeError("Task did not remain waiting through restart")
                answer = "统计2025年已完成订单的销售额总额，不要按渠道或任何维度分组，只用KPI展示，不需要额外补数。"
                response = await client.post(f"/api/v1/runs/{saved['run_id']}/resume", json={"answer": answer})
                response.raise_for_status()
                result = await drain(client, saved["run_id"])
                queries = result["artifacts"].get("queries", [])
                saved["checks"].append({"name": "restart_resume_and_current_request_override", "passed": result["status"] == "completed" and
                    result["thread_id"] == saved["thread_id"] and len(queries) == 1 and len(queries[0]["rows"]) == 1 and "GROUP BY" not in queries[0]["sql"].upper(),
                    "status": result["status"], "error": result.get("error"), "usage": result["artifacts"].get("usage")})
                history = (await client.get(f"/api/v1/threads/{saved['thread_id']}")).json()
                saved["checks"].append({"name": "resume_message_once", "passed": sum(m["content"] == answer for m in history["messages"]) == 1})
            finally:
                response = await client.patch("/api/v1/memories/preference:channel-view?scenario_id=ecommerce", json={"active": saved["preference_active_before"]})
                response.raise_for_status()
                saved["preference_restored"] = True
                STATE_FILE.write_text(json.dumps(saved, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps(saved, ensure_ascii=False, indent=2))
            if not all(check["passed"] for check in saved["checks"]):
                raise SystemExit("Targeted check did not pass; no automatic rerun.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", required=True)
    parser.add_argument("--stage", choices=["prepare", "resume", "preference-only"], required=True)
    runner(main(parser.parse_args()))
