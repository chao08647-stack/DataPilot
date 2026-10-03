"""Check relocated assets and honest unavailable mode, without model/DB connections."""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PREFIX = "insight-portability-"
DOMAINS = {"ecommerce", "saas", "retail"}

# Isolated Python ignores inherited Python paths; only the relocated app is inserted.
PROBE = r'''
import json
import os
import socket
import sys
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

root = Path.cwd().resolve()
assert not any(key.upper().startswith("INSIGHT_") for key in os.environ)
assert not os.environ.get("PYTHONPATH")
assert not (root / ".env").exists() and not (root / ".runtime").exists()
sys.path.insert(0, str(root / "backend"))
from fastapi.testclient import TestClient
from insight.config import PROJECT_ROOT, Settings
from insight.main import create_app

config = Settings(_env_file=None)
assert PROJECT_ROOT == root and config.project_root == root
assert config.data_dir == root / "data"
assert not config.postgres_uri.get_secret_value()
assert not config.llm_configured and not config.embedding_base_url

def deny(*args, **kwargs):
    raise AssertionError("Portability probe must not connect to a database/model")

attempts, internal_socketpairs = [], []
def audit(event, args):
    # Windows asyncio implements its private wakeup socketpair via loopback TCP.
    fallback = getattr(socket, "_fallback_socketpair", None)
    if (event == "socket.connect" and fallback is not None
            and sys._getframe(1).f_code is fallback.__code__
            and args[1][0] in {"127.0.0.1", "::1"}):
        internal_socketpairs.append(True)
        return
    if event in {"socket.connect", "socket.getaddrinfo"}:
        attempts.append(event)
        raise AssertionError("External network access is forbidden")
sys.addaudithook(audit)

with ExitStack() as stack:
    guards = [stack.enter_context(patch(name, side_effect=deny)) for name in (
        "insight.providers.ModelClient.complete", "insight.providers.Embedder.embed",
        "psycopg.connect", "psycopg.Connection.connect", "psycopg.AsyncConnection.connect",
    )]
    with TestClient(create_app(config)) as client:
        health = client.get("/health").raise_for_status().json()
        assert health["mode"] == health["status"] == "unavailable"
        assert not health["postgres_ready"] and not health["llm_configured"]
        assert not health["embedding_configured"]
        assert health["model_connectivity"] == "not_checked"
        templates = client.get("/api/v1/templates").raise_for_status().json()
        assert {item["id"] for item in templates} == {"ecommerce", "saas", "retail"}
        assert len(templates) == 3 and all(item["synthetic"] for item in templates)
        questions = client.get("/api/v1/questions").raise_for_status().json()
        assert len(questions) == 10
        assert {item["id"] for item in questions} == {f"q{i:02}" for i in range(1, 11)}
        assert all(set(item) == {"id", "title", "question", "domain_id"} for item in questions)
        assert all(all(isinstance(value, str) and value for value in item.values()) for item in questions)
        for domain, count in (("ecommerce", 5), ("saas", 3), ("retail", 2)):
            selected = client.get("/api/v1/questions", params={"domain_id": domain}).raise_for_status().json()
            assert selected == [item for item in questions if item["domain_id"] == domain]
            assert len(selected) == count
        assert client.post("/api/v1/questions", json={}).status_code == 405
        response = client.post("/api/v1/runs", json={"domain_id": "ecommerce", "question": "2025年销售额总额"})
        assert response.status_code == 503
        assert client.get("/api/v1/runs").status_code == 503
        runtime = client.app.state.runtime
        assert runtime.repository is None and runtime.graph is None and not runtime.tasks
        assert runtime.seed_report == []
        assert set(runtime.paths) == {"ecommerce", "saas", "retail"}
        assert all(path.resolve().is_relative_to(root / "data") for path in runtime.paths.values())
    assert not any(guard.called for guard in guards)
assert not attempts
for name, module in tuple(sys.modules.items()):
    if name == "insight" or name.startswith("insight."):
        assert Path(module.__file__).resolve().is_relative_to(root / "backend")
print(json.dumps({"passed": True, "project_root_relocated": True, "templates": 3,
    "questions": 10, "catalog_fields_only": True, "catalog_readonly": True,
    "health_mode": "unavailable", "unconfigured_run_status": 503,
    "models_called": 0, "postgres_connections": 0, "external_connections": 0,
    "replay_used": False, "application_ports_listened": 0,
    "stdlib_internal_socketpairs": len(internal_socketpairs)}))
'''


def clean_environment(environment):
    return {key: value for key, value in environment.items()
            if not key.upper().startswith("INSIGHT_")
            and key.upper() not in {"PYTHONPATH", "PYTHONHOME"}}


def checked_temporary(path, parent, source):
    """Resolve exact mkdtemp child before copying or recursively deleting it."""
    path, parent, source = Path(path), Path(parent).resolve(), Path(source).resolve()
    if path.is_symlink() or path.is_junction():
        raise ValueError("Refusing a linked temporary root")
    target = path.resolve()
    if (target.parent != parent or not target.name.startswith(PREFIX)
            or target.is_relative_to(source.parent) or source.is_relative_to(target)):
        raise ValueError("Temporary target must be a dedicated child outside the parent workspace")
    return target


def copy_assets(source, target):
    source, target = Path(source).resolve(), Path(target).resolve()
    for folder in ("backend/insight", "scenarios", "skills"):
        origin = source / folder
        if any(path.is_symlink() or path.is_junction() for path in (origin, *origin.rglob("*"))):
            raise ValueError("Linked assets are not allowed in a standalone copy")
        shutil.copytree(origin, target / folder, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".env", ".env.*"))
    (target / "data").mkdir()
    for domain in sorted(DOMAINS):
        definition = json.loads((source / "scenarios" / domain / "light_bi.json").read_text(encoding="utf-8"))
        if definition.get("id") != domain or not definition.get("enterprise"):
            raise ValueError("Expected current independent synthetic template")
        version = re.sub(r"[^a-zA-Z0-9_-]", "-", str(definition["schema_version"]))
        name = f"{domain}-{version}.duckdb"
        origin = source / "data" / name
        if (not origin.is_file() or origin.is_symlink()
                or not origin.resolve().is_relative_to(source / "data")
                or origin.with_suffix(".duckdb.wal").exists()):
            raise ValueError("Current synthetic data must be generated and quiescent; run scripts/generate_data.py first")
        shutil.copy2(origin, target / "data" / name)


def run_check(root=ROOT):
    root = Path(root).resolve()
    parent = Path(tempfile.gettempdir()).resolve()
    temporary = Path(tempfile.mkdtemp(prefix=PREFIX, dir=parent))
    target = checked_temporary(temporary, parent, root)
    report = None
    try:
        copy_assets(root, target)
        result = subprocess.run([sys.executable, "-I", "-B", "-c", PROBE], cwd=target,
                                env=clean_environment(os.environ), text=True, encoding="utf-8",
                                capture_output=True, timeout=90, check=False)
        if result.returncode:
            raise RuntimeError("Relocated no-service check failed: " + result.stderr[-2500:])
        report = json.loads(result.stdout.strip().splitlines()[-1])
        if report.get("passed") is not True:
            raise RuntimeError("Relocated probe did not return a passing report")
        report.update(outside_parent=True, checked_at=datetime.now(UTC).isoformat(),
                      scope="Relocated local backend/assets and unavailable-mode API only; not model, PostgreSQL, frontend or full deployment verification")
    finally:
        # Never delete a computed broad path or the source; recheck after child exit.
        checked_temporary(temporary, parent, root)
        shutil.rmtree(target)
    report["temporary_directory_removed"] = not target.exists()
    return report


def main():
    report = run_check()
    directory = ROOT / ".runtime"
    directory.mkdir(exist_ok=True)
    (directory / "portability.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
