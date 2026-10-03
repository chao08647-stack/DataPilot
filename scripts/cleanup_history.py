"""Explicit local history cleanup. Dry-run first; verified backup before deletion.

No model clients, workflow runtime or business database are opened by this script.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from dev import ROOT, configure

ACTIVE = {"queued", "pending", "running", "waiting_for_input"}
CHILDREN = ("ia_messages", "ia_events", "ia_query_results")
CHECKPOINTS = ("checkpoint_writes", "checkpoint_blobs", "checkpoints")


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str, ensure_ascii=False).encode()).hexdigest()


def cleanup_plan(rows):
    if any(row["status"] in ACTIVE for row in rows):
        raise ValueError("Active tasks exist; finish them before cleanup.")
    targets = sorted((r for r in rows if r["status"] == "failed" or
                      (r["status"] == "completed" and r.get("artifacts", {}).get("partial") is True)),
                     key=lambda r: r["run_id"])
    ids = {r["run_id"] for r in targets}
    retained = sorted((r for r in rows if r["run_id"] not in ids), key=lambda r: r["run_id"])
    threads = {r["thread_id"] for r in targets}
    shared = threads & {r["thread_id"] for r in retained}
    return {
        "target_ids": sorted(ids), "thread_ids": sorted(threads - shared),
        "shared_threads": sorted(shared), "failed": sum(r["status"] == "failed" for r in targets),
        "partial": sum(r["status"] == "completed" for r in targets), "retained": len(retained),
        "target_fingerprint": fingerprint(targets), "retained_fingerprint": fingerprint(retained),
    }


def file_hash(path):
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def verify_local(conn):
    row = conn.execute("SELECT current_database() AS db, inet_server_port() AS port").fetchone()
    directory = Path(conn.execute("SHOW data_directory").fetchone()["data_directory"]).resolve()
    if row != {"db": "insight_agents", "port": 15432} or directory != (ROOT / ".runtime/postgres").resolve():
        raise ValueError("Refusing cleanup outside this project's dedicated local PostgreSQL.")


def backup(dsn, bin_dir, destination):
    from psycopg.conninfo import conninfo_to_dict

    params = conninfo_to_dict(dsn)
    env = {**os.environ, "PGHOST": "127.0.0.1", "PGPORT": "15432", "PGUSER": params["user"],
           "PGDATABASE": "insight_agents", "PGPASSWORD": params.get("password", "")}
    suffix = ".exe" if os.name == "nt" else ""
    for command in ([str(bin_dir / f"pg_dump{suffix}"), "--format=custom", "--file", str(destination)],
                    [str(bin_dir / f"pg_restore{suffix}"), "--list", str(destination)]):
        result = subprocess.run(command, env=env, capture_output=True, timeout=60,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        if result.returncode:
            raise RuntimeError("Backup failed verification; nothing deleted. Check local PostgreSQL tools.")
    return file_hash(destination)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local-postgres", action="store_true", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-count", type=int)
    parser.add_argument("--expected-fingerprint")
    parser.add_argument("--bin-dir", type=Path)
    args = parser.parse_args()
    configure(args)  # Only the new project's local database credentials; no model injection.
    from psycopg import sql

    from insight.config import Settings
    from insight.repository import Repository

    repo = Repository(Settings().postgres_uri.get_secret_value())
    manifest = None
    with repo.connect() as conn:
        verify_local(conn)
        if args.apply:
            conn.execute("SET LOCAL lock_timeout='5s'")
            # Blocks history writes during audit/backup/delete, but permits pg_dump and UI reads.
            names = ("ia_threads", "ia_runs", *CHILDREN, *CHECKPOINTS)
            conn.execute(sql.SQL("LOCK TABLE {} IN SHARE ROW EXCLUSIVE MODE").format(
                sql.SQL(", ").join(sql.Identifier(name) for name in names)))
        rows = conn.execute("SELECT * FROM ia_runs ORDER BY run_id").fetchall()
        plan = cleanup_plan(rows)
        if not args.apply:
            print(json.dumps({"mode": "dry_run", **plan}, indent=2))
            return
        if (not args.bin_dir or args.expected_count != len(plan["target_ids"]) or
                args.expected_fingerprint != plan["target_fingerprint"]):
            raise ValueError("Apply requires pg tools, exact audited count and target fingerprint.")
        if plan["shared_threads"]:
            raise ValueError("Target shares a thread with retained runs; manual checkpoint review required.")
        if not plan["target_ids"]:
            print(json.dumps({"deleted": 0, "retained": plan["retained"]}))
            return
        # Avoid silently leaving a saved card pointing at a removed query.
        resources_before = conn.execute("SELECT * FROM ia_resources ORDER BY kind,id").fetchall()
        for resource in resources_before:
            if resource["kind"] == "dashboard" and any(
                card.get("provenance", {}).get("run_id") in plan["target_ids"]
                for card in resource["payload"].get("cards", [])
            ):
                raise ValueError("A saved dashboard references a target; review before cleanup.")
        directory = ROOT / ".runtime/backups"
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S_%fZ")
        dump = directory / f"history-cleanup-{stamp}.dump"
        digest = backup(repo.dsn, args.bin_dir.resolve(), dump)
        manifest = dump.with_suffix(".json")
        report = {"backup": str(dump.relative_to(ROOT)), "sha256": digest, "plan": plan,
                  "created_at": datetime.now(UTC).isoformat(), "status": "backup_verified_before_delete"}
        manifest.write_text(json.dumps(report, indent=2), encoding="utf-8")
        counts = {}
        for table in CHILDREN:
            counts[table] = conn.execute(sql.SQL("DELETE FROM {} WHERE run_id = ANY(%s)").format(
                sql.Identifier(table)), (plan["target_ids"],)).rowcount
        counts["ia_runs"] = conn.execute("DELETE FROM ia_runs WHERE run_id = ANY(%s)", (plan["target_ids"],)).rowcount
        for table in CHECKPOINTS:
            counts[table] = conn.execute(sql.SQL("DELETE FROM {} WHERE thread_id = ANY(%s)").format(
                sql.Identifier(table)), (plan["thread_ids"],)).rowcount
        counts["ia_threads"] = conn.execute("DELETE FROM ia_threads WHERE thread_id = ANY(%s)", (plan["thread_ids"],)).rowcount
        remaining = conn.execute("SELECT * FROM ia_runs ORDER BY run_id").fetchall()
        if counts["ia_runs"] != len(plan["target_ids"]) or fingerprint(remaining) != plan["retained_fingerprint"]:
            raise RuntimeError("Retained run verification failed; transaction rolled back.")
        if resources_before != conn.execute("SELECT * FROM ia_resources ORDER BY kind,id").fetchall():
            raise RuntimeError("Resources changed during cleanup; transaction rolled back.")
        report.update(status="deleted", deleted_counts=counts, retained_unchanged=True)
    # Only mark success after the transaction's context manager commits.
    if manifest:
        manifest.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps({"deleted": counts["ia_runs"], "retained": len(remaining),
                          "manifest": str(manifest.relative_to(ROOT)), "backup_sha256": digest}))


if __name__ == "__main__":
    main()
