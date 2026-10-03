"""Opt-in tests use only a dedicated DB in this project's local PostgreSQL cluster."""
import importlib.util
import sys
from pathlib import Path

import pytest


@pytest.mark.integration
def test_real_postgres_restart_memory_and_isolation(request):
    if not request.config.getoption("--integration", default=False):
        pytest.skip("Explicit --integration is required; ordinary tests never access PostgreSQL")
    project = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(project / "scripts"))
    spec = importlib.util.spec_from_file_location("insight_postgres_check", project / "scripts" / "check_postgres.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    report = module.execute_checks()
    assert report["status"] == "passed"
    assert report["database"] == "insight_agents_checks"
    assert report["check_count"] == 7
