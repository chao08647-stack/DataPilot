"""Explicit HTTP verification for dashboards and optionally synthetic live-model diagnostics."""
import argparse
import json
import sys
import time
from datetime import UTC, datetime

import httpx
from dev import ROOT

QUESTIONS = {
    "profit": ("ecommerce", "比较2025年9月和11月贡献利润，按标价成交额、折扣、成功退款、成交成本、履约费用、支付费用和营销支出拆解变化，核对各项是否等于整体变化。不要把账面贡献解释成因果。"),
    "refund": ("ecommerce", "分析2025年11月已成熟订单的退款与履约关系，按供应商和退款原因定位问题，比较延迟与正常签收订单的退款率，明确只能说明关联。"),
    "funnel": ("saas", "比较2025年9月与11月成熟注册账户的注册、激活、试用和付费漏斗，区分获客渠道结构变化与同渠道转化变化，用确定性工具计算。"),
    "mrr": ("saas", "核对2025年9月末与12月末的MRR变动，分为新增、扩张、收缩、流失、恢复，不要把账单收款当MRR；结合实际查询给出收入桥接图。"),
    "store": ("retail", "比较2025年9月与11月相同门店的经营表现，按营业门店天数、日均客流、客流成交比和客单价分解销售变化，定位需要关注的门店，不推断未经证明的因果。"),
    "inventory": ("retail", "检查2025年11月门店商品库存，用期初库存、入库、销售、调入调出和调整核对期末库存，区分缺失快照和缺货，找出采购或调拨逾期线索。"),
}


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--integration", action="store_true", required=True)
    parser.add_argument("--live-models", action="store_true", help="Explicitly authorize bounded synthetic model calls")
    parser.add_argument("--cases", nargs="+", choices=list(QUESTIONS), default=list(QUESTIONS))
    parser.add_argument("--base-url", default="http://127.0.0.1:8010")
    parser.add_argument("--keep-boards", action="store_true", help="Retain only boards created by this verification for visual inspection")
    parser.add_argument("--boards-only", action="store_true")
    parser.add_argument("--skip-boards", action="store_true", help="Reuse already recorded dashboard checks during a targeted live diagnostic")
    args = parser.parse_args()
    report = {"time": datetime.now(UTC).isoformat(), "boards": [], "cases": [], "synthetic_only": True}
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    report_path = ROOT / ".runtime" / f"enterprise-check-{stamp}.json"

    def save():
        report_path.parent.mkdir(exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    with httpx.Client(base_url=args.base_url, timeout=65, trust_env=False) as client:
        health = client.get("/health")
        health.raise_for_status()
        if not health.json().get("postgres_ready"):
            raise SystemExit("Dedicated application PostgreSQL is not ready")
        domains = client.get("/api/v1/domains").json()
        for domain in ([] if args.skip_boards else domains):
            if not domain.get("synthetic"):
                continue
            for template in domain.get("dashboard_templates", []):
                response = client.post("/api/v1/dashboards", json={"domain_id": domain["id"], "title": "验收 · " + template["title"], "template_id": template["id"]})
                response.raise_for_status()
                board = response.json()
                before = len(client.get("/api/v1/threads").json())
                refreshed = client.post(f"/api/v1/dashboards/{board['id']}/refresh", json={})
                refreshed.raise_for_status()
                board = refreshed.json()
                cards = [{"title": c["title"], "status": (c.get("snapshot") or {}).get("status"), "error": (c.get("snapshot") or {}).get("error"), "rows": len((c.get("snapshot") or {}).get("query", {}).get("rows", []))} for c in board["cards"]]
                report["boards"].append({"domain_id": domain["id"], "board_id": board["id"], "cards": cards, "no_new_threads": before == len(client.get("/api/v1/threads").json())})
                save()
                print(json.dumps(report["boards"][-1], ensure_ascii=False), flush=True)
                if not args.keep_boards:
                    client.delete(f"/api/v1/dashboards/{board['id']}").raise_for_status()
        if args.live_models and not args.boards_only:
            for name in args.cases:
                domain_id, question = QUESTIONS[name]
                if not any(d["id"] == domain_id and d.get("synthetic") for d in domains):
                    raise SystemExit("Live diagnostics may use installed synthetic templates only")
                started = time.perf_counter()
                created = client.post("/api/v1/runs", json={"domain_id": domain_id, "question": question})
                created.raise_for_status()
                run_id = created.json()["run_id"]
                # Waiting for SSE is bounded; failures are saved, not automatically rerun.
                cursor_ids = []
                try:
                    with client.stream("GET", f"/api/v1/runs/{run_id}/events", timeout=330) as stream:
                        stream.raise_for_status()
                        for line in stream.iter_lines():
                            if line.startswith("id: "):
                                cursor_ids.append(int(line[4:]))
                finally:
                    result = client.get(f"/api/v1/runs/{run_id}")
                    result.raise_for_status()
                    run = result.json()
                    artifact = run.get("artifacts", {})
                    record = {"case": name, "domain_id": domain_id, "run_id": run_id, "status": run["status"], "error": run.get("error"), "partial": artifact.get("partial", False),
                        "elapsed_seconds": round(time.perf_counter()-started, 3), "usage": artifact.get("usage", {}), "query_count": len(artifact.get("queries", [])),
                        "calculations": [{k: c.get(k) for k in ("tool", "status", "reconciliation", "limitations")} for c in artifact.get("calculations", [])],
                        "sse_monotonic_unique": cursor_ids == sorted(set(cursor_ids)), "repair_rounds": artifact.get("repair_rounds", 0),
                        "tool_events": [e["payload"] for e in run.get("events", []) if e["type"] == "analysis.tool_used"]}
                    report["cases"].append(record)
                    save()
                    print(json.dumps(record, ensure_ascii=False), flush=True)
    save()
    if any(c["status"] != "ready" for b in report["boards"] for c in b["cards"]) or any(c["status"] != "completed" or c["partial"] or not c["calculations"] or any(x["status"] != "ok" for x in c["calculations"]) for c in report["cases"]):
        raise SystemExit("Some cases did not pass; inspect the retained report. No automatic retry.")


if __name__ == "__main__":
    main()
