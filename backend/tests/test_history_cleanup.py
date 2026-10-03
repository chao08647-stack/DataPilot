"""Offline guards for the explicitly invoked administrative cleanup script."""
import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("history_cleanup", SCRIPTS / "cleanup_history.py")
cleanup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cleanup)


def run(id, status="completed", partial=False, thread=None):
    return {"run_id": id, "thread_id": thread or id, "status": status, "artifacts": {"partial": partial}}


def test_only_failed_and_explicit_partial_are_selected():
    plan = cleanup.cleanup_plan([run("ok"), run("bad", "failed"), run("partial", partial=True),
                                 run("cancelled", "cancelled"), run("interrupted", "interrupted")])
    assert plan["target_ids"] == ["bad", "partial"]
    assert plan["retained"] == 3
    assert plan["failed"] == plan["partial"] == 1


@pytest.mark.parametrize("status", sorted(cleanup.ACTIVE))
def test_active_tasks_block_cleanup(status):
    with pytest.raises(ValueError, match="Active tasks"):
        cleanup.cleanup_plan([run("failed", "failed"), run("active", status)])


def test_shared_successful_thread_never_becomes_checkpoint_deletion_target():
    plan = cleanup.cleanup_plan([run("ok", thread="shared"), run("bad", "failed", thread="shared")])
    assert plan["shared_threads"] == ["shared"]
    assert plan["thread_ids"] == []


def test_fingerprint_detects_changes_not_just_same_count():
    first = cleanup.cleanup_plan([run("one", "failed"), run("ok")])
    second = cleanup.cleanup_plan([run("two", "failed"), run("ok")])
    assert first["target_fingerprint"] != second["target_fingerprint"]
    assert first["retained_fingerprint"] == second["retained_fingerprint"]
