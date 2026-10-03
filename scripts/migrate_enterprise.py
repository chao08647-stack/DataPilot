"""Explicit backup + additive enterprise migration; never upgrades an active task database."""
import argparse
import hashlib
import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from dev import ROOT, common_arguments, configure


def main():
    parser = argparse.ArgumentParser()
    common_arguments(parser)
    parser.add_argument("--bin-dir", required=True, type=Path)
    args = parser.parse_args()
    configure(args)
    from psycopg.conninfo import conninfo_to_dict

    from insight.config import Settings
    from insight.enterprise import ResourceStore
    from insight.repository import Repository
    repo = Repository(Settings().postgres_uri.get_secret_value())
    with repo.connect() as conn:
        conn.execute("SELECT pg_advisory_lock(194801002)")
        if conn.execute("SELECT count(*) AS n FROM ia_runs WHERE status IN ('queued','running','waiting_for_input')").fetchone()["n"]:
            raise SystemExit("Migration blocked: finish/cancel active tasks using the existing app first.")
        params = conninfo_to_dict(repo.dsn)
        backup_dir = ROOT / ".runtime" / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S_%fZ")
        target = backup_dir / f"insight-{stamp}.dump"
        env = {**os.environ, "PGHOST": params.get("host", "127.0.0.1"), "PGPORT": params.get("port", "15432"), "PGUSER": params["user"], "PGDATABASE": params["dbname"], "PGPASSWORD": params.get("password", "")}
        suffix = ".exe" if os.name == "nt" else ""
        for command in ([str(args.bin_dir / f"pg_dump{suffix}"), "--format=custom", "--file", str(target)],
                        [str(args.bin_dir / f"pg_restore{suffix}"), "--list", str(target)]):
            result = subprocess.run(command, env=env, capture_output=True, timeout=60)
            if result.returncode:
                raise SystemExit("Backup verification failed; migration was not applied. Check local PostgreSQL tools.")
        # Release the active-run SELECT's table lock before ALTER on a new connection.
        # The session advisory lock still excludes concurrent migrations.
        conn.commit()
        repo.setup(migration=True)
        ResourceStore(repo).setup(migration=True)
        record = {"database": params["dbname"], "backup": str(target.relative_to(ROOT)), "sha256": hashlib.sha256(target.read_bytes()).hexdigest(), "verified_at": datetime.now(UTC).isoformat(), "migration": "light-bi-history-results-v1"}
        (backup_dir / f"insight-{stamp}.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
        print(json.dumps(record, indent=2))


if __name__ == "__main__":
    main()
