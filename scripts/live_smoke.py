"""Bounded real API smoke tests, deliberately excluded from normal pytest and CI."""

import argparse
import json
import time
from datetime import UTC, datetime

import httpx
from dev import ROOT, runner

QUESTIONS = {
    "ecommerce": "分析2025年每个月已完成订单的销售额和毛利变化，毛利按订单明细成交额减成本计算；用折线图展示，不需要额外补数。",
    "saas": "分析2025年各月的已支付账单实收金额，注意不是MRR，用折线图展示。不需要额外补数。",
    "retail": "比较2025年各门店已完成交易的销售额，按销售额排名，用柱状图展示。不需要额外补数。",
}


async def main(args):
    report = {"timestamp": datetime.now(UTC).isoformat(), "kind": "explicit_live_smoke", "cases": []}
    async with httpx.AsyncClient(base_url=args.base_url, timeout=370) as client:
        health = (await client.get("/health")).json()
        if not health.get("postgres_ready") or not health.get("llm_configured"):
            raise RuntimeError("Real API is not ready")
        for scenario in args.scenarios:
            started = time.perf_counter()
            created = await client.post("/api/v1/runs", json={"scenario_id": scenario, "question": QUESTIONS[scenario]})
            created.raise_for_status()
            run_id = created.json()["run_id"]
            async with client.stream("GET", f"/api/v1/runs/{run_id}/events") as stream:
                stream.raise_for_status()
                seen = []
                async for line in stream.aiter_lines():
                    if line.startswith("id: "):
                        seen.append(int(line[4:]))
            response = await client.get(f"/api/v1/runs/{run_id}")
            response.raise_for_status()
            run = response.json()
            artifact = run.get("artifacts", {})
            case = {"scenario_id": scenario, "run_id": run_id, "status": run["status"], "error": run.get("error"),
                    "elapsed_seconds": round(time.perf_counter()-started, 3), "usage": artifact.get("usage", {}),
                    "query_count": len(artifact.get("queries", [])), "chart_count": len(artifact.get("charts", [])),
                    "repair_rounds": artifact.get("repair_rounds", 0), "sse_monotonic_unique": seen == sorted(set(seen)),
                    "skills_loaded": sorted({e["payload"]["skill_id"] for e in run.get("events", []) if e["type"] == "skill.loaded"}),
                    "retrieval_modes": sorted({e["payload"]["mode"] for e in run.get("events", []) if e["type"] == "memory.recalled"})}
            report["cases"].append(case)
            print(json.dumps(case, ensure_ascii=False), flush=True)
    destination = ROOT / ".runtime" / ("live-smoke-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + ".json")
    destination.parent.mkdir(exist_ok=True)
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if any(case["status"] != "completed" or not case["sse_monotonic_unique"] for case in report["cases"]):
        raise SystemExit("One or more live cases did not pass; inspect the recorded result, do not hide with replay.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:8010")
    parser.add_argument("--scenarios", nargs="+", choices=list(QUESTIONS), default=list(QUESTIONS))
    runner(main(parser.parse_args()))
