"""Opt-in native MySQL probe. Installs no service and only creates .runtime files.

Downloads the official noinstall archive, verifies its published integrity hash,
uses a fresh data directory, binds only loopback, and stops only the new process.
Neither credentials nor query data leave this machine.
"""
import argparse
import hashlib
import json
import os
import secrets
import socket
import subprocess
import sys
import time
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / ".runtime" / "deps"))

import pymysql  # noqa: E402 -- probe driver is isolated under this project's .runtime/deps

from insight.connectors import MySQLConnector  # noqa: E402
from insight.sql import SQLRejected  # noqa: E402

VERSION = "8.4.11"
URL = f"https://cdn.mysql.com/Downloads/MySQL-8.4/mysql-{VERSION}-winx64.zip"
ARCHIVE_MD5 = "2e833921898a9a030ea6bfe81bd811bc"
ARCHIVE_SIZE = 281191914
PORT = 13306


def prepare_binary():
    runtime = ROOT / ".runtime"
    runtime.mkdir(exist_ok=True)
    archive = runtime / f"mysql-{VERSION}-winx64.zip"
    offset = archive.stat().st_size if archive.exists() else 0
    if offset > ARCHIVE_SIZE:
        raise RuntimeError("Existing archive is not the expected package; refusing to overwrite it")
    if offset < ARCHIVE_SIZE:
        print("Downloading official MySQL 8.4.11 portable archive (268.2 MiB)", flush=True)
        request = urllib.request.Request(URL, headers={"Range": f"bytes={offset}-"} if offset else {})
        with urllib.request.urlopen(request, timeout=30) as response:
            if offset and (response.status != 206 or not response.headers.get("Content-Range", "").startswith(f"bytes {offset}-")):
                raise RuntimeError("Download server did not support safe resume; partial archive retained")
            with archive.open("ab" if offset else "xb") as output:
                while block := response.read(1024 * 1024):
                    output.write(block)
    checksum = hashlib.md5()
    with archive.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            checksum.update(block)
    if checksum.hexdigest() != ARCHIVE_MD5:
        raise RuntimeError("Official archive integrity check failed; partial archive retained for inspection")
    binary_root = runtime / f"mysql-{VERSION}-winx64"
    binary = binary_root / "bin" / "mysqld.exe"
    if not binary_root.exists():
        print("Extracting verified archive inside project .runtime", flush=True)
        with zipfile.ZipFile(archive) as package:
            for member in package.infolist():
                destination = (runtime / member.filename).resolve()
                if not destination.is_relative_to(binary_root.resolve()):
                    raise RuntimeError("Unsafe archive member")
            package.extractall(runtime)
    if not binary.is_file():
        raise RuntimeError("The existing portable directory is incomplete; refusing to overwrite it")
    return binary_root, binary


def probe():
    with socket.socket() as port_check:
        if port_check.connect_ex(("127.0.0.1", PORT)) == 0:
            raise RuntimeError("Port 13306 is already in use; refusing to touch the listener")
    binary_root, binary = prepare_binary()
    run_root = ROOT / ".runtime" / f"mysql-probe-{uuid4().hex[:12]}"
    run_root.mkdir()
    data = run_root / "data"
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    common = [str(binary), "--no-defaults", f"--basedir={binary_root.as_posix()}", f"--datadir={data.as_posix()}"]
    print("Initializing a new isolated MySQL data directory", flush=True)
    initialized = subprocess.run([*common, "--initialize-insecure", f"--log-error={(run_root / 'initialize.log').as_posix()}"],
                                 creationflags=flags, capture_output=True, timeout=90, check=False)
    if initialized.returncode:
        raise RuntimeError(f"Native MySQL initialization failed (exit {initialized.returncode}); inspect .runtime initialize.log")
    process = subprocess.Popen([*common, f"--port={PORT}", "--bind-address=127.0.0.1", "--mysqlx=0",
        "--secure-file-priv=NULL", "--local-infile=OFF", "--skip-log-bin", "--performance-schema=OFF",
        "--innodb-buffer-pool-size=64M", f"--log-error={(run_root / 'mysql.log').as_posix()}",
        f"--pid-file={(run_root / 'mysqld.pid').as_posix()}"], creationflags=flags,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    admin = None
    env_name = "INSIGHT_SOURCE_NATIVE_PROBE_PASSWORD"
    previous = os.environ.get(env_name)
    try:
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError("Isolated native MySQL exited before startup; inspect .runtime mysql.log")
            try:
                admin = pymysql.connect(host="127.0.0.1", port=PORT, user="root", password="", autocommit=True, connect_timeout=1)
                break
            except pymysql.Error:
                time.sleep(0.2)
        if admin is None:
            raise TimeoutError("Isolated MySQL did not become ready")
        password = secrets.token_urlsafe(32)
        with admin.cursor() as cursor:
            cursor.execute("ALTER USER 'root'@'localhost' IDENTIFIED BY %s", (secrets.token_urlsafe(40),))
            cursor.execute("SELECT @@datadir,@@version")
            actual_data, version = cursor.fetchone()
            if Path(actual_data).resolve() != data.resolve() or "MariaDB" in version:
                raise RuntimeError("Unexpected database identity")
            cursor.execute("CREATE DATABASE insight_agents_checks")
            cursor.execute("CREATE TABLE insight_agents_checks.orders(id INT,amount DECIMAL(12,2),day DATE)")
            cursor.execute("CREATE TABLE insight_agents_checks.refunds(order_id INT,amount DECIMAL(12,2))")
            cursor.execute("INSERT INTO insight_agents_checks.orders VALUES(1,10,'2025-01-01'),(2,20,'2025-01-02'),(3,30,'2025-01-03')")
            cursor.execute("INSERT INTO insight_agents_checks.refunds VALUES(1,2),(1,3),(2,4)")
            cursor.execute("CREATE USER 'insight_reader'@'127.0.0.1' IDENTIFIED BY %s", (password,))
            cursor.execute("GRANT SELECT ON insight_agents_checks.* TO 'insight_reader'@'127.0.0.1'")
        os.environ[env_name] = password
        source = {"id": "native-mysql-check", "kind": "mysql", "config": {"host": "127.0.0.1", "port": PORT,
            "database": "insight_agents_checks", "user": "insight_reader", "password_env": env_name,
            "schemas": ["insight_agents_checks"], "allowed_tables": ["orders", "refunds"]}}
        connector = MySQLConnector(source)
        status = connector.test()
        assert status["status"] == "ok", status
        tables = connector.introspect()
        assert {table["table_name"] for table in tables} == {"orders", "refunds"}
        scene = {"tables": tables, "relations": [{"left_table": "orders", "left_column": "id", "right_table": "refunds", "right_column": "order_id"}]}
        allowed = [table["name"] for table in tables]
        result = connector.execute("SELECT id,amount FROM orders WHERE day>=CAST(? AS DATE) ORDER BY id", scene, allowed, "binding", parameters=["2025-01-02"], limit=1)
        assert result["rows"] == [[2, 20.0]] and result["truncated"]
        result = connector.execute("SELECT o.id,(SELECT SUM(r.amount) FROM refunds r WHERE r.order_id=o.id) AS refund FROM orders o ORDER BY o.id", scene, allowed, "subquery")
        assert result["rows"] == [[1, 5.0], [2, 4.0], [3, None]]
        try:
            connector.execute("SELECT * FROM mysql.user", scene, allowed, "system")
        except SQLRejected:
            pass
        else:
            raise AssertionError("System access unexpectedly allowed")
        reader = pymysql.connect(host="127.0.0.1", port=PORT, user="insight_reader", password=password, database="insight_agents_checks")
        try:
            with reader.cursor() as cursor:
                try:
                    cursor.execute("INSERT INTO orders VALUES(99,99,'2025-01-01')")
                except pymysql.err.OperationalError as exc:
                    assert exc.args[0] == 1142
                else:
                    raise AssertionError("Reader role has unexpected write permission")
        finally:
            reader.rollback()
            reader.close()
        with admin.cursor() as cursor:
            cursor.executemany("INSERT INTO insight_agents_checks.orders VALUES(1,%s,'2025-01-01')", [(i,) for i in range(12000)])
            cursor.executemany("INSERT INTO insight_agents_checks.refunds VALUES(1,%s)", [(i,) for i in range(12000)])
        expensive = "SELECT SUM(SQRT(o.amount*r.amount)) AS total FROM orders o JOIN refunds r ON o.id=r.order_id"
        try:
            connector.execute(expensive, scene, allowed, "timeout", timeout=0.02)
        except TimeoutError:
            pass
        else:
            raise AssertionError("Expensive query did not time out")
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(connector.execute, expensive, scene, allowed, "cancel", timeout=10)
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                with connector._lock:
                    active = connector._active.get("cancel")
                if active is not None and active.callback is not None:
                    break
                time.sleep(0.01)
            assert connector.cancel("cancel")
            try:
                future.result(timeout=6)
            except TimeoutError:
                pass
            else:
                raise AssertionError("Cancelled query unexpectedly completed")
        return {"status": "passed", "real_engine": True, "version": version, "read_only_role": True,
            "metadata": True, "parameter_binding": True, "schema_scope": True, "controlled_subquery": True,
            "row_limit": True, "statement_timeout": True, "explicit_cancellation": True,
            "runtime_directory": str(run_root.relative_to(ROOT)), "stopped_after_probe": True}
    finally:
        if previous is None:
            os.environ.pop(env_name, None)
        else:
            os.environ[env_name] = previous
        if admin is not None:
            try:
                with admin.cursor() as cursor:
                    cursor.execute("SHUTDOWN")
            except pymysql.Error:
                pass
            finally:
                admin.close()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            # This is exactly the newly created process, never an existing server.
            process.terminate()
            process.wait(timeout=10)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", required=True)
    parser.parse_args()
    try:
        report = probe()
    except Exception as exc:  # noqa: BLE001 -- isolated probe must report failures and clean up
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__,
            "message": str(exc)[:300] if type(exc) is RuntimeError else "Native probe failed; inspect its project runtime logs"}))
        raise SystemExit(1)
    print(json.dumps(report, indent=2))
