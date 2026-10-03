"""Offline safety/contract checks for the relocated no-service probe."""
import importlib.util
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("check_portability", ROOT / "scripts/check_portability.py")
portability = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(portability)


def test_environment_cannot_inherit_app_or_python_path():
    env = {"INSIGHT_LLM_API_KEY": "unused", "insight_postgres_uri": "unused",
           "PYTHONPATH": "parent", "PythonHome": "parent", "PATH": "interpreter"}
    assert portability.clean_environment(env) == {"PATH": "interpreter"}


def test_cleanup_only_accepts_exact_isolated_temp_child(tmp_path):
    source = tmp_path / "workspace" / "project"
    parent = tmp_path / "temp"
    allowed = parent / "insight-portability-123"
    assert portability.checked_temporary(allowed, parent, source) == allowed
    for rejected in (parent, source, parent / "other", allowed / "nested", source / "insight-portability-123"):
        with pytest.raises(ValueError):
            portability.checked_temporary(rejected, parent, source)


@pytest.mark.parametrize("mode", ["success", "child_failure", "timeout", "copy_failure"])
def test_child_isolated_and_temporary_removed(tmp_path, monkeypatch, mode):
    root, parent = tmp_path / "workspace" / "project", tmp_path / "temp"
    root.mkdir(parents=True)
    parent.mkdir()
    monkeypatch.setattr(portability.tempfile, "gettempdir", lambda: str(parent))
    monkeypatch.setenv("INSIGHT_POSTGRES_URI", "must-not-be-read")
    monkeypatch.setenv("PYTHONPATH", "parent-app")
    copied = []

    def copy(source, target):
        copied.append(target)
        assert source == root
        if mode == "copy_failure":
            raise ValueError("fixture copy failure")

    def run(command, **kwargs):
        assert command[1:4] == ["-I", "-B", "-c"]
        assert kwargs["cwd"] == copied[0]
        assert "INSIGHT_POSTGRES_URI" not in kwargs["env"] and "PYTHONPATH" not in kwargs["env"]
        if mode == "timeout":
            raise subprocess.TimeoutExpired(command, 90)
        return SimpleNamespace(returncode=1 if mode == "child_failure" else 0,
                               stdout=json.dumps({"passed": True}), stderr="fixture child failure")

    monkeypatch.setattr(portability, "copy_assets", copy)
    monkeypatch.setattr(portability.subprocess, "run", run)
    if mode == "success":
        report = portability.run_check(root)
        assert report["temporary_directory_removed"] and report["outside_parent"]
    else:
        with pytest.raises((ValueError, RuntimeError, subprocess.TimeoutExpired)):
            portability.run_check(root)
    assert copied and not copied[0].exists()


def test_probe_checks_current_unavailable_contract_not_replay():
    compile(portability.PROBE, "<portability-probe>", "exec")
    assert 'health["mode"] == health["status"] == "unavailable"' in portability.PROBE
    assert 'response.status_code == 503' in portability.PROBE
    assert 'set(item) == {"id", "title", "question", "domain_id"}' in portability.PROBE
    assert "/examples/" not in portability.PROBE
    assert '"replay_used": False' in portability.PROBE


def test_asset_copy_excludes_configuration_runtime_and_older_data(tmp_path):
    source, target = tmp_path / "source", tmp_path / "target"
    for folder in ("backend/insight", "scenarios", "skills", "data", ".runtime"):
        (source / folder).mkdir(parents=True)
    (source / ".env").write_text("NOT_REAL_CONFIGURATION", encoding="utf-8")
    (source / ".runtime" / "private.json").write_text("{}", encoding="utf-8")
    (source / "skills" / ".env.test").write_text("NOT_REAL_CONFIGURATION", encoding="utf-8")
    (source / "data" / "old-version.duckdb").write_bytes(b"old-test-fixture")
    for domain in portability.DOMAINS:
        folder = source / "scenarios" / domain
        folder.mkdir()
        (folder / "light_bi.json").write_text(json.dumps({"id": domain, "enterprise": True,
                                                        "schema_version": "light-bi-3.1"}), encoding="utf-8")
        (source / "data" / f"{domain}-light-bi-3-1.duckdb").write_bytes(b"test-fixture")
    portability.copy_assets(source, target)
    assert not (target / ".env").exists() and not (target / ".runtime").exists()
    assert not (target / "skills" / ".env.test").exists()
    assert {path.name for path in (target / "data").iterdir()} == {
        f"{domain}-light-bi-3-1.duckdb" for domain in portability.DOMAINS}


def test_cleanup_refuses_temp_sibling_within_parent_workspace(tmp_path):
    source = tmp_path / "workspace" / "project"
    parent = source.parent
    with pytest.raises(ValueError):
        portability.checked_temporary(parent / "insight-portability-123", parent, source)
