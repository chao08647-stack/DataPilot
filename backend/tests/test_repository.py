from concurrent.futures import ThreadPoolExecutor

import pytest

from insight.repository import InMemoryRepository


@pytest.fixture
def repo():
    return InMemoryRepository()


def test_thread_scope_and_active_lock(repo):
    run = repo.create_run("ecommerce", "query", "thread-a")
    with pytest.raises(ValueError, match="unfinished"):
        repo.create_run("ecommerce", "another", "thread-a")
    repo.update_run(run["run_id"], status="waiting_for_input")
    with pytest.raises(ValueError, match="unfinished"):
        repo.create_run("ecommerce", "another", "thread-a")
    repo.update_run(run["run_id"], status="completed")
    with pytest.raises(ValueError, match="scenario"):
        repo.create_run("saas", "another", "thread-a")
    second = repo.create_run("ecommerce", "another", "thread-a")
    assert second["run_id"] != run["run_id"]


def test_concurrent_requests_only_one_wins(repo):
    def attempt(_):
        try:
            return repo.create_run("retail", "question", "shared")["run_id"]
        except ValueError:
            return None
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(attempt, range(16)))
    assert sum(result is not None for result in results) == 1


def test_messages_and_events_idempotent_monotonic_and_isolated(repo):
    run = repo.create_run("saas", "first")
    other = repo.create_run("retail", "second")
    thread_id, run_id = run["thread_id"], run["run_id"]
    a = repo.add_message(thread_id, "assistant", "need clarification", run_id, key="ask")
    b = repo.add_message(thread_id, "assistant", "do not duplicate", run_id, key="ask")
    assert a == b
    assert len(repo.messages(thread_id)) == 2
    with pytest.raises(ValueError, match="thread"):
        repo.add_message(other["thread_id"], "user", "wrong", run_id)
    first = repo.event(run_id, "run.started", {}, key="start")
    assert repo.event(run_id, "run.started", {"ignored": True}, key="start") == first
    repo.event(other["run_id"], "run.started", {})
    last = repo.event(run_id, "node.started", {"node": "sql"})
    assert last["event_id"] > first["event_id"]
    assert repo.events(run_id, after=first["event_id"]) == [last]


def test_summary_cursor_never_regresses_and_raw_messages_retained(repo):
    run = repo.create_run("saas", "one")
    thread_id, run_id = run["thread_id"], run["run_id"]
    first = repo.messages(thread_id)[0]
    last = repo.add_message(thread_id, "assistant", "two", run_id)
    repo.save_summary(thread_id, "new", last["message_id"])
    repo.save_summary(thread_id, "stale", first["message_id"])
    assert repo.get_summary(thread_id) == {"text": "new", "upto_id": last["message_id"]}
    assert len(repo.messages(thread_id)) == 2
    with pytest.raises(ValueError, match="cursor"):
        repo.save_summary(thread_id, "invalid", 500)


def test_restart_preserves_waiting_but_interrupts_executing(repo):
    queued = repo.create_run("retail", "queued")
    running = repo.create_run("saas", "running")
    waiting = repo.create_run("ecommerce", "waiting")
    repo.update_run(running["run_id"], status="running")
    repo.update_run(waiting["run_id"], status="waiting_for_input")
    assert repo.recover_interrupted() == 2
    assert repo.get_run(queued["run_id"])["status"] == "interrupted"
    assert repo.get_run(running["run_id"])["status"] == "interrupted"
    assert repo.get_run(waiting["run_id"])["status"] == "waiting_for_input"
    assert repo.recover_interrupted() == 0
    assert repo.resume_run(waiting["run_id"])["status"] == "running"
    with pytest.raises(ValueError, match="not waiting"):
        repo.resume_run(waiting["run_id"])


def test_defensive_copies_and_thread_detail(repo):
    run = repo.create_run("retail", "query")
    run["artifacts"]["poison"] = True
    assert repo.get_run(run["run_id"])["artifacts"] == {}
    detail = repo.thread_detail(run["thread_id"])
    assert detail["thread"]["scenario_id"] == "retail"
    assert detail["messages"][0]["content"] == "query"
    assert len(detail["runs"]) == 1
    assert len(repo.list_threads("retail")) == 1
    assert repo.list_threads("saas") == []
    with pytest.raises(ValueError):
        repo.update_run(run["run_id"], arbitrary_column="bad")
    with pytest.raises(ValueError):
        repo.update_run(run["run_id"], status="waiting")
