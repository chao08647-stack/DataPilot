"""Explicit bounded execution of catalog questions; no SQL/answer substitution or auto reruns."""
import argparse
import json
import sys
import time
from datetime import UTC, datetime

import httpx
from dev import ROOT


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", required=True)
    parser.add_argument("--questions", nargs="+", help="Explicit subset q01..q10; default all once, serially")
    parser.add_argument("--retry-failed", action="store_true", help="Retry selected failed/partial cases once; old attempts remain")
    parser.add_argument("--base-url", default="http://127.0.0.1:8010")
    args = parser.parse_args()
    report = {"started_at": datetime.now(UTC).isoformat(), "kind": "real_catalog_history", "synthetic_only": True, "cases": []}
    destination = ROOT / ".runtime" / ("history-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S_%fZ") + ".json")

    def save():
        destination.parent.mkdir(exist_ok=True)
        destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    with httpx.Client(base_url=args.base_url, timeout=65, trust_env=False) as client:
        health = client.get("/api/v1/health").raise_for_status().json()
        if not health.get("postgres_ready") or not health.get("llm_configured"):
            raise SystemExit("Database/model configuration unavailable; no history generated")
        questions = client.get("/api/v1/questions").raise_for_status().json()
        known = {q["id"] for q in questions}
        if args.questions and not set(args.questions).issubset(known):
            raise SystemExit("Unknown question ID; no tasks submitted")
        domains = {d["id"]: d for d in client.get("/api/v1/domains").raise_for_status().json()}
        for question in questions:
            if args.questions and question["id"] not in args.questions:
                continue
            domain = domains[question["domain_id"]]
            if not domain.get("synthetic") or not str(domain.get("model_version", "")).startswith("light-bi-3"):
                raise SystemExit("Install and verify the current synthetic Light BI template before live bootstrap")
            started = time.perf_counter()
            created = client.post("/api/v1/runs", json={"domain_id": question["domain_id"], "question": question["question"],
                "question_id": question["id"], "retry_failed": args.retry_failed}).raise_for_status().json()
            run_id, ids = created["run_id"], []
            if created["status"] in {"queued", "running"}:
                with client.stream("GET", f"/api/v1/runs/{run_id}/events", timeout=360) as stream:
                    stream.raise_for_status()
                    for line in stream.iter_lines():
                        if line.startswith("id: "):
                            ids.append(int(line[4:]))
            run = client.get(f"/api/v1/runs/{run_id}").raise_for_status().json()
            artifact = run.get("artifacts", {})
            structurally_complete = (run["status"] == "completed" and not artifact.get("partial")
                and bool(artifact.get("queries")) and any(c["type"] != "table" for c in artifact.get("charts", []))
                and any(c["type"] == "table" for c in artifact.get("charts", []))
                and all(c.get("status") == "ok" for c in artifact.get("calculations", [])))
            record = {"question_id": question["id"], "run_id": run_id, "thread_id": run["thread_id"], "domain_id": question["domain_id"],
                "model_version": run.get("model_version"), "data_version": run.get("data_version"), "reused": created.get("reused", False),
                "status": run["status"], "partial": artifact.get("partial", False), "error": run.get("error"),
                "elapsed_seconds": round(time.perf_counter()-started, 3), "usage": artifact.get("usage", {}),
                "repair_rounds": artifact.get("repair_rounds", 0), "chart_types": [c["type"] for c in artifact.get("charts", [])],
                "calculation_status": [{"tool": c["tool"], "status": c["status"], "reconciliation": c.get("reconciliation")} for c in artifact.get("calculations", [])],
                "workflow_complete": structurally_complete, "numeric_verification": "pending_independent_check",
                "sse_monotonic_unique": ids == sorted(set(ids))}
            report["cases"].append(record)
            save()
            print(json.dumps(record, ensure_ascii=False), flush=True)
            if not structurally_complete:
                # Stop at the first actual fault; the caller diagnoses before opting into any rerun.
                raise SystemExit("Case incomplete; retained history/report, no automatic retry or later submissions")
    save()
    print(f"History saved; independent numeric acceptance still required. Report: {destination.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
