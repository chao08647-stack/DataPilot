"""Create/start only Insight Agents' own loopback PostgreSQL cluster. Never touches another DB."""

import argparse
import json
import os
import secrets
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bin-dir", type=Path, required=True, help="Directory containing initdb/pg_ctl")
    parser.add_argument("--stop", action="store_true", help="Stop ONLY this project's own cluster")
    args = parser.parse_args()
    runtime = ROOT / ".runtime"
    cluster = (runtime / "postgres").resolve()
    if cluster.parent != runtime.resolve():
        raise RuntimeError("Invalid cluster path")
    runtime.mkdir(exist_ok=True)
    exe = ".exe" if os.name == "nt" else ""
    pg_ctl = str(args.bin_dir / f"pg_ctl{exe}")
    env = {**os.environ, "PATH": str(args.bin_dir) + os.pathsep + os.environ.get("PATH", "")}
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

    def command(parts, check=True):
        # On Windows a daemon may inherit pipe handles; a disk-backed temporary log avoids
        # waiting for EOF from every server descendant after pg_ctl itself already exited.
        with tempfile.TemporaryFile(mode="w+b") as output:
            result = subprocess.run(parts, env=env, stdout=output, stderr=output, creationflags=flags, timeout=40, check=False)
            output.seek(0)
            diagnostics = output.read().decode("utf-8", errors="replace")
        if check and result.returncode:
            # PostgreSQL messages contain only the NEW cluster path, never a connection URI or password.
            raise RuntimeError(diagnostics[-1600:])
        return result

    if args.stop:
        command([pg_ctl, "-D", str(cluster), "-m", "fast", "stop"])
        print("Insight Agents local PostgreSQL stopped; data retained.")
        return
    secret_path = runtime / "local-postgres.json"
    if not (cluster / "PG_VERSION").exists():
        if cluster.exists() and any(cluster.iterdir()):
            raise RuntimeError("Refusing to initialize over a nonempty directory")
        password = secrets.token_urlsafe(32)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False, dir=runtime) as handle:
            handle.write(password)
            password_file = Path(handle.name)
        try:
            command([str(args.bin_dir / f"initdb{exe}"), "-D", str(cluster), "-U", "insight", "-A", "scram-sha-256", "--encoding=UTF8", "--locale=C", f"--pwfile={password_file}"])
            secret_path.write_text(json.dumps({"password": password}), encoding="utf-8")
        finally:
            password_file.unlink(missing_ok=True)
    if not secret_path.exists():
        raise RuntimeError("Existing cluster credentials missing; refusing to replace them")
    if command([str(args.bin_dir / f"pg_isready{exe}"), "-h", "127.0.0.1", "-p", "15432", "-t", "3"], check=False).returncode:
        command([pg_ctl, "-D", str(cluster), "-l", str(runtime / "postgres.log"), "-o", "-h 127.0.0.1 -p 15432", "-w", "start"])
    import psycopg
    password = json.loads(secret_path.read_text(encoding="utf-8"))["password"]
    with psycopg.connect(host="127.0.0.1", port=15432, user="insight", password=password, dbname="postgres", autocommit=True, connect_timeout=5) as conn:
        # Verify identity before creating anything on a possibly occupied port.
        configured = Path(conn.execute("SHOW data_directory").fetchone()[0]).resolve()
        if configured != cluster:
            raise RuntimeError("Port 15432 belongs to another PostgreSQL cluster; no changes made")
        if not conn.execute("SELECT 1 FROM pg_database WHERE datname='insight_agents'").fetchone():
            conn.execute("CREATE DATABASE insight_agents")
    print("Independent PostgreSQL ready on loopback:15432 (database insight_agents).")


if __name__ == "__main__":
    main()
