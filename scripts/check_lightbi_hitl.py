"""Explicit one-shot HITL prepare/resume, with a manual backend restart between.

No preferences are read or changed. No service is started/stopped and no model
connectivity probe is sent. Prepare creates one ambiguous normal user task;
resume sends one clarification to that same run. Network errors never retry a
write. State is marked before each POST so rerunning cannot duplicate submission.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

import httpx
import verify_history as verification

ROOT = Path(__file__).resolve().parents[1]
QUESTION = "我想看看经营情况，但还没确定看哪个指标、统计哪段时间。请先确认需要的指标和时间范围，不要自行假定。"
ANSWER = "统计2025年1月1日至12月31日已完成订单的销售额总额，按订单日期统计order_total，不扣退款；不要按渠道、月份或任何维度分组，用一个KPI展示，可以附一行结果明细，不需要额外补数。"
TERMINAL = {"completed", "failed", "cancelled", "interrupted", "waiting_for_input"}


def get(client, path, params=None):
    return client.get(path, params=params).raise_for_status().json()


def require(condition, description):
    if not condition:
        raise verification.VerificationError(description)


def ready(client):
    health = get(client, "/api/v1/health")
    require(health.get("postgres_ready") and health.get("llm_configured"), "Persisted workflow/model configuration unavailable; no request sent")
    domain = get(client, "/api/v1/domains/ecommerce")
    require(domain.get("synthetic") and str(domain.get("model_version", "")).startswith("light-bi-3.1"), "Current synthetic Light BI domain required")
    return domain


def events(client, run_id, after=0, *, max_seconds=360):
    """Read one SSE connection only, using Last-Event-ID (no reconnect retries)."""
    collected, closed, frame = [], None, {}
    started = time.monotonic()
    with client.stream("GET", f"/api/v1/runs/{quote(run_id, safe='')}/events", headers={"Last-Event-ID": str(after)}) as stream:
        stream.raise_for_status()
        for line in stream.iter_lines():
            require(time.monotonic()-started <= max_seconds, "SSE stage exceeded the explicit time budget; no automatic retry")
            if line.startswith(":"):
                continue
            if line:
                field, separator, value = line.partition(":")
                if separator and field in {"id", "event", "data"}:
                    frame.setdefault(field, []).append(value.lstrip(" "))
                continue
            if not frame:
                continue
            name = frame.get("event", ["message"])[0]
            payload = json.loads("\n".join(frame.get("data", ["{}"])))
            if name == "stream.closed":
                closed = payload.get("status")
                frame = {}
                break
            require(len(frame.get("id", [])) == 1, "Persisted SSE frame must carry one event ID")
            identifier = int(frame["id"][0])
            require(identifier > (collected[-1] if collected else after), "SSE IDs were duplicated, unordered or ignored Last-Event-ID")
            require(payload.get("event_id") == identifier and payload.get("run_id") == run_id and payload.get("type") == name,
                    "SSE payload identity differs from its frame/run")
            collected.append(identifier)
            require(len(collected) <= 5000, "SSE event budget exceeded")
            frame = {}
    require(closed in TERMINAL, "SSE disconnected without explicit waiting/terminal closure; no automatic retry")
    return {"ids": collected, "closed_status": closed, "cursor": after}


def event_ids(run):
    identifiers = [event["event_id"] for event in run.get("events", [])]
    require(identifiers == sorted(set(identifiers)), "Persisted events contain duplicate or unordered IDs")
    return identifiers


def stable_resume_stream(client, run, max_seconds):
    """Replay a strict persisted suffix; reads must not submit/restart any work."""
    identifiers = event_ids(run)
    require(bool(identifiers), "Workflow has no persisted SSE events")
    cursor = identifiers[max(0, len(identifiers)//2-1)]
    replay = events(client, run["run_id"], cursor, max_seconds=max_seconds)
    require(replay["ids"] == [identifier for identifier in identifiers if identifier > cursor], "Last-Event-ID replay did not return the exact persisted suffix")
    after = get(client, "/api/v1/runs/" + quote(run["run_id"], safe=""))
    require(after["status"] == run["status"] and event_ids(after) == identifiers
            and after.get("artifacts", {}).get("usage", {}) == run.get("artifacts", {}).get("usage", {}),
            "SSE replay unexpectedly changed workflow state, events or model usage")
    return {"cursor": cursor, "ids": replay["ids"], "usage_unchanged": True}


def check_messages(client, run, *, resumed):
    history = get(client, "/api/v1/threads/" + quote(run["thread_id"], safe=""))
    require(history["thread"]["thread_id"] == run["thread_id"], "Thread response identity mismatch")
    require([row["run_id"] for row in history["runs"]] == [run["run_id"]], "A duplicate task was created in the HITL thread")
    messages = history["messages"]
    identifiers = [message["message_id"] for message in messages]
    require(identifiers == sorted(set(identifiers)), "Thread message identities duplicated or reordered")
    require(all(message["run_id"] == run["run_id"] for message in messages), "Messages cross the dedicated run boundary")
    require(sum(message["role"] == "user" and message["content"] == QUESTION for message in messages) == 1,
            "Original user message must be saved exactly once")
    require(sum(message["role"] == "user" and message["content"] == ANSWER for message in messages) == int(resumed),
            "Clarification answer must be saved exactly once after resume and absent before")
    require(sum(message["role"] == "user" for message in messages) == (2 if resumed else 1), "Unexpected duplicate user submission")
    return identifiers


def check_starts(run, *, resumed):
    starts = [event for event in run.get("events", []) if event["type"] == "run.started"]
    require(sum(not event.get("payload", {}).get("resumed", False) for event in starts) == 1,
            "Expected exactly one initial workflow start")
    require(sum(bool(event.get("payload", {}).get("resumed")) for event in starts) == int(resumed),
            "Expected exactly one resume start, with no duplicate model submission")


def prepare(client, state, save, *, backend_pid=None, max_seconds=360):
    require(not state.get("create_attempted"), "Prepare was already attempted; no duplicate task will be submitted")
    domain = ready(client)
    state.update(phase="prepare_submitting", create_attempted=True, resume_attempted=False,
                 model_version=domain["model_version"], data_version=verification.VERSION,
                 prepare_backend_pid=backend_pid, request_counts={"create": 1, "resume": 0})
    save()
    created = client.post("/api/v1/runs", json={"domain_id": "ecommerce", "question": QUESTION}).raise_for_status().json()
    state.update(run_id=created["run_id"], thread_id=created["thread_id"])
    save()
    stream = events(client, state["run_id"], max_seconds=max_seconds)
    run = get(client, "/api/v1/runs/" + quote(state["run_id"], safe=""))
    state.update(status=run["status"], workflow_error=run.get("error"), usage=deepcopy(run.get("artifacts", {}).get("usage", {})), prepare_event_ids=stream["ids"])
    require(run["run_id"] == state["run_id"] and run["thread_id"] == state["thread_id"], "Created run/thread identity changed")
    require(run["status"] == "waiting_for_input" and stream["closed_status"] == "waiting_for_input", "Ambiguous request did not stop for clarification")
    require(stream["ids"] == event_ids(run), "Initial SSE stream omitted persisted events")
    require(any(event["type"] == "run.input_required" for event in run["events"]), "Waiting task has no clarification event")
    state["prepare_message_ids"] = check_messages(client, run, resumed=False)
    check_starts(run, resumed=False)
    state["prepare_replay"] = stable_resume_stream(client, run, max_seconds)
    state.update(phase="waiting_for_manual_restart", prepare_passed=True, last_event_id=stream["ids"][-1])
    save()
    return state


def expected_sales():
    # Independent raw observation check; never calls the application/model SQL path.
    import duckdb
    with duckdb.connect(str(ROOT / "data/ecommerce-light-bi-3-1.duckdb"), read_only=True) as connection:
        return connection.execute("SELECT SUM(order_total) FROM orders WHERE status='completed' AND order_date>=DATE '2025-01-01' AND order_date<DATE '2026-01-01'").fetchone()[0]


def check_kpi(client, run, expected):
    artifact = run["artifacts"]
    require(not artifact.get("partial"), "Resumed task produced only a partial result")
    require(all(chart.get("type") in {"kpi", "table"} for chart in artifact.get("charts", [])), "Current explicit KPI-only request was not respected")
    charts = [chart for chart in artifact.get("charts", []) if chart.get("type") == "kpi"]
    require(len(charts) == 1, "Expected exactly one annual total KPI")
    chart = charts[0]
    query = next((query for query in artifact.get("queries", []) if query["id"] == chart["query_id"]), None)
    require(query is not None and "sales" in query.get("metric_ids", []), "KPI is not associated with the formal sales metric")
    rows = verification._saved_rows(client, run["run_id"], query)
    require(len(rows) == 1 and len(chart.get("y", [])) == 1, "Annual total KPI must use one complete result row and one value")
    value = rows[0].get(chart["y"][0])
    require(type(value) in (int, float) and math.isfinite(value) and math.isclose(value, expected, abs_tol=0.0001, rel_tol=1e-12),
            "Resumed KPI amount differs from independently read 2025 completed-order total")
    return {"query_id": query["id"], "rows": 1, "actual": value, "expected": expected, "numeric_match": True}


def resume(client, state, save, *, restart_confirmed=False, backend_pid=None, max_seconds=360, sales_oracle=expected_sales):
    require(restart_confirmed, "Resume requires explicit operator confirmation of manual backend restart")
    require(state.get("prepare_passed") and state.get("phase") == "waiting_for_manual_restart" and not state.get("resume_attempted"),
            "This state is not an unresumed waiting task; refusing duplicate resume")
    if state.get("prepare_backend_pid") is not None:
        require(backend_pid is not None and backend_pid != state["prepare_backend_pid"], "Provide the changed backend PID after restart")
    domain = ready(client)
    require(domain["model_version"] == state["model_version"], "Semantic version changed during HITL restart")
    run = get(client, "/api/v1/runs/" + quote(state["run_id"], safe=""))
    require(run["status"] == "waiting_for_input" and run["thread_id"] == state["thread_id"], "Persisted waiting run/thread did not survive restart")
    require(event_ids(run) == state["prepare_event_ids"], "Waiting task was restarted or changed before explicit resume")
    require(check_messages(client, run, resumed=False) == state["prepare_message_ids"], "Waiting messages changed across restart")
    state["restart_replay"] = stable_resume_stream(client, run, max_seconds)
    state.update(phase="resume_submitting", resume_attempted=True, restart_evidence="operator_confirmed",
                 resume_backend_pid=backend_pid, request_counts={"create": 1, "resume": 1})
    save()
    accepted = client.post(f"/api/v1/runs/{quote(state['run_id'], safe='')}/resume", json={"answer": ANSWER}).raise_for_status().json()
    require(accepted["run_id"] == state["run_id"] and accepted["thread_id"] == state["thread_id"], "Resume created a different run/thread")
    stream = events(client, state["run_id"], state["last_event_id"], max_seconds=max_seconds)
    result = get(client, "/api/v1/runs/" + quote(state["run_id"], safe=""))
    state.update(status=result["status"], workflow_error=result.get("error"), usage=deepcopy(result.get("artifacts", {}).get("usage", {})), resume_event_ids=stream["ids"])
    require(result["run_id"] == state["run_id"] and result["thread_id"] == state["thread_id"], "Final task identity changed")
    require(result["status"] == "completed" and stream["closed_status"] == "completed", "Resumed task did not complete; original history retained")
    require(state["prepare_event_ids"] + stream["ids"] == event_ids(result), "Restart/resume SSE continuation lost or duplicated events")
    state["final_message_ids"] = check_messages(client, result, resumed=True)
    require(state["final_message_ids"][:len(state["prepare_message_ids"])] == state["prepare_message_ids"], "Resume replaced earlier persisted messages")
    check_starts(result, resumed=True)
    state["final_replay"] = stable_resume_stream(client, result, max_seconds)
    state["kpi"] = check_kpi(client, result, sales_oracle())
    state.update(phase="completed", passed=True, same_run_and_thread=True, original_and_answer_messages_once=True,
                 sse_ids_unique_and_ordered=True, sse_reconnect_did_not_submit_models=True,
                 text_semantic_verification="not_performed")
    save()
    return state


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", required=True, action="store_true")
    parser.add_argument("--stage", choices=["prepare", "resume"], required=True)
    parser.add_argument("--state-file", type=Path, default=ROOT / ".runtime/lightbi-hitl-state.json")
    parser.add_argument("--restart-confirmed", action="store_true")
    parser.add_argument("--backend-pid", type=int, help="Optional operator-supplied PID, must change on resume if supplied for prepare")
    parser.add_argument("--max-seconds", type=int, default=360)
    parser.add_argument("--base-url", type=verification.loopback_url, default="http://127.0.0.1:8010")
    args = parser.parse_args()
    state_file = args.state_file.resolve()
    if not state_file.is_relative_to((ROOT / ".runtime").resolve()) or not 1 <= args.max_seconds <= 600 or args.backend_pid is not None and args.backend_pid <= 0:
        parser.error("State must remain inside this project's .runtime, with valid time/PID bounds")
    state = {"kind": "lightbi_hitl_restart_check", "started_at": datetime.now(UTC).isoformat(), "passed": False}
    owned_state = owned_lock = False
    lock = state_file.with_suffix(state_file.suffix + ".lock")
    directory = ROOT / ".runtime"
    directory.mkdir(exist_ok=True)
    report_file = directory / ("lightbi-hitl-" + args.stage + "-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + uuid4().hex[:8] + ".json")
    try:
        state_file.parent.mkdir(parents=True, exist_ok=True)
        with lock.open("x", encoding="utf-8") as handle:
            handle.write(args.stage)
        owned_lock = True
        if args.stage == "prepare":
            require(not state_file.exists(), "State file already exists; previous attempt is preserved and no new run will be created")
        else:
            state = json.loads(state_file.read_text(encoding="utf-8"))
            require(state.get("kind") == "lightbi_hitl_restart_check", "Not a recognized HITL state file")
            require(args.restart_confirmed and state.get("prepare_passed") and state.get("phase") == "waiting_for_manual_restart"
                    and not state.get("resume_attempted"), "Restart confirmation and an unresumed waiting state are required; existing state preserved")
        owned_state = True
        def save():
            state_file.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        with httpx.Client(base_url=args.base_url, timeout=httpx.Timeout(35, connect=10), trust_env=False, follow_redirects=False) as client:
            if args.stage == "prepare":
                prepare(client, state, save, backend_pid=args.backend_pid, max_seconds=args.max_seconds)
            else:
                resume(client, state, save, restart_confirmed=args.restart_confirmed, backend_pid=args.backend_pid, max_seconds=args.max_seconds)
    except Exception as exc:
        state.update(passed=False, failed_stage=args.stage, error_type=type(exc).__name__)
        if isinstance(exc, verification.VerificationError):
            state["reason"] = str(exc)
        if isinstance(exc, httpx.HTTPStatusError):
            state["http_status"] = exc.response.status_code
        if state.get(args.stage.replace("prepare", "create") + "_attempted"):
            state["phase"] = args.stage + "_failed"
        if owned_state:
            state_file.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    finally:
        if owned_lock:
            lock.unlink()
        report_file.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(state, ensure_ascii=False, indent=2), flush=True)
    print("HITL report:", report_file.relative_to(ROOT), flush=True)
    if args.stage == "prepare" and state.get("phase") == "waiting_for_manual_restart":
        print("Restart the backend manually, then run --stage resume --restart-confirmed with the same state file.")
        return 0
    return 0 if state.get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
