"""Offline harness tests: subprocess and database startup are never invoked.

Optional byte-pinned artifact integration is enabled only by the explicit
CP5105_DEMO_ORIGINAL environment variable; ordinary CI has no parent-file need.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import importlib.util
import json
import os
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "validate_demo_intake_sql.py"
SPEC = importlib.util.spec_from_file_location("demo_sql_harness", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
harness = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(harness)


@pytest.fixture(autouse=True)
def forbid_subprocess(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("offline test attempted a subprocess")
    monkeypatch.setattr(harness.subprocess, "run", forbidden)


def test_pg_environment_is_scrubbed_and_restored(monkeypatch):
    monkeypatch.setenv("PGSERVICE", "external-db")
    monkeypatch.setenv("PGHOST", "external-host")
    monkeypatch.setenv("PGPASSWORD", "dummy-test-value")
    with pytest.raises(RuntimeError), harness.without_pg_environment():
        assert not any(key.startswith("PG") for key in os.environ)
        raise RuntimeError("test exceptional exit")
    assert os.environ["PGSERVICE"] == "external-db"
    assert os.environ["PGHOST"] == "external-host"
    assert os.environ["PGPASSWORD"] == "dummy-test-value"
    assert not any(key.startswith("PG") for key in harness.clean_environment(Path("/client/bin")))


def test_dump_copy_is_pinned_and_no_clobber(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    source = root / "demo.dump"
    source.write_bytes(b"tiny verified fixture")
    monkeypatch.setattr(harness, "DUMP_BYTES", source.stat().st_size)
    monkeypatch.setattr(harness, "DUMP_SHA256", hashlib.sha256(source.read_bytes()).hexdigest())
    target = root / "snapshot.dump"
    assert harness.verify_dump(source, target) == harness.DUMP_SHA256
    assert target.read_bytes() == source.read_bytes()
    assert target.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        harness.verify_dump(source, target)


def test_bad_dump_fingerprint_is_refused(tmp_path, monkeypatch):
    source = tmp_path.resolve() / "demo.dump"
    source.write_bytes(b"not trusted")
    monkeypatch.setattr(harness, "DUMP_BYTES", source.stat().st_size)
    with pytest.raises(harness.IntakeSQLError, match="fingerprint"):
        harness.verify_dump(source)


@pytest.mark.parametrize("parent_link", [False, True])
def test_symlink_inputs_are_refused(tmp_path, parent_link):
    root = tmp_path.resolve()
    source = root / "original.json"
    source.write_bytes(b"fixture")
    if parent_link:
        link = root / "linked-directory"
        link.symlink_to(root, target_is_directory=True)
        path = link / source.name
    else:
        path = root / "linked.json"
        path.symlink_to(source)
    with pytest.raises(harness.IntakeSQLError, match="symlink"):
        harness.read_bytes(path, 100)


def test_postgres_programs_are_byte_pinned(tmp_path, monkeypatch):
    binary = tmp_path.resolve() / "pg_ctl"
    binary.write_bytes(b"not a postgres client")
    binary.chmod(0o700)
    monkeypatch.setattr(harness, "PG_PROGRAM_SHA256", {"pg_ctl": "0" * 64})
    with pytest.raises(harness.IntakeSQLError, match="executable fingerprint"):
        harness.verify_programs(binary.parent)


def make_rows(name="News Query", ticker="AAPL"):
    columns = sorted(harness.REQUIRED_COLUMNS[name])
    rows = []
    for number in range(harness.EXPECTED_COUNTS[name]):
        row = {key: 1 for key in columns}
        row.update(symbol=ticker)
        if name == "Fundamentals Query":
            row["report_date"] = date(2024, 12, 28)
        if name == "Insider Query":
            row["month_start"] = date(2025, 3, 1)
        if name == "Market Query":
            row["time_bucket"] = datetime(2025, 3, 31, 23, tzinfo=timezone.utc)
        if name == "News Query":
            row.update(id=f"{number:02x}", title="Fixture headline", publishedDate="2024-11-18 13:30:23", path=f"{number:02x}.html", synopsis="Fixture summary")
        rows.append(row)
    return columns, rows


def test_real_shape_summary_records_ordered_hash_columns_ids_and_nulls():
    columns, rows = make_rows()
    rows[0]["publisher"] = None
    result = harness.summarize_rows("News Query", "AAPL", columns, rows)
    encoded = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    assert result["rows_sha256"] == hashlib.sha256(encoded).hexdigest()
    assert result["columns"] == columns
    assert result["null_counts"]["publisher"] == 1
    assert result["news_ids"] == [f"{number:02x}" for number in range(8)]
    assert all(key.endswith(f"/{number:02x}.png") for number, key in enumerate(result["news_private_image_keys"]))
    reversed_result = harness.summarize_rows("News Query", "AAPL", columns, list(reversed(rows)))
    assert result["rows_sha256"] != reversed_result["rows_sha256"]


@pytest.mark.parametrize("mutation", ["row_count", "foreign_ticker", "empty_synopsis", "duplicate_id", "missing_column", "nonfinite"])
def test_invalid_result_does_not_count_as_sql_acceptance(mutation):
    columns, rows = make_rows()
    if mutation == "row_count":
        rows.pop()
    elif mutation == "foreign_ticker":
        rows[0]["symbol"] = "MSFT"
    elif mutation == "empty_synopsis":
        rows[0]["synopsis"] = ""
    elif mutation == "duplicate_id":
        rows[0]["id"] = rows[1]["id"]
    elif mutation == "missing_column":
        columns.remove("title")
        for row in rows:
            del row["title"]
    elif mutation == "nonfinite":
        rows[0]["recency_rank"] = float("nan")
    with pytest.raises(harness.IntakeSQLError):
        harness.summarize_rows("News Query", "AAPL", columns, rows)


def test_expected_join_nulls_are_not_reclassified_as_corruption():
    columns, rows = make_rows("Insider Query", "NVDA")
    rows[0]["sentiment_avg_mspr"] = None
    result = harness.summarize_rows("Insider Query", "NVDA", columns, rows)
    assert result["row_count"] == 10
    assert result["null_counts"]["sentiment_avg_mspr"] == 1


@pytest.mark.parametrize("invalid", ["id-0", "../01", "AB", "a" * 65])
def test_news_ids_use_adapter_opaque_hex_contract(invalid):
    from analytics_agent.demo_intake import DemoIntakeError
    columns, rows = make_rows()
    rows[0]["id"] = invalid
    with pytest.raises(DemoIntakeError):
        harness.summarize_rows("News Query", "AAPL", columns, rows)


@pytest.mark.parametrize("name,key,value", [("Fundamentals Query", "report_date", "2025-03-01"), ("Insider Query", "month_start", "2025-03-01"), ("Market Query", "time_bucket", datetime(2025, 3, 1))])
def test_dates_must_have_expected_database_types(name, key, value):
    columns, rows = make_rows(name)
    rows[0][key] = value
    with pytest.raises(harness.IntakeSQLError):
        harness.summarize_rows(name, "AAPL", columns, rows)


def test_temporal_and_decimal_serialization_is_explicit():
    assert harness.json_value({"date": date(2025, 3, 1), "number": Decimal("1.25")}) == {"date": "2025-03-01", "number": "1.25"}
    with pytest.raises(harness.IntakeSQLError, match="non-finite"):
        harness.json_value(Decimal("NaN"))


def cli_args():
    return ["--original", "/original", "--dump", "/dump", "--pg-bin", "/pg-bin", "--case", "/AAPL.json", "/AAPL-manifest.json", "--case", "/NVDA.json", "/NVDA-manifest.json"]


def test_no_existing_connection_target_arguments_are_accepted():
    with pytest.raises(SystemExit) as exc:
        harness.main(cli_args() + ["--host", "external.example"])
    assert exc.value.code == 2


def stub_preflight(monkeypatch):
    cases = [{"ticker": ticker, "queries": {name: "WITH fixture AS (SELECT 1) SELECT * FROM fixture" for name in harness.EXPECTED_COUNTS}, "adapted_sha256": "a" * 64, "manifest_sha256": "b" * 64} for ticker in ("AAPL", "NVDA")]
    monkeypatch.setattr(harness, "load_cases", lambda *args: cases)
    monkeypatch.setattr(harness, "verify_dump", lambda *args: harness.DUMP_SHA256)
    monkeypatch.setattr(harness, "verify_programs", lambda *args: None)


def test_default_dry_run_never_creates_a_cluster(monkeypatch, capsys):
    stub_preflight(monkeypatch)
    def forbidden(*args):
        raise AssertionError("dry-run created a cluster")
    monkeypatch.setattr(harness, "OwnedCluster", forbidden)
    assert harness.main(cli_args()) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["stage"] == "dry_run" and result["ok"] is True
    assert result["runtime_blocked"] is True
    assert result["workflow_executed"] is False and result["model_executed"] is False
    assert "retained_private_cluster" not in result


@pytest.mark.parametrize("fail_where", ["start", "queries", "cleanup"])
def test_failure_preserves_exit_status_and_always_attempts_exact_owned_cleanup(monkeypatch, capsys, fail_where):
    stub_preflight(monkeypatch)
    calls = []
    class FakeCluster:
        root = Path("/private/tmp/cp5105-demo-sql-owned-fixture")
        def __init__(self, *args):
            pass
        def start(self, *args):
            calls.append("start")
            if fail_where == "start":
                raise RuntimeError("start failed")
        def stop(self):
            calls.append("stop")
            if fail_where == "cleanup":
                raise RuntimeError("cleanup failed")
            return {"stopped": True}
    monkeypatch.setattr(harness, "OwnedCluster", FakeCluster)
    def queries(*args):
        calls.append("queries")
        if fail_where == "queries":
            raise RuntimeError("query failed")
        return {"fixture": True}
    monkeypatch.setattr(harness, "run_queries", queries)
    assert harness.main(cli_args() + ["--execute-isolated"]) == 1
    result = json.loads(capsys.readouterr().out)
    assert calls[-1] == "stop" and calls.count("stop") == 1
    assert result["ok"] is False
    assert result["retained_private_cluster"] == str(FakeCluster.root)
    assert result["cleanup"]["stopped"] is (fail_where != "cleanup")


def test_existing_pgdata_collision_refused_before_any_command(tmp_path, monkeypatch):
    cluster = object.__new__(harness.OwnedCluster)
    cluster.data = tmp_path.resolve() / "already-there"
    cluster.data.mkdir()
    monkeypatch.setattr(cluster, "verify_owned", lambda: None)
    with pytest.raises(harness.IntakeSQLError, match="PGDATA collision"):
        cluster.start(Path("/not-read"))


def test_manifest_must_remain_model_blocked(tmp_path, monkeypatch):
    import analytics_agent.demo_intake as adapter
    root = tmp_path.resolve()
    original = root / "original.json"
    original.write_bytes(b"fixture original")
    pairs = []
    for ticker in ("AAPL", "NVDA"):
        workflow = root / f"{ticker}.json"
        workflow.write_text("{}")
        manifest = root / f"{ticker}.manifest.json"
        manifest.write_text(json.dumps({"runtime_blocked": False, "fixed_inputs": {"ticker": ticker, "start_date": harness.DATE}}))
        pairs.append([str(workflow), str(manifest)])
    monkeypatch.setattr(adapter, "verified_sql_queries", lambda *args: {name: "WITH fixture AS (SELECT 1) SELECT * FROM fixture" for name in harness.EXPECTED_COUNTS})
    with pytest.raises(harness.IntakeSQLError, match="must keep runtime/models blocked"):
        harness.load_cases(original, pairs)


def test_optional_pinned_original_artifact_integration(tmp_path):
    """Explicit opt-in only; verifies actual adapter→harness bytes, still no PG."""
    source = os.environ.get("CP5105_DEMO_ORIGINAL")
    if source is None:
        pytest.skip("set CP5105_DEMO_ORIGINAL to opt in to pinned parent-artifact integration")
    from analytics_agent.demo_intake import adapt_workflow
    original = Path(source)
    original_bytes = original.read_bytes()
    pairs = []
    for ticker in ("AAPL", "NVDA"):
        adapted = adapt_workflow(original_bytes, ticker=ticker, start_date=harness.DATE)
        workflow = tmp_path.resolve() / f"{ticker}.json"
        manifest = tmp_path.resolve() / f"{ticker}.manifest.json"
        workflow.write_bytes(adapted.workflow_json)
        manifest.write_bytes(adapted.manifest_json)
        pairs.append([str(workflow), str(manifest)])
    cases = harness.load_cases(original, pairs)
    assert [case["ticker"] for case in cases] == ["AAPL", "NVDA"]
    assert all(set(case["queries"]) == set(harness.EXPECTED_COUNTS) for case in cases)
