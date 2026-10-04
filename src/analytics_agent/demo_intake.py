"""Deterministic, offline adaptation of one reviewed authored n8n workflow.

This is not the Phase 2 catalog, a JobSpec generator, or an execution harness.
Only the byte-pinned original is accepted. No function performs I/O. Artifact
hashes identify content, not a signature or authorization to run a workflow.
"""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any

ORIGINAL_SHA256 = "ff4dc3f9467cfe35da9514d10ffbb9baae26abec88525dbd5faafb56d4d9791d"
ARCHIVE_SHA256 = "0add591f714587f35c85bfddc81b25f60a464167d9443fdc288b5c4201243e36"
DEFAULT_START_DATE = "2025-03-01"
ALLOWED_TICKERS = ("AAPL", "NVDA")
PRIVATE_BUCKET = "lumilake-private"
IMAGE_PREFIX = f"intake/zhengyuan-demo/{ARCHIVE_SHA256[:12]}/demo/unstructured/news-images"
MANIFEST_VERSION = "zhengyuan-demo-adaptation/v1"

_STOCK_EXPR = "{{ $('Stock') }}"
_DATE_EXPR = "{{ $('SQL Date Planner').item.start_date }}"
_S3_FOLDER_KEY = "=${{ $('News Query').item.id }}.png"
_CHAIN = "@n8n/n8n-nodes-langchain.chainLlm"
_MODEL = "@n8n/n8n-nodes-langchain.lmOpenHuggingFaceInference"
_CONNECTIONS_SHA256 = "d4dbe32543f8764713447e673feb1ffac66b9e3156b2220467bfe964e7cc499b"

# Each query was read in full. Replacements are confined to these exact fields
# and qualified FROM tokens, never to arbitrary strings elsewhere in a workflow.
_SQL_NODES = (
    (25, "d777dd07-ac6f-40fe-8316-c016e68cbb4b", "Fundamentals Query",
     "31f840d9785d292a8906053dd602b6cca06c8036191a81ab24cdce2830390f6d", 5, 0,
     ("d_instrument_profile_fmp", "bridge_instrument_peer",
      "fact_financials_std_fmp__fmp_income_statement",
      "fact_financials_std_fmp__fmp_sheet_statement",
      "fact_financials_std_fmp__fmp_flow_statement")),
    (26, "41b3f245-cca0-471f-acb8-4cd4eb8039b2", "Insider Query",
     "13f98f4a63d6bc429a4a349946b9793b2e23eae9eae6c43b5d28be11670bf682", 2, 2,
     ("fact_insider_sentiment_finnhub", "fact_insider_tx_finnhub")),
    (27, "35c8ad17-a47b-4842-afaa-a5ad2446b093", "Market Query",
     "ec098ca5f134c0667df85250e144d54b4bf78f54ddaf99e3cda5c4ee2d94f092", 2, 1,
     ("fact_ohlc_10m", "fact_price_metrics_finnhub")),
    (28, "1b0b017f-34a1-4c2a-b2a3-76119a44477a", "News Query",
     "25a24d00e458397c40956b362bad3bb50d4c9fd90c386d99900e5c35a9f6fff4", 1, 0,
     ('"fact_news-metadata"',)),
)


class DemoIntakeError(ValueError):
    """Payload-free refusal with a stable code."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class AdaptedDemo:
    """Immutable serialized artifacts; callers cannot silently mutate a dict."""

    workflow_json: bytes
    manifest_json: bytes
    parser_payload_json: bytes


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _serialize(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2,
                       allow_nan=False) + "\n").encode("utf-8")


def _compact_sha(value: Any) -> str:
    return _sha(json.dumps(value, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"), allow_nan=False).encode("utf-8"))


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(_: str) -> None:
    raise ValueError("non-JSON number")


def _parse(raw: bytes, code: str) -> dict[str, Any]:
    if type(raw) is not bytes or len(raw) > 2_000_000:
        raise DemoIntakeError(code)
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object,
                           parse_constant=_reject_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise DemoIntakeError(code) from None
    if not isinstance(value, dict):
        raise DemoIntakeError(code)
    return value


def _validate_inputs(ticker: str, start_date: str) -> None:
    if type(ticker) is not str or ticker not in ALLOWED_TICKERS:
        raise DemoIntakeError("TICKER_NOT_APPROVED")
    if type(start_date) is not str or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", start_date):
        raise DemoIntakeError("DATE_INVALID")
    try:
        dt.date.fromisoformat(start_date)
    except ValueError:
        raise DemoIntakeError("DATE_INVALID") from None
    if not "2023-09-07" <= start_date <= "2025-03-31":
        raise DemoIntakeError("DATE_OUTSIDE_OBSERVED_OHLC")


def _validate_source_shape(workflow: dict[str, Any]) -> None:
    """Defense-in-depth checks behind the immutable original-byte authority."""
    if not isinstance(workflow, dict) or set(workflow) != {"nodes", "connections", "pinData", "meta"}:
        raise DemoIntakeError("SOURCE_SHAPE")
    nodes = workflow["nodes"]
    if not isinstance(nodes, list) or len(nodes) != 31:
        raise DemoIntakeError("SOURCE_SHAPE")
    for node in nodes:
        if not isinstance(node, dict) or not isinstance(node.get("parameters"), dict):
            raise DemoIntakeError("SOURCE_SHAPE")
        if any(type(node.get(k)) is not str or not node[k] for k in ("id", "name", "type")):
            raise DemoIntakeError("SOURCE_SHAPE")
    if len({n["id"] for n in nodes}) != 31 or len({n["name"] for n in nodes}) != 31:
        raise DemoIntakeError("SOURCE_SHAPE")
    if Counter(n["type"] for n in nodes) != Counter({
        _CHAIN: 18, _MODEL: 6, "n8n-nodes-base.postgres": 4,
        "n8n-nodes-base.s3": 1, "n8n-nodes-base.code": 1,
        "@n8n/n8n-nodes-langchain.chatTrigger": 1,
    }):
        raise DemoIntakeError("SOURCE_SHAPE")
    if not isinstance(workflow["connections"], dict) or _compact_sha(workflow["connections"]) != _CONNECTIONS_SHA256:
        raise DemoIntakeError("SOURCE_CONNECTIONS_CHANGED")
    for index, node_id, name, digest, stocks, dates, tables in _SQL_NODES:
        node = nodes[index]
        params = node["parameters"]
        query = params.get("query")
        if (node["id"], node["name"], node["type"], node.get("typeVersion")) != (
            node_id, name, "n8n-nodes-base.postgres", 2.6
        ) or params.get("operation") != "executeQuery" or params.get("options") != {}:
            raise DemoIntakeError("SOURCE_SQL_NODE_CHANGED")
        if type(query) is not str or _sha(query.encode("utf-8")) != digest:
            raise DemoIntakeError("SOURCE_SQL_CHANGED")
        if (query.count(_STOCK_EXPR), query.count(_DATE_EXPR), query.count("star.")) != (stocks, dates, len(tables)):
            raise DemoIntakeError("SOURCE_SQL_COUNTS")
        if any(query.count("FROM star." + table) != 1 for table in tables):
            raise DemoIntakeError("SOURCE_TABLE_CHANGED")
    s3 = nodes[29]
    if (s3["id"], s3["name"], s3["type"], s3.get("typeVersion")) != (
        "46b2f328-1897-41d7-9048-bd49962199ea", "News Artifact Query", "n8n-nodes-base.s3", 1
    ) or s3["parameters"] != {
        "resource": "folder", "operation": "getAll", "bucketName": "unstructured/news-images",
        "limit": None, "options": {"folderKey": _S3_FOLDER_KEY},
    }:
        raise DemoIntakeError("SOURCE_S3_CHANGED")


def _load_source(original: bytes) -> dict[str, Any]:
    if type(original) is not bytes:
        raise DemoIntakeError("SOURCE_TYPE")
    if len(original) != 53_709 or _sha(original) != ORIGINAL_SHA256:
        raise DemoIntakeError("SOURCE_FINGERPRINT")
    workflow = _parse(original, "SOURCE_JSON")
    _validate_source_shape(workflow)
    return workflow


def private_image_key(news_id: str) -> str:
    """Map bounded opaque hex verbatim; archive membership is a separate gate.

    Observed IDs have variable lengths. Do not pad, rehash, or infer UUIDs.
    A syntactically valid ID is not evidence that an object exists.
    """
    if type(news_id) is not str or not re.fullmatch(r"[0-9a-f]{1,64}", news_id):
        raise DemoIntakeError("NEWS_ID_INVALID")
    return f"{IMAGE_PREFIX}/{news_id}.png"


def _parser_payload(workflow: dict[str, Any], ticker: str, start_date: str) -> dict[str, Any]:
    return {"graphs": [{"name": f"zhengyuan-demo-{ticker.lower()}-{start_date}",
                        "workflow": workflow, "inputs": {"Stock": [ticker]}}]}


def adapt_workflow(original: bytes, *, ticker: str,
                   start_date: str = DEFAULT_START_DATE) -> AdaptedDemo:
    """Generate an independent snapshot variant, never execute or modify input.

    The date is a lower bound, not an as-of cutoff. SQL ignores runtime Stock
    and Planner values, while preserved prompts still require Stock=[ticker].
    Manual runtime input overrides are NOT made safe by this adapter.
    """
    _validate_inputs(ticker, start_date)
    source = _load_source(original)
    workflow = copy.deepcopy(source)
    changes = []
    for index, node_id, name, _, stocks, dates, tables in _SQL_NODES:
        old = source["nodes"][index]["parameters"]["query"]
        new = old
        for table in tables:
            new = new.replace("FROM star." + table, "FROM demo." + table, 1)
        new = new.replace(_STOCK_EXPR, ticker).replace(_DATE_EXPR, start_date)
        if any(token in new for token in ("star.", "{{", "}}", "$(")):
            raise DemoIntakeError("RESIDUAL_SQL_EXPRESSION")
        workflow["nodes"][index]["parameters"]["query"] = new
        changes.append({"pointer": f"/nodes/{index}/parameters/query", "node_id": node_id,
                        "node_name": name, "old_value": old, "new_value": new,
                        "schema_replacements": len(tables), "ticker_replacements": stocks,
                        "date_replacements": dates})
    workflow["nodes"][29]["parameters"]["bucketName"] = IMAGE_PREFIX
    changes.append({"pointer": "/nodes/29/parameters/bucketName",
                    "node_id": source["nodes"][29]["id"], "node_name": "News Artifact Query",
                    "old_value": "unstructured/news-images", "new_value": IMAGE_PREFIX})
    workflow_json = _serialize(workflow)
    parser_json = _serialize(_parser_payload(workflow, ticker, start_date))
    models = [{"node_id": node["id"], "name": node["name"], "parameters": node["parameters"]}
              for node in source["nodes"] if node["type"] == _MODEL]
    manifest = {
        "manifest_version": MANIFEST_VERSION,
        "artifact_kind": "authored-n8n-workflow-snapshot-adaptation-not-jobspec",
        "original_sha256": ORIGINAL_SHA256, "archive_sha256": ARCHIVE_SHA256,
        "adapted_sha256": _sha(workflow_json), "parser_payload_sha256": _sha(parser_json),
        "serialization": "UTF-8 JSON; sorted keys; indent=2; LF including final LF; no NaN",
        "provenance": {
            "original_name": "trading-agent.json", "original_bytes": 53_709,
            "archive_name": "lumilake-demo-data.tar.gz", "archive_bytes": 681_293_896,
            "archive_verification": "Pinned from 2026-09-07 intake evidence; adapter does not read archive",
            "archive_checksum_is_upstream_signature": False,
            "evidence_document": "docs/zhengyuan-workflow-data-intake-20260907.md#9",
        },
        "fixed_inputs": {"ticker": ticker, "start_date": start_date,
                         "n8n_inputs": {"Stock": [ticker]}},
        "changes": changes,
        "preservation": {"node_count": 31, "connections_sha256": _CONNECTIONS_SHA256,
                         "all_other_json_values_unchanged": True, "model_nodes": models,
                         "prompts_connections_and_planner_preserved": True},
        "sql_semantics": {
            "schema": {"from": "star", "to": "demo"},
            "ticker": "Validated literal in all four queries; no runtime SQL input interpolation",
            "planner_output_used_by_sql": False,
            "planner_node_retained": True,
            "date": "Fixed lower bound in Market; Insider subtracts 24 months; not an upper/as-of cutoff",
            "fundamentals_and_news_have_date_filter": False,
            "other_prompts_still_read_stock": True,
        },
        "storage": {"partition": "private", "physical_bucket_intent": PRIVATE_BUCKET,
                    "full_key_template": IMAGE_PREFIX + "/{id}.png",
                    "archive_member_template": "s3/demo/unstructured/news-images/{id}.png",
                    "folder_key_expression_preserved": _S3_FOLDER_KEY,
                    "content_type_intent": "image/png", "object_upload_or_get_performed": False,
                    "routing": "Keep complete intake/... key; no global gateway routing change",
                    "news_id_contract": "Opaque 1..64 lowercase hex characters, unchanged; future harness must enforce actual archive membership",
                    "declassification_authorized": False},
        "recorded_data_coverage": {
            "evidence_date": "2026-09-07", "rechecked_by_adapter": False,
            "tables": 10, "required_table_column_references": 88,
            "copy_rows": 2_143_987, "symbols": 100, "news_png_pairs": 756,
            "focused_eligible_news_png_matches": 8,
            "focused_ohlc_utc": ["2023-09-07T08:00:01Z", "2025-03-31T23:50:01Z"],
            "news_naive_datetime_ranges": {
                "AAPL": ["2024-11-18 13:30:23", "2024-11-19 04:01:43"],
                "NVDA": ["2025-05-02 08:11:00", "2025-05-02 17:15:00"],
            },
            "interpretation": "Asynchronous historical snapshot only; no common as-of, continuity, or backtest claim",
        },
        "parser_contract": {
            "api": "lumilake_server.parser.n8n.parse_n8n_payload",
            "is_http_submission_body": False,
            "future_http_gate": "JobSubmitRequest needs data/workflow-string/output_location and Workflow-Format:n8n; not constructed here",
            "override_warning": "Future harness must enforce artifact hashes and fixed Stock inputs; manual overrides are not safe",
        },
        "runtime_blocked": True,
        "runtime_gates": [
            "Separate isolated SQL/type/NULL/UTC/permissions and PNG GET/Content-Type acceptance",
            "Enforce archive identity, private full keys, news-ID grammar and fixed parser Stock inputs",
            "Resolve topK=1 loss in current parser; source model options are unchanged",
            "Pin immutable model/processor revisions; current runtime uses revision=main",
            "Model license/auth/cache validity and actual cache environment remain unverified",
            "Original Gemma 27B/LLaVA 7B allocation, precision, context, peak VRAM and lifecycle need approval",
            "Validate LLaVA two-stage execution and long-image center-crop information loss",
            "Snapshot smoke-test vs unified as-of experiment is a separate owner decision",
        ],
        "claims": {"sql_executed": False, "runtime_success": False,
                   "end_to_end_success": False, "jobspec_success": "not_applicable",
                   "benchmark_or_paper_replication": False},
    }
    return AdaptedDemo(workflow_json, _serialize(manifest), parser_json)


def build_parser_payload(original: bytes, *, ticker: str,
                         start_date: str = DEFAULT_START_DATE) -> dict[str, Any]:
    """Build only the maintained parser's graphs wrapper, with no input override.

    This is NOT a JobSubmitRequest or authorization to preview/submit a job.
    """
    return json.loads(adapt_workflow(original, ticker=ticker, start_date=start_date).parser_payload_json)


def verified_sql_queries(original: bytes, workflow_json: bytes,
                         manifest_json: bytes) -> dict[str, str]:
    """Re-derive against pinned original authority before exposing four queries.

    Never trust a manifest's self-reported hash, ticker or runtime state. Exact
    regenerated artifact bytes must match; this does not authorize SQL execution.
    """
    _load_source(original)
    manifest = _parse(manifest_json, "MANIFEST_INVALID")
    fixed = manifest.get("fixed_inputs")
    if not isinstance(fixed, dict):
        raise DemoIntakeError("MANIFEST_INVALID")
    expected = adapt_workflow(original, ticker=fixed.get("ticker"), start_date=fixed.get("start_date"))
    if type(workflow_json) is not bytes or workflow_json != expected.workflow_json or manifest_json != expected.manifest_json:
        raise DemoIntakeError("ARTIFACT_MISMATCH")
    workflow = json.loads(expected.workflow_json)
    return {name: workflow["nodes"][index]["parameters"]["query"]
            for index, _, name, *_ in _SQL_NODES}
