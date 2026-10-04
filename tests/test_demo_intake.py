"""Pure helper tests plus explicitly optional pinned-original integration.

The owner-supplied workflow is intentionally not vendored into a code repo.
Set CP5105_TRADING_WORKFLOW, or keep it in the parent CP5105 directory, to run
integration cases. Missing source causes an explicit integration-only skip.
"""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat

import pytest

from analytics_agent import demo_intake as intake


@pytest.fixture(scope="module")
def original() -> bytes:
    path = Path(os.environ.get("CP5105_TRADING_WORKFLOW", str(
        Path(__file__).resolve().parents[2] / "trading-agent.json"
    )))
    if not path.is_file():
        pytest.skip("pinned-original integration requires CP5105_TRADING_WORKFLOW or CP5105/trading-agent.json")
    raw = path.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == intake.ORIGINAL_SHA256
    return raw


def _diff(left, right, pointer=""):
    if type(left) is not type(right):
        return {pointer}
    if isinstance(left, dict):
        result = set()
        for key in set(left) | set(right):
            path = pointer + "/" + key.replace("~", "~0").replace("/", "~1")
            result |= {path} if key not in left or key not in right else _diff(left[key], right[key], path)
        return result
    if isinstance(left, list):
        if len(left) != len(right):
            return {pointer}
        return set().union(*(_diff(a, b, f"{pointer}/{i}") for i, (a, b) in enumerate(zip(left, right))))
    return set() if left == right else {pointer}


@pytest.mark.parametrize("ticker", ["aapl", " AAPL", "MSFT", "AAPL' OR TRUE --", "ＡＡＰＬ", None, True, [], {}])
def test_ticker_authority_is_closed(ticker):
    with pytest.raises(intake.DemoIntakeError, match="^TICKER_NOT_APPROVED$"):
        intake._validate_inputs(ticker, intake.DEFAULT_START_DATE)


@pytest.mark.parametrize("value", ["20250301", "2025-3-01", "2025-02-29", "2025-03-01\n", "２０２５-03-01", "2025-03-01'--", None, 20250301, []])
def test_date_is_strict_iso_calendar(value):
    with pytest.raises(intake.DemoIntakeError, match="^DATE_INVALID$"):
        intake._validate_inputs("AAPL", value)


@pytest.mark.parametrize("value", ["2023-09-06", "2025-04-01", "2026-03-01"])
def test_date_outside_observed_range_refused(value):
    with pytest.raises(intake.DemoIntakeError, match="^DATE_OUTSIDE_OBSERVED_OHLC$"):
        intake._validate_inputs("NVDA", value)


@pytest.mark.parametrize("ticker", ["AAPL", "NVDA"])
@pytest.mark.parametrize("value", ["2023-09-07", "2024-02-29", "2025-03-01", "2025-03-31"])
def test_trusted_input_controls(ticker, value):
    intake._validate_inputs(ticker, value)


@pytest.mark.parametrize("raw", [None, {}, "{}", bytearray(b"{}")])
def test_source_requires_bytes(raw):
    with pytest.raises(intake.DemoIntakeError, match="^SOURCE_TYPE$"):
        intake.adapt_workflow(raw, ticker="AAPL")


@pytest.mark.parametrize("raw", [b"", b"{}", b"x" * 53_709])
def test_source_hash_not_caller_claim(raw):
    with pytest.raises(intake.DemoIntakeError, match="^SOURCE_FINGERPRINT$"):
        intake.adapt_workflow(raw, ticker="AAPL")


@pytest.mark.parametrize("value", [None, [], {}, {"nodes": [], "connections": {}, "meta": {}, "pinData": {}},
                                    {"nodes": [{}] * 31, "connections": {}, "meta": {}, "pinData": {}}])
def test_shape_precheck_refuses_malformed_helpers(value):
    with pytest.raises(intake.DemoIntakeError, match="^SOURCE_SHAPE$"):
        intake._validate_source_shape(value)


@pytest.mark.parametrize("raw", [b"[]", b'{"x":1,"x":2}', b'{"x":NaN}', b'\xff', b"{" * 3000])
def test_manifest_json_gate_is_strict(raw):
    with pytest.raises(intake.DemoIntakeError, match="^MANIFEST_INVALID$"):
        intake._parse(raw, "MANIFEST_INVALID")


@pytest.mark.parametrize("news_id", ["", "../a", "a/b", "a%2fb", "a?x", "A" * 32, "a" * 65, "lumilake-public/x", None, 1, []])
def test_key_cannot_override_namespace(news_id):
    with pytest.raises(intake.DemoIntakeError, match="^NEWS_ID_INVALID$"):
        intake.private_image_key(news_id)


def test_opaque_id_preserved_and_private_key_routing():
    news_id = "91c0fc6bad6f4330b6bf33748dd2cccc"  # Actual variable-length archive ID.
    key = intake.private_image_key(news_id)
    assert key == "intake/zhengyuan-demo/0add591f7145/demo/unstructured/news-images/" + news_id + ".png"
    assert key.split("/", 1)[0] == "intake"
    assert intake.PRIVATE_BUCKET == "lumilake-private"
    assert intake.private_image_key("a") != intake.private_image_key("0a")


@pytest.mark.parametrize("ticker", ["AAPL", "NVDA"])
def test_exact_original_integration_deterministic_and_whitelisted(original, ticker):
    result = intake.adapt_workflow(original, ticker=ticker)
    assert result == intake.adapt_workflow(original, ticker=ticker)
    assert result.workflow_json.endswith(b"\n") and result.manifest_json.endswith(b"\n")
    before, after = json.loads(original), json.loads(result.workflow_json)
    manifest = json.loads(result.manifest_json)
    expected_paths = {f"/nodes/{i}/parameters/query" for i in range(25, 29)} | {"/nodes/29/parameters/bucketName"}
    assert _diff(before, after) == expected_paths
    assert {c["pointer"] for c in manifest["changes"]} == expected_paths
    replay = copy.deepcopy(before)
    for change in manifest["changes"]:
        target = replay
        parts = change["pointer"].split("/")[1:]
        for part in parts[:-1]:
            target = target[int(part)] if isinstance(target, list) else target[part]
        assert target[parts[-1]] == change["old_value"]
        target[parts[-1]] = change["new_value"]
    assert replay == after
    assert len(after["nodes"]) == 31
    assert after["connections"] == before["connections"]
    assert after["nodes"][1] == before["nodes"][1]  # Planner and prompts retained.
    assert after["nodes"][2:8] == before["nodes"][2:8]  # All six model configs.
    assert manifest["adapted_sha256"] == hashlib.sha256(result.workflow_json).hexdigest()
    assert manifest["parser_payload_sha256"] == hashlib.sha256(result.parser_payload_json).hexdigest()
    assert manifest["archive_sha256"] == intake.ARCHIVE_SHA256
    assert manifest["original_sha256"] == intake.ORIGINAL_SHA256
    assert manifest["runtime_blocked"] is True
    assert manifest["claims"]["runtime_success"] is False
    assert manifest["claims"]["end_to_end_success"] is False
    assert manifest["sql_semantics"]["planner_output_used_by_sql"] is False
    assert manifest["recorded_data_coverage"]["rechecked_by_adapter"] is False
    assert manifest["storage"]["declassification_authorized"] is False


@pytest.mark.parametrize("ticker", ["AAPL", "NVDA"])
def test_four_pinned_queries_and_parser_input(original, ticker):
    result = intake.adapt_workflow(original, ticker=ticker)
    queries = intake.verified_sql_queries(original, result.workflow_json, result.manifest_json)
    assert set(queries) == {"Fundamentals Query", "Insider Query", "Market Query", "News Query"}
    assert sum(q.count(f"symbol = '{ticker}'") for q in queries.values()) == 10
    assert sum(q.count("FROM demo.") for q in queries.values()) == 10
    assert sum(q.count("'2025-03-01'") for q in queries.values()) == 3
    assert not any(t in q for q in queries.values() for t in ("star.", "{{", "}}", "$("))
    payload = intake.build_parser_payload(original, ticker=ticker)
    assert payload == json.loads(result.parser_payload_json)
    assert payload["graphs"][0]["inputs"] == {"Stock": [ticker]}
    assert payload["graphs"][0]["workflow"] == json.loads(result.workflow_json)
    assert "data" not in payload  # Parser contract, NOT the HTTP submit contract.
    assert json.loads(result.manifest_json)["parser_contract"]["is_http_submission_body"] is False
    with pytest.raises(TypeError):
        intake.build_parser_payload(original, ticker=ticker, inputs={"Stock": ["OTHER"]})


def test_date_variants_are_distinct_not_runtime_overrides(original):
    a = intake.adapt_workflow(original, ticker="AAPL")
    b = intake.adapt_workflow(original, ticker="AAPL", start_date="2025-03-02")
    assert a != b
    assert _diff(json.loads(a.workflow_json), json.loads(b.workflow_json)) == {
        "/nodes/26/parameters/query", "/nodes/27/parameters/query"}


def test_changed_original_rejected_even_whitespace(original):
    with pytest.raises(intake.DemoIntakeError, match="SOURCE_FINGERPRINT"):
        intake.adapt_workflow(original + b"\n", ticker="AAPL")


@pytest.mark.parametrize("mutation", ["node_id", "node_type", "sql", "s3", "connections"])
def test_expected_original_field_guards(original, mutation):
    workflow = json.loads(original)
    if mutation == "node_id": workflow["nodes"][25]["id"] = "different"
    elif mutation == "node_type": workflow["nodes"][25]["type"] = "custom"
    elif mutation == "sql": workflow["nodes"][25]["parameters"]["query"] += " "
    elif mutation == "s3": workflow["nodes"][29]["parameters"]["options"]["folderKey"] = "../escape"
    else: workflow["connections"] = {}
    with pytest.raises(intake.DemoIntakeError):
        intake._validate_source_shape(workflow)


@pytest.mark.parametrize("mutation", ["sql_rehashed", "runtime", "archive", "ticker", "parser_hash", "private_bucket"])
def test_artifact_authority_cannot_be_self_reported(original, mutation):
    result = intake.adapt_workflow(original, ticker="AAPL")
    workflow = json.loads(result.workflow_json)
    manifest = json.loads(result.manifest_json)
    if mutation == "sql_rehashed":
        workflow["nodes"][25]["parameters"]["query"] = "SELECT 'tampered'"
        manifest["adapted_sha256"] = hashlib.sha256(intake._serialize(workflow)).hexdigest()
    elif mutation == "runtime": manifest["runtime_blocked"] = False
    elif mutation == "archive": manifest["archive_sha256"] = "0" * 64
    elif mutation == "ticker": manifest["fixed_inputs"]["ticker"] = "NVDA"
    elif mutation == "parser_hash": manifest["parser_payload_sha256"] = "0" * 64
    else: manifest["storage"]["physical_bucket_intent"] = "lumilake-public"
    with pytest.raises(intake.DemoIntakeError, match="ARTIFACT_MISMATCH"):
        intake.verified_sql_queries(original, intake._serialize(workflow), intake._serialize(manifest))


def test_cross_artifact_and_noncanonical_bytes_rejected(original):
    a = intake.adapt_workflow(original, ticker="AAPL")
    b = intake.adapt_workflow(original, ticker="NVDA")
    for workflow, manifest in ((a.workflow_json, b.manifest_json),
                               (a.workflow_json.rstrip(), a.manifest_json),
                               (a.workflow_json, b"{}")):
        with pytest.raises(intake.DemoIntakeError):
            intake.verified_sql_queries(original, workflow, manifest)


@pytest.fixture
def cli():
    path = Path(__file__).resolve().parents[1] / "scripts/adapt_zhengyuan_demo.py"
    spec = importlib.util.spec_from_file_location("adapt_zhengyuan_demo_cli", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.main


def test_cli_bundle_is_private_and_no_clobber(original, cli, tmp_path):
    source = tmp_path / "original.json"
    source.write_bytes(original)
    target = tmp_path / "bundle"
    args = ["--original", str(source), "--ticker", "NVDA", "--output-directory", str(target)]
    assert cli(args) == 0
    assert stat.S_IMODE(target.stat().st_mode) == 0o700
    files = {p.name: p.read_bytes() for p in target.iterdir()}
    assert set(files) == {"workflow.json", "manifest.json", "parser-payload.json"}
    assert all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in target.iterdir())
    with pytest.raises(SystemExit) as exc:
        cli(args)
    assert exc.value.code == 1
    assert files == {p.name: p.read_bytes() for p in target.iterdir()}
    assert source.read_bytes() == original


def test_cli_rejects_symlink_and_bad_source_without_output(original, cli, tmp_path):
    source = tmp_path / "original.json"
    source.write_bytes(original)
    target = tmp_path / "link"
    target.symlink_to(tmp_path / "absent")
    args = ["--original", str(source), "--ticker", "AAPL", "--output-directory", str(target)]
    with pytest.raises(SystemExit): cli(args)
    assert target.is_symlink() and not (tmp_path / "absent").exists()
    source.write_bytes(b"{}")
    target = tmp_path / "new"
    with pytest.raises(SystemExit):
        cli(["--original", str(source), "--ticker", "AAPL", "--output-directory", str(target)])
    assert not target.exists()
