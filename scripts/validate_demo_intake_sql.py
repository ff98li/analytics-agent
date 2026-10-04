#!/usr/bin/env python3
"""Validate the pinned demo's four SELECTs in a new, private PostgreSQL cluster.

Default is dry-run artifact verification. --execute-isolated is deliberately
explicit. No host, DSN, existing PGDATA, or existing database can be supplied.
The stopped cluster and its private logs are retained for evidence/recovery.
This is SQL/data acceptance, never workflow or model execution evidence.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from typing import Any, Iterator

DUMP_SHA256 = "4bf135762f322fd668631262b554978fc99a983442f457a853c9ab44d71d425a"
DUMP_BYTES = 38_020_305
# Local identity of the reviewed conda-forge PostgreSQL 18.6 client. These
# hashes prevent an arbitrary --pg-bin from becoming an execution interface;
# they identify this installation, not an upstream supply-chain signature.
PG_PROGRAM_SHA256 = {
    "initdb": "cef85c020858b41385a6277a90e11566ac6c8799ceeef03b9c6d39632a84d027",
    "pg_ctl": "622cee78cb87d7b41e70c139a28527722c112e2a00ecbe8bdcc9a290c707cc1b",
    "pg_restore": "f8ea8bc061707efbc1937be9d2392c38dbe24629fb7686df643e7f4680466f77",
    "psql": "4c5df2bb009b79564c9321347eb98bb2393e7a582b6ccc790728fa261e731a32",
    "createdb": "b5fbf9d4664bf59d453b69142d616185a81e8707e96cca05abb3be8e1f92f1db",
    "postgres": "094e1a864a73a4b4f6b9c877b1b55ecf57f64267860defd6b91c9ec558e37c58",
}
PORT = 55439  # Private socket namespace; no TCP listener is created.
OWNER = "cp5105_intake_owner"
READER = "cp5105_intake_reader"
DATABASE = "cp5105_demo_intake"
DATE = "2025-03-01"
EXPECTED_COUNTS = {
    "Fundamentals Query": 10,
    "Insider Query": 10,
    "Market Query": 10,
    "News Query": 8,
}
REQUIRED_COLUMNS = {
    "Fundamentals Query": {
        "symbol", "report_date", "period", "companyName", "revenue", "netIncome",
        "operatingCashFlow", "freeCashFlow", "current_ratio", "debt_to_equity",
        "gross_margin", "operating_margin", "net_margin", "fcf_conversion",
    },
    "Insider Query": {
        "symbol", "month_start", "sentiment_net_change", "sentiment_avg_mspr",
        "tx_count", "tx_net_notional", "buy_count", "sell_count",
        "sentiment_mspr_3m_avg", "tx_net_notional_3m",
    },
    "Market Query": {
        "symbol", "time_bucket", "bucket_open", "bucket_close", "bucket_high",
        "bucket_low", "bucket_volume", "bucket_return", "week52_high", "week52_low",
        "ytd_return_pct", "quarter_return_pct", "five_day_return_pct", "beta",
        "market_cap_m", "pe_ttm", "pb_quarterly", "revenue_growth_ttm_yoy_pct",
        "eps_growth_ttm_yoy_pct", "operating_margin_ttm_pct", "net_profit_margin_ttm_pct",
    },
    "News Query": {
        "symbol", "id", "title", "publishedDate", "publisher", "source", "category",
        "path", "synopsis", "summary", "recency_rank", "category_rank",
    },
}
NONNULL_COLUMNS = {
    "Fundamentals Query": ("symbol", "report_date", "period", "companyName", "revenue", "netIncome"),
    "Insider Query": ("symbol", "month_start"),  # FULL OUTER JOIN legitimately has NULLs.
    "Market Query": ("symbol", "time_bucket", "bucket_open", "bucket_close", "bucket_high", "bucket_low", "bucket_volume"),
    "News Query": ("symbol", "id", "title", "publishedDate", "path", "synopsis"),
}


class IntakeSQLError(ValueError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise IntakeSQLError(message)


def checked_path(value: str | Path, *, directory: bool = False) -> Path:
    """Reject symlinks in every input component, not merely the final filename."""
    path = Path(value)
    require(path.is_absolute() and ".." not in path.parts, "absolute non-traversing paths required")
    for component in (*reversed(path.parents), path):
        require(not component.is_symlink(), f"symlink refused: {component}")
    info = path.stat()
    require(stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode), f"wrong input type: {path}")
    return path


def read_bytes(path: Path, limit: int) -> bytes:
    checked_path(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as source:
        require(stat.S_ISREG(os.fstat(source.fileno()).st_mode), "input is not a regular file")
        data = source.read(limit + 1)
    require(len(data) <= limit, f"input exceeds size bound: {path}")
    return data


def verify_dump(path: Path, destination: Path | None = None) -> str:
    """Hash an O_NOFOLLOW input, optionally copying that exact stream privately."""
    checked_path(path)
    digest = hashlib.sha256()
    size = 0
    output = None
    try:
        if destination is not None:
            fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            output = os.fdopen(fd, "wb")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as source:
            require(stat.S_ISREG(os.fstat(source.fileno()).st_mode), "dump must be a regular file")
            while chunk := source.read(1024 * 1024):
                size += len(chunk)
                require(size <= DUMP_BYTES, "dump exceeds pinned size")
                digest.update(chunk)
                if output is not None:
                    output.write(chunk)
        require(size == DUMP_BYTES and digest.hexdigest() == DUMP_SHA256, "dump fingerprint mismatch")
        return digest.hexdigest()
    finally:
        if output is not None:
            output.close()


def load_cases(original: Path, pairs: list[list[str]]) -> list[dict[str, Any]]:
    # This helper re-derives both artifacts from the pinned original; manifest
    # hashes alone are not a trust root. It never runs the authored workflow.
    from analytics_agent.demo_intake import verified_sql_queries

    original_bytes = read_bytes(original, 1_000_000)
    require(len(pairs) == 2, "exactly two --case WORKFLOW MANIFEST pairs required")
    cases = []
    for workflow_path, manifest_path in pairs:
        workflow = read_bytes(Path(workflow_path), 1_000_000)
        manifest_bytes = read_bytes(Path(manifest_path), 1_000_000)
        queries = verified_sql_queries(original_bytes, workflow, manifest_bytes)
        manifest = json.loads(manifest_bytes)
        require(manifest.get("runtime_blocked") is True, "manifest must keep runtime/models blocked")
        fixed = manifest["fixed_inputs"]
        require(fixed["ticker"] in {"AAPL", "NVDA"} and fixed["start_date"] == DATE, "only pinned AAPL/NVDA and 2025-03-01 accepted")
        require(set(queries) == set(EXPECTED_COUNTS), "expected exactly four verified SQL queries")
        require(all(isinstance(q, str) and q.lstrip().upper().startswith("WITH ") for q in queries.values()), "expected authored WITH/SELECT queries")
        cases.append({"ticker": fixed["ticker"], "queries": queries, "adapted_sha256": hashlib.sha256(workflow).hexdigest(), "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest()})
    require({case["ticker"] for case in cases} == {"AAPL", "NVDA"}, "both distinct pinned tickers required")
    return sorted(cases, key=lambda case: case["ticker"])


def clean_environment(pg_bin: Path) -> dict[str, str]:
    # Do not inherit any libpq service, host, password, preload, or shell settings.
    return {"PATH": f"{pg_bin}:/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "C", "LC_ALL": "C", "TZ": "UTC"}


def verify_programs(pg_bin: Path) -> None:
    checked_path(pg_bin, directory=True)
    for program, expected in PG_PROGRAM_SHA256.items():
        path = checked_path(pg_bin / program)
        require(os.access(path, os.X_OK), f"missing executable: {program}")
        require(hashlib.sha256(read_bytes(path, 32_000_000)).hexdigest() == expected, f"PostgreSQL executable fingerprint mismatch: {program}")


@contextmanager
def without_pg_environment() -> Iterator[None]:
    """libpq used by psycopg must not inherit PGSERVICE/PGOPTIONS/etc either."""
    saved = {key: value for key, value in os.environ.items() if key.startswith("PG")}
    for key in saved:
        del os.environ[key]
    try:
        yield
    finally:
        for key in tuple(os.environ):
            if key.startswith("PG"):
                del os.environ[key]
        os.environ.update(saved)


def json_value(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        require(value.is_finite(), "non-finite decimal result")
        return str(value)
    if isinstance(value, float):
        require(math.isfinite(value), "non-finite floating result")
    if isinstance(value, dict):
        return {key: json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_value(item) for item in value]
    return value


def summarize_rows(name: str, ticker: str, columns: list[str], rows: list[dict[str, Any]]) -> dict[str, Any]:
    require(len(columns) == len(set(columns)), f"{name}: duplicate result columns")
    require(REQUIRED_COLUMNS[name] <= set(columns), f"{name}: missing result columns")
    require(len(rows) == EXPECTED_COUNTS[name], f"{ticker}/{name}: expected {EXPECTED_COUNTS[name]} rows, got {len(rows)}")
    for row in rows:
        require(set(row) == set(columns), f"{name}: inconsistent row shape")
        require(row["symbol"] == ticker, f"{name}: foreign ticker in result")
        for key in NONNULL_COLUMNS[name]:
            require(row.get(key) is not None and row[key] != "", f"{ticker}/{name}: empty {key}")
        if name in {"Fundamentals Query", "Insider Query"}:
            key = "report_date" if name == "Fundamentals Query" else "month_start"
            require(type(row[key]) is date, f"{ticker}/{name}: expected PostgreSQL date for {key}")
        if name == "Market Query":
            value = row["time_bucket"]
            require(isinstance(value, datetime) and value.utcoffset() is not None and value.utcoffset().total_seconds() == 0, f"{ticker}/{name}: expected UTC-aware time_bucket")
    if name == "Insider Query":
        require(any(row.get("tx_count") is not None for row in rows), f"{ticker}: no insider transaction evidence")
    if name == "Fundamentals Query":
        for key in ("operatingCashFlow", "freeCashFlow", "current_ratio", "gross_margin"):
            require(any(row.get(key) is not None for row in rows), f"{ticker}: no financial evidence for {key}")
    normalized = json_value(rows)
    encoded = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    result = {"row_count": len(rows), "columns": columns, "null_counts": {key: sum(row[key] is None for row in rows) for key in columns}, "rows_sha256": hashlib.sha256(encoded).hexdigest(), "rows_hash_encoding": "ordered rows; sorted object keys; UTF-8 compact JSON; temporal ISO8601; Decimal string"}
    if name == "News Query":
        from analytics_agent.demo_intake import private_image_key

        ids = [row["id"] for row in rows]
        keys = [private_image_key(item) for item in ids]
        require(len(set(ids)) == len(ids), f"{ticker}: duplicate news IDs")
        for row in rows:
            require(isinstance(row["publishedDate"], str), "expected source news timestamp text")
            parsed = datetime.fromisoformat(row["publishedDate"])
            require(parsed.tzinfo is None, "expected naive source news timestamp; do not invent a timezone")
        result["news_ids"] = ids
        result["news_private_image_keys"] = keys
        result["news_published_dates"] = [row["publishedDate"] for row in normalized]
        result["news_timestamp_semantics"] = "ISO8601-parseable source text without timezone; not asserted UTC or as-of"
    return result


class OwnedCluster:
    """A cluster whose target can only originate from this object's mkdtemp."""

    def __init__(self, pg_bin: Path):
        self.pg_bin = checked_path(pg_bin, directory=True)
        verify_programs(pg_bin)
        self.root = Path(tempfile.mkdtemp(prefix="cp5105-demo-sql-", dir="/private/tmp"))
        self.root.chmod(0o700)
        self.identity = (self.root.stat().st_dev, self.root.stat().st_ino)
        self.data = self.root / "data"
        self.socket = self.root / "socket"
        self.socket.mkdir(mode=0o700)
        self.env = clean_environment(pg_bin)
        self.pid: int | None = None
        self.sequence = 0
        self.initialized = False

    def verify_owned(self) -> None:
        info = checked_path(self.root, directory=True).stat()
        require(self.root.parent == Path("/private/tmp") and self.root.name.startswith("cp5105-demo-sql-"), "cluster target escaped private temporary root")
        require((info.st_dev, info.st_ino) == self.identity and info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o700, "cluster ownership changed")
        for child in (self.data, self.socket):
            if child.exists() or child.is_symlink():
                item = checked_path(child, directory=True).stat()
                require(item.st_uid == os.getuid() and stat.S_IMODE(item.st_mode) == 0o700, "cluster child ownership changed")

    def command(self, program: str, *args: str, expected: tuple[int, ...] = (0,)) -> subprocess.CompletedProcess:
        self.verify_owned()
        self.sequence += 1
        with (self.root / f"{self.sequence:02d}-{program}.stdout.log").open("xb") as out, (self.root / f"{self.sequence:02d}-{program}.stderr.log").open("xb") as err:
            result = subprocess.run([str(self.pg_bin / program), *args], env=self.env, stdin=subprocess.DEVNULL, stdout=out, stderr=err, timeout=60, check=False)
        require(result.returncode in expected, f"{program} failed ({result.returncode}); inspect private logs in {self.root}")
        return result

    def client_args(self, database: str = DATABASE) -> tuple[str, ...]:
        return ("--host", str(self.socket), "--port", str(PORT), "--username", OWNER, "--dbname", database)

    def capture_pid(self) -> int | None:
        self.verify_owned()
        pid_file = self.data / "postmaster.pid"
        if not pid_file.exists():
            return None
        lines = read_bytes(pid_file, 4096).decode().splitlines()
        require(len(lines) >= 6 and lines[1] == str(self.data) and lines[3] == str(PORT) and lines[4] == str(self.socket) and lines[5] == "", "postmaster identity/listener mismatch")
        pid = int(lines[0])
        require(pid > 1 and (self.pid is None or self.pid == pid), "postmaster PID changed")
        probe = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "command="], env=self.env, capture_output=True, text=True, timeout=5, check=False)
        require(probe.returncode == 0 and str(self.pg_bin / "postgres") in probe.stdout and f"-D {self.data}" in probe.stdout, "postmaster process identity mismatch; refusing process control")
        self.pid = pid
        return pid

    def start(self, dump: Path) -> None:
        self.verify_owned()
        require(not self.data.exists(), "refusing existing PGDATA collision")
        snapshot = self.root / "verified-demo.dump"
        verify_dump(dump, snapshot)  # Restore only the verified, private snapshot.
        self.command("initdb", "-D", str(self.data), "--username", OWNER, "--auth-local=trust", "--auth-host=reject", "--encoding=UTF8", "--no-locale")
        self.initialized = True
        options = f"-h '' -k {self.socket} -p {PORT} -c unix_socket_permissions=0700 -c max_connections=10 -c shared_buffers=128MB"
        self.command("pg_ctl", "-D", str(self.data), "-l", str(self.root / "postgres.log"), "-o", options, "-w", "-t", "30", "start")
        self.capture_pid()
        self.command("createdb", "--host", str(self.socket), "--port", str(PORT), "--username", OWNER, DATABASE)
        self.command("pg_restore", *self.client_args(), "--no-owner", "--no-privileges", "--exit-on-error", "--single-transaction", str(snapshot))
        grants = f"""CREATE ROLE {READER} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
REVOKE ALL ON DATABASE {DATABASE} FROM PUBLIC;
GRANT CONNECT ON DATABASE {DATABASE} TO {READER};
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA demo TO {READER};
GRANT SELECT ON ALL TABLES IN SCHEMA demo TO {READER};
ALTER ROLE {READER} SET default_transaction_read_only = on;"""
        self.command("psql", "-X", "--set=ON_ERROR_STOP=1", *self.client_args(), "--command", grants)

    def stop(self) -> dict[str, Any]:
        self.verify_owned()
        if not self.initialized:
            return {"stopped": True, "server_started": False}
        pid = self.capture_pid()
        if pid is not None:
            try:
                self.command("pg_ctl", "-D", str(self.data), "-m", "fast", "-w", "-t", "30", "stop")
            except (IntakeSQLError, subprocess.TimeoutExpired):
                if self.capture_pid() is not None:
                    self.command("pg_ctl", "-D", str(self.data), "-m", "immediate", "-w", "-t", "15", "stop")
        self.command("pg_ctl", "-D", str(self.data), "status", expected=(3,))
        require(not (self.data / "postmaster.pid").exists(), "postmaster.pid remains after shutdown")
        if self.pid is not None:
            try:
                os.kill(self.pid, 0)
            except ProcessLookupError:
                pass
            else:
                raise IntakeSQLError("postmaster PID still exists after shutdown; no success claimed")
        return {"stopped": True, "postmaster_pid": self.pid, "pg_ctl_status": 3, "postmaster_pid_file_absent": True}


def run_queries(cluster: OwnedCluster, cases: list[dict[str, Any]]) -> dict[str, Any]:
    import psycopg
    from psycopg.rows import dict_row

    cluster.capture_pid()
    connect_args = {"host": str(cluster.socket), "port": PORT, "dbname": DATABASE, "connect_timeout": 5, "options": "-c timezone=UTC -c statement_timeout=15000 -c default_transaction_read_only=on", "row_factory": dict_row}
    # data_directory is a privileged SHOW parameter: check it as the isolated
    # owner, without granting pg_read_all_settings to the query reader.
    with without_pg_environment(), psycopg.connect(**connect_args, user=OWNER) as owner_conn:
        with owner_conn.cursor() as cursor:
            cursor.execute("SELECT current_setting('data_directory') AS data, current_setting('listen_addresses') AS listen, current_setting('server_version') AS version")
            server = cursor.fetchone()
            require(server is not None and server["data"] == str(cluster.data) and server["listen"] == "" and server["version"].startswith("18.6"), "isolated server identity/settings mismatch")
    with without_pg_environment(), psycopg.connect(**connect_args, user=READER) as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT current_user AS role, current_database() AS db, current_setting('TimeZone') AS timezone, current_setting('transaction_read_only') AS readonly")
            settings = cursor.fetchone()
            require(settings is not None and settings["role"] == READER and settings["db"] == DATABASE and settings["timezone"] == "UTC" and settings["readonly"] == "on", "isolated reader settings mismatch")
            cursor.execute("SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls FROM pg_roles WHERE rolname = current_user")
            require(not any(cursor.fetchone().values()), "reader has privileged role attributes")
            cursor.execute("SELECT has_database_privilege(current_database(), 'TEMP') AS temp, has_schema_privilege('demo', 'CREATE') AS create_demo, EXISTS (SELECT 1 FROM information_schema.tables WHERE table_schema='demo' AND has_table_privilege(format('%I.%I', table_schema, table_name), 'INSERT, UPDATE, DELETE, TRUNCATE')) AS write_tables")
            require(not any(cursor.fetchone().values()), "reader has write capabilities")
        conn.commit()
        results = {}
        for case in cases:
            outputs = {}
            for name, query in case["queries"].items():
                with conn.transaction(), conn.cursor() as cursor:
                    cursor.execute("SET TRANSACTION READ ONLY")
                    cursor.execute("SET LOCAL statement_timeout = '15s'")
                    cursor.execute("SET LOCAL TimeZone = 'UTC'")
                    cursor.execute(query)
                    require(cursor.description is not None, f"{name}: SELECT produced no columns")
                    columns = [column.name for column in cursor.description]
                    type_oids = [column.type_code for column in cursor.description]
                    outputs[name] = summarize_rows(name, case["ticker"], columns, cursor.fetchmany(11))
                    outputs[name]["sql_sha256"] = hashlib.sha256(query.encode()).hexdigest()
                    cursor.execute("SELECT oid, pg_catalog.format_type(oid, NULL) AS type_name FROM pg_catalog.pg_type WHERE oid = ANY(%s)", (type_oids,))
                    type_names = {item["oid"]: item["type_name"] for item in cursor.fetchall()}
                    require(set(type_oids) <= set(type_names), "unresolved PostgreSQL result type")
                    outputs[name]["column_types"] = [{"column": column, "postgres_oid": oid, "postgres_type": type_names[oid]} for column, oid in zip(columns, type_oids, strict=True)]
            results[case["ticker"]] = {"adapted_sha256": case["adapted_sha256"], "manifest_sha256": case["manifest_sha256"], "queries": outputs}
    return {"connection": settings | server, "cases": results}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--original", required=True, type=Path)
    parser.add_argument("--dump", required=True, type=Path)
    parser.add_argument("--pg-bin", required=True, type=Path)
    parser.add_argument("--case", action="append", nargs=2, metavar=("WORKFLOW", "MANIFEST"), required=True)
    parser.add_argument("--execute-isolated", action="store_true")
    args = parser.parse_args(argv)
    evidence: dict[str, Any] = {"ok": False, "stage": "sql_acceptance" if args.execute_isolated else "dry_run", "runtime_blocked": True, "workflow_executed": False, "model_executed": False, "s3_exercised": False, "temporal_semantics": "asynchronous historical snapshot; fixed start_date 2025-03-01; not an as-of backtest or no-future-leakage claim"}
    cluster = None
    previous_umask = os.umask(0o077)
    try:
        cases = load_cases(args.original, args.case)
        evidence["dump_sha256"] = verify_dump(args.dump)
        verify_programs(args.pg_bin)
        if args.execute_isolated:
            cluster = OwnedCluster(args.pg_bin)
            evidence["retained_private_cluster"] = str(cluster.root)
            cluster.start(args.dump)
            evidence.update(run_queries(cluster, cases))
        else:
            evidence["cases"] = {case["ticker"]: {"adapted_sha256": case["adapted_sha256"], "manifest_sha256": case["manifest_sha256"], "queries": {name: {"expected_rows": EXPECTED_COUNTS[name], "sql_sha256": hashlib.sha256(query.encode()).hexdigest()} for name, query in case["queries"].items()}} for case in cases}
            evidence["notice"] = "Artifact checks only: no cluster created, SQL executed, or runtime acceptance."
        evidence["ok"] = True
    except (Exception, KeyboardInterrupt) as exc:
        evidence["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if cluster is not None:
            try:
                evidence["cleanup"] = cluster.stop()
            except (Exception, KeyboardInterrupt) as exc:
                evidence["ok"] = False
                evidence["cleanup"] = {"stopped": False, "error": f"{type(exc).__name__}: {exc}", "recovery_required": True}
        os.umask(previous_umask)
    print(json.dumps(json_value(evidence), ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False))
    return 0 if evidence["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
