"""Two-phase HITL HTTP/SSE protocol checks using only an in-process API fake."""
import importlib.util
import json
import sys
from copy import deepcopy
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("verify_history", ROOT / "scripts/verify_history.py")
verification = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verification)
sys.modules["verify_history"] = verification
SPEC = importlib.util.spec_from_file_location("check_lightbi_hitl", ROOT / "scripts/check_lightbi_hitl.py")
hitl = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(hitl)


class Response:
    def __init__(self, value):
        self.value = value

    def raise_for_status(self):
        return self

    def json(self):
        return deepcopy(self.value)


class Stream:
    def __init__(self, lines):
        self.lines = lines

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def raise_for_status(self):
        return self

    def iter_lines(self):
        return iter(self.lines)


class FakeAPI:
    def __init__(self):
        self.posts, self.stream_headers, self.messages = [], [], []
        self.resume_timeout = self.repeat_message = self.replay_calls_model = False
        self.ignore_cursor = self.duplicate_event = False
        self.total = 43945055.28
        self.run = {"run_id": "hitl-run", "thread_id": "hitl-thread", "origin": "user", "question_id": None,
                    "status": "queued", "model_version": "light-bi-3.1-testhash", "events": [], "artifacts": {"usage": {"calls": 0}}}

    def get(self, path, params=None):
        if path == "/api/v1/health":
            return Response({"postgres_ready": True, "llm_configured": True, "model_connectivity": "not_checked"})
        if path == "/api/v1/domains/ecommerce":
            return Response({"id": "ecommerce", "synthetic": True, "model_version": "light-bi-3.1-testhash"})
        if path == "/api/v1/runs/hitl-run":
            return Response(self.run)
        if path == "/api/v1/threads/hitl-thread":
            return Response({"thread": {"thread_id": "hitl-thread"}, "runs": [self.run], "messages": self.messages})
        if path == "/api/v1/runs/hitl-run/queries/sales-total/rows":
            return Response({"columns": ["sales"], "rows": [[self.total]], "total": 1, "truncated": False,
                             "legacy": False, "full_result_available": True})
        raise AssertionError(path)

    def event(self, identifier, kind, payload=None):
        return {"event_id": identifier, "run_id": "hitl-run", "type": kind, "payload": payload or {}}

    def message(self, identifier, role, content):
        return {"message_id": identifier, "thread_id": "hitl-thread", "run_id": "hitl-run", "role": role, "content": content}

    def post(self, path, json):
        self.posts.append((path, deepcopy(json)))
        if path == "/api/v1/runs":
            assert json == {"domain_id": "ecommerce", "question": hitl.QUESTION}
            self.run.update(status="waiting_for_input", events=[self.event(2, "run.started", {"resumed": False}),
                self.event(4, "node.completed"), self.event(7, "run.input_required")], artifacts={"usage": {"calls": 1}})
            self.messages = [self.message(1, "user", hitl.QUESTION), self.message(2, "assistant", "请明确指标和时间")]
        elif path == "/api/v1/runs/hitl-run/resume":
            if self.resume_timeout:
                raise httpx.ReadTimeout("synthetic timeout")
            assert json == {"answer": hitl.ANSWER}
            self.run["events"].extend([self.event(11, "run.started", {"resumed": True}), self.event(13, "artifact.created"), self.event(19, "run.completed")])
            self.run.update(status="completed", artifacts={"partial": False, "usage": {"calls": 8},
                "queries": [{"id": "sales-total", "metric_ids": ["sales"], "columns": ["sales"], "rows": [[self.total]], "total_rows": 1,
                             "result_ref": {"run_id": "hitl-run", "query_id": "sales-total"}}],
                "charts": [{"type": "kpi", "query_id": "sales-total", "y": ["sales"]}]})
            self.messages.extend([self.message(3, "user", hitl.ANSWER), self.message(4, "assistant", "销售总额已核对")])
            if self.repeat_message:
                self.messages.append(self.message(5, "user", hitl.ANSWER))
        else:
            raise AssertionError("No preference/model/other write allowed: " + path)
        return Response(self.run)

    def stream(self, method, path, headers):
        assert method == "GET" and path == "/api/v1/runs/hitl-run/events"
        self.stream_headers.append(deepcopy(headers))
        cursor = int(headers["Last-Event-ID"])
        selected = [event for event in self.run["events"] if self.ignore_cursor or event["event_id"] > cursor]
        if self.duplicate_event and selected:
            selected.append(selected[-1])
        if cursor and self.replay_calls_model:
            self.run["artifacts"]["usage"]["calls"] += 1
        lines = [": heartbeat", ""]
        for event in selected:
            lines.extend([f"id: {event['event_id']}", f"event: {event['type']}", "data: " + json.dumps(event), ""])
        lines.extend(["event: stream.closed", "data: " + json.dumps({"status": self.run["status"]}), ""])
        return Stream(lines)


def waiting():
    api, state, saved = FakeAPI(), {}, []
    hitl.prepare(api, state, lambda: saved.append(deepcopy(state)), backend_pid=100)
    assert state["phase"] == "waiting_for_manual_restart"
    return api, state, saved


def test_manual_restart_two_stage_same_run_messages_once_and_exact_sse_suffix():
    api, state, saved = waiting()
    # Simulate process exit: reconstruct from durable JSON rather than shared state.
    restored = json.loads(json.dumps(saved[-1]))
    hitl.resume(api, restored, lambda: None, restart_confirmed=True, backend_pid=200, sales_oracle=lambda: 43945055.28)
    assert restored["passed"] and restored["same_run_and_thread"]
    assert restored["prepare_event_ids"] + restored["resume_event_ids"] == [2, 4, 7, 11, 13, 19]
    assert restored["request_counts"] == {"create": 1, "resume": 1}
    assert [path for path, _ in api.posts] == ["/api/v1/runs", "/api/v1/runs/hitl-run/resume"]
    assert any(header["Last-Event-ID"] == "7" for header in api.stream_headers)
    assert restored["restart_evidence"] == "operator_confirmed"
    assert restored["final_replay"]["usage_unchanged"]


def test_attempt_marker_is_saved_before_each_write_and_blocks_double_submission():
    api, state, saved = waiting()
    assert saved[0]["create_attempted"] and "run_id" not in saved[0]
    with pytest.raises(verification.VerificationError, match="already attempted"):
        hitl.prepare(api, state, lambda: None)
    hitl.resume(api, state, lambda: saved.append(deepcopy(state)), restart_confirmed=True, backend_pid=200, sales_oracle=lambda: 43945055.28)
    marker = next(row for row in saved if row.get("phase") == "resume_submitting")
    assert marker["resume_attempted"] and marker["request_counts"]["resume"] == 1
    with pytest.raises(verification.VerificationError, match="duplicate resume"):
        hitl.resume(api, state, lambda: None, restart_confirmed=True, backend_pid=200)
    assert len(api.posts) == 2


def test_network_failure_on_resume_is_not_retried_or_submitted_twice():
    api, state, saved = waiting()
    api.resume_timeout = True
    with pytest.raises(httpx.ReadTimeout):
        hitl.resume(api, state, lambda: saved.append(deepcopy(state)), restart_confirmed=True, backend_pid=200)
    assert saved[-1]["resume_attempted"] and saved[-1]["phase"] == "resume_submitting"
    with pytest.raises(verification.VerificationError, match="duplicate resume"):
        hitl.resume(api, state, lambda: None, restart_confirmed=True, backend_pid=200)
    assert len(api.posts) == 2


@pytest.mark.parametrize("kwargs", [{}, {"restart_confirmed": True}, {"restart_confirmed": True, "backend_pid": 100}])
def test_resume_requires_human_restart_and_changed_supplied_pid(kwargs):
    api, state, _ = waiting()
    with pytest.raises(verification.VerificationError):
        hitl.resume(api, state, lambda: None, **kwargs)
    assert len(api.posts) == 1


@pytest.mark.parametrize("fault", ["ignore_cursor", "duplicate_event", "replay_calls_model"])
def test_sse_reconnect_must_not_repeat_ids_ignore_cursor_or_trigger_models(fault):
    api, state, _ = waiting()
    setattr(api, fault, True)
    with pytest.raises(verification.VerificationError):
        hitl.resume(api, state, lambda: None, restart_confirmed=True, backend_pid=200)
    assert len(api.posts) == 1


def test_duplicate_clarification_message_fails_acceptance():
    api, state, _ = waiting()
    api.repeat_message = True
    with pytest.raises(verification.VerificationError, match="exactly once"):
        hitl.resume(api, state, lambda: None, restart_confirmed=True, backend_pid=200, sales_oracle=lambda: 43945055.28)


def test_wrong_kpi_total_does_not_pass_just_because_task_completed():
    api, state, _ = waiting()
    api.total = 1.
    with pytest.raises(verification.VerificationError, match="amount differs"):
        hitl.resume(api, state, lambda: None, restart_confirmed=True, backend_pid=200, sales_oracle=lambda: 43945055.28)
