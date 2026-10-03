"""Non-mutating native prerequisite check; does not call models or print settings."""
import importlib.metadata
import json
import shutil
import subprocess
import sys

from dev import ROOT


def main():
    packages = {}
    for name in ("fastapi", "langgraph", "langgraph-checkpoint-postgres", "psycopg", "duckdb", "sqlglot"):
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = "missing: install backend dependencies"
    node = shutil.which("node")
    node_version = subprocess.run([node, "--version"], capture_output=True, text=True).stdout.strip() if node else "missing"
    print(json.dumps({"python": sys.version.split()[0], "python_supported": sys.version_info[:2] == (3, 12), "node": node_version,
        "packages": packages, "postgres_tools_on_path": bool(shutil.which("pg_dump")), "enabled_connectors": ["postgres", "duckdb"],
        "project_local_postgres_initialized": (ROOT / ".runtime" / "postgres" / "PG_VERSION").is_file(), "model_requests": 0, "ports": {"backend": 8010, "frontend": 5174, "application_postgres": 15432}}, indent=2))


if __name__ == "__main__":
    main()
