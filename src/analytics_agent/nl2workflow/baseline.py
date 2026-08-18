"""Rule-based baseline DAG generator (Phase 1).

The baseline maps an NL request to one of six canonical intents (profile,
news, fundamentals, market, trading, compare) via keyword rules and symbol
detection, then instantiates a canonical Lumilake YAML-shaped DAG template.
The ground-truth set in ``nl-requests/ground_truth.json`` encodes the target
structure for each in-scope request; accuracy = structural match (op set +
edge set) between generated and ground-truth DAGs.
"""

from __future__ import annotations

import re
from typing import Any

import yaml

_LLM_CONFIG = {
    "model": "Qwen/Qwen3-8B",
    "max_tokens": 768,
    "temperature": 0.4,
    "top_p": 1,
}

_SYMBOL_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("AAPL", re.compile(r"\bAAPL\b|\bApple\b", re.I)),
    ("NVDA", re.compile(r"\bNVDA\b|\bNVIDIA\b|\bNvidia\b", re.I)),
]

_INTENT_RULES: list[tuple[str, re.Pattern[str]]] = [
    (
        "trading",
        re.compile(
            r"\b(buy|sell|invest|investment|recommend|recommendation|verdict|"
            r"trade|trading|analy[sz]e)\b",
            re.I,
        ),
    ),
    (
        "compare",
        re.compile(r"\b(compare|comparison|versus|vs\.?|both)\b", re.I),
    ),
    (
        "news",
        re.compile(r"\b(news|headline|article|coverage|story)\b", re.I),
    ),
    (
        "profile",
        re.compile(
            r"\b(profile|overview|company info|about|who is|what does)\b", re.I
        ),
    ),
    (
        "fundamentals",
        re.compile(
            r"\b(fundamental|revenue|earnings|income|eps|profit|financial)\b", re.I
        ),
    ),
    (
        "market",
        re.compile(
            r"\b(market|price|prices|ohlc|candle|52-week|52 week|metrics|volume)\b",
            re.I,
        ),
    ),
]

# ── SQL templates (schema: lumilake_demo.*, seeded by the lakehouse stack) ────

_PROFILE_SQL = """\
SELECT symbol, "companyName", sector, industry, "marketCap", beta
FROM lumilake_demo.instrument_profile
WHERE symbol = '{symbol}'
LIMIT 1\
"""

_FUNDAMENTALS_SQL = """\
SELECT
  symbol,
  AVG(revenue) AS avg_revenue,
  AVG("netIncome") AS avg_net_income,
  AVG(eps) AS avg_eps,
  MAX(date::date)::text AS latest_report
FROM lumilake_demo.financial_income_statement
WHERE symbol = '{symbol}'
GROUP BY symbol\
"""

_MARKET_SQL = """\
WITH bars_recent AS (
  SELECT
    symbol,
    MAX(high) AS recent_high,
    MIN(low) AS recent_low,
    AVG(close) AS recent_close_avg,
    SUM(volume) AS total_volume,
    MAX(timestamp)::text AS latest_ts
  FROM (
    SELECT * FROM lumilake_demo.ohlc_10m
    WHERE symbol = '{symbol}'
    ORDER BY timestamp DESC
    LIMIT 390
  ) recent
  GROUP BY symbol
),
metrics AS (
  SELECT symbol, metric::jsonb AS m
  FROM lumilake_demo.market_metrics
  WHERE symbol = '{symbol}'
  ORDER BY version DESC
  LIMIT 1
)
SELECT
  b.symbol,
  b.latest_ts,
  b.recent_high,
  b.recent_low,
  b.recent_close_avg,
  b.total_volume,
  m.m ->> '52WeekHigh'     AS week52_high,
  m.m ->> '52WeekLow'      AS week52_low,
  m.m ->> 'beta'           AS beta,
  m.m ->> 'peTTM'          AS pe_ttm,
  m.m ->> 'epsGrowthTTMYoy' AS eps_growth_ttm
FROM bars_recent b
LEFT JOIN metrics m ON m.symbol = b.symbol\
"""

_NEWS_SQL = """\
SELECT
  symbol,
  STRING_AGG(title, ' | ' ORDER BY "publishedDate" DESC) AS headlines,
  STRING_AGG(
    LEFT(COALESCE(summary, text, ''), 200),
    ' || ' ORDER BY "publishedDate" DESC
  ) AS synopses,
  COUNT(*) AS news_count
FROM (
  SELECT *
  FROM lumilake_demo.news_metadata
  WHERE symbol = '{symbol}'
    AND id IS NOT NULL
  ORDER BY "publishedDate" DESC
  LIMIT 5
) recent
GROUP BY symbol\
"""

# Dated variants used by the trading DAG, where a date-planner stage supplies
# ``start_date`` (mirrors Q2 trading-agent's templates).
_FUNDAMENTALS_SQL_DATED = """\
WITH income_recent AS (
  SELECT
    symbol,
    AVG(revenue) AS avg_revenue,
    AVG("netIncome") AS avg_net_income,
    AVG(eps) AS avg_eps,
    MAX(date::date)::text AS latest_report
  FROM lumilake_demo.financial_income_statement
  WHERE symbol = '{symbol}'
    AND date::date >= '{start_date}'::date
  GROUP BY symbol
)
SELECT
  symbol,
  avg_revenue,
  avg_net_income,
  avg_eps,
  latest_report
FROM income_recent\
"""

_MARKET_SQL_DATED = """\
WITH bars_recent AS (
  SELECT
    symbol,
    MAX(high) AS recent_high,
    MIN(low) AS recent_low,
    AVG(close) AS recent_close_avg,
    SUM(volume) AS total_volume,
    MAX(timestamp)::text AS latest_ts
  FROM lumilake_demo.ohlc_10m
  WHERE symbol = '{symbol}'
    AND timestamp >= '{start_date}'::timestamp
  GROUP BY symbol
),
metrics AS (
  SELECT symbol, metric::jsonb AS m
  FROM lumilake_demo.market_metrics
  WHERE symbol = '{symbol}'
  ORDER BY version DESC
  LIMIT 1
)
SELECT
  b.symbol,
  b.latest_ts,
  b.recent_high,
  b.recent_low,
  b.recent_close_avg,
  b.total_volume,
  m.m ->> '52WeekHigh'     AS week52_high,
  m.m ->> '52WeekLow'      AS week52_low,
  m.m ->> 'beta'           AS beta,
  m.m ->> 'peTTM'          AS pe_ttm,
  m.m ->> 'epsGrowthTTMYoy' AS eps_growth_ttm
FROM bars_recent b
LEFT JOIN metrics m ON m.symbol = b.symbol\
"""

_DATE_PLANNER_MESSAGES = [
    {
        "role": "system",
        "content": (
            "You are a SQL date planner. Produce exactly one structured field "
            "named start_date."
        ),
    },
    {
        "role": "user",
        "content": (
            "Pick a single lookback start_date that yields ~24 months of "
            "trailing data. Return YYYY-MM-DD."
        ),
    },
]


def extract_symbols(text: str) -> list[str]:
    """Return canonical symbols mentioned in ``text``, in order of appearance."""
    hits: list[tuple[int, str]] = []
    for canonical, pattern in _SYMBOL_PATTERNS:
        for match in pattern.finditer(text):
            hits.append((match.start(), canonical))
    hits.sort()
    seen: list[str] = []
    for _, symbol in hits:
        if symbol not in seen:
            seen.append(symbol)
    return seen


def classify_intent(text: str) -> str:
    """First-match keyword intent. Defaults to ``profile``."""
    for intent, pattern in _INTENT_RULES:
        if pattern.search(text):
            return intent
    return "profile"


_START_DATE_PARAM = {
    "label": "start_date",
    "node": "SQL Date Planner",
    "path": "items.output.start_date",
}


def _sql_op(op_id: str, inputs: list[str], sql: str) -> dict[str, Any]:
    # Params are derived from the placeholders actually present in the
    # template, so templates never carry unused parameter bindings.
    params: list[dict[str, Any]] = []
    if "{symbol}" in sql and inputs:
        params.append({"label": "symbol", "node": inputs[0]})
    if "{start_date}" in sql:
        params.append(dict(_START_DATE_PARAM))
    return {
        "id": op_id,
        "op": "DataRetrievalOp",
        "inputs": inputs,
        "data_spec": {
            "type": "lumid",
            "mode": "sql",
            "output_format": "jsonl",
            "template": sql,
            "params": params,
        },
    }


def _llm_op(
    op_id: str,
    inputs: list[str],
    system: str,
    user: str,
    structural_outputs: list[dict] | None = None,
    prompt_template: str | None = None,
    format_kwargs: dict[str, str] | None = None,
    aggregate_table: list[dict] | None = None,
) -> dict[str, Any]:
    op: dict[str, Any] = {
        "id": op_id,
        "op": "LLMChatOp",
        "inputs": inputs,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user if prompt_template is None else ""},
        ],
        "config": dict(_LLM_CONFIG),
    }
    if prompt_template is not None:
        op["prompt"] = {
            "template": prompt_template,
            "format_kwargs": format_kwargs or {},
        }
    if aggregate_table:
        op["aggregate_table"] = aggregate_table
    if structural_outputs:
        op["structural_outputs"] = structural_outputs
    return op


def _table_rows(node: str, fields: list[str]) -> list[dict[str, str]]:
    return [{"label": f, "node": node, "path": f"items.table.{f}"} for f in fields]


def _output_rows(node: str, fields: list[str]) -> list[dict[str, str]]:
    return [{"label": f, "node": node, "path": f"items.output.{f}"} for f in fields]


_THESIS_OUT = [
    {"name": "thesis", "type": "string"},
    {"name": "confidence", "type": "number", "min": 0, "max": 1},
    {"name": "catalysts", "type": "string"},
]
_RISKS_OUT = [
    {"name": "thesis", "type": "string"},
    {"name": "confidence", "type": "number", "min": 0, "max": 1},
    {"name": "risks", "type": "string"},
]
_RISK_ID_OUT = [
    {"name": "risks", "type": "string"},
    {"name": "severity", "type": "string", "enum": ["low", "medium", "high"]},
]
_SYNTH_OUT = [
    {"name": "balanced_view", "type": "string"},
    {"name": "expected_value", "type": "number"},
    {"name": "guardrails", "type": "string"},
]
_VERDICT_OUT = [
    {"name": "verdict", "type": "string", "enum": ["BUY", "SELL", "HOLD"]},
    {"name": "rationale", "type": "string"},
]

_BULL_DATA = (
    _table_rows(
        "Fundamentals Query",
        ["avg_revenue", "avg_net_income", "avg_eps", "latest_report"],
    )
    + _table_rows(
        "Market Query",
        [
            "recent_high",
            "recent_low",
            "recent_close_avg",
            "total_volume",
            "week52_high",
            "week52_low",
            "beta",
            "pe_ttm",
            "eps_growth_ttm",
        ],
    )
    + _table_rows("News Query", ["headlines", "synopses", "news_count"])
)


def _trading_ops() -> list[dict[str, Any]]:
    return [
        _llm_op(
            "SQL Date Planner",
            ["Stock"],
            _DATE_PLANNER_MESSAGES[0]["content"],
            _DATE_PLANNER_MESSAGES[1]["content"],
            structural_outputs=[
                {
                    "name": "start_date",
                    "type": "datetime",
                    "min": "2023-01-01",
                    "max": "2025-12-31",
                }
            ],
        ),
        _sql_op(
            "Fundamentals Query",
            ["Stock", "SQL Date Planner"],
            _FUNDAMENTALS_SQL_DATED,
        ),
        _sql_op(
            "Market Query",
            ["Stock", "SQL Date Planner"],
            _MARKET_SQL_DATED,
        ),
        _sql_op("News Query", ["Stock"], _NEWS_SQL),
        _llm_op(
            "Bull Researcher",
            ["Stock", "Fundamentals Query", "Market Query", "News Query"],
            "You are the bull analyst. Argue the BUY case using the data aggregated below. Cite specific figures. Address obvious bear counters preemptively.",
            "",
            structural_outputs=_THESIS_OUT,
            prompt_template=(
                "Argue the BUY case for {ref0}. Cite the figures below.\n\n{df}\n\n"
                "Return thesis (paragraph), confidence (0-1), catalysts (comma-separated)."
            ),
            format_kwargs={"ref0": "Stock"},
            aggregate_table=_BULL_DATA,
        ),
        _llm_op(
            "Bear Researcher",
            ["Stock", "Fundamentals Query", "Market Query", "News Query"],
            "You are the bear analyst. Argue the SELL case using the data aggregated below. Cite specific figures. Address obvious bull counters preemptively.",
            "",
            structural_outputs=_RISKS_OUT,
            prompt_template=(
                "Argue the SELL case for {ref0}. Cite the figures below.\n\n{df}\n\n"
                "Return thesis (paragraph), confidence (0-1), risks (comma-separated)."
            ),
            format_kwargs={"ref0": "Stock"},
            aggregate_table=_BULL_DATA,
        ),
        _llm_op(
            "Bull Rebuttal",
            ["Stock", "Bull Researcher", "Bear Researcher"],
            "You are the bull analyst defending the BUY case.",
            "",
            structural_outputs=_THESIS_OUT,
            prompt_template=(
                "Refine the BUY case for {ref0}. Rebut the bear's strongest points "
                "and reinforce the thesis.\n\n{df}\n\nReturn thesis (paragraph), "
                "confidence (0-1), catalysts (comma-separated)."
            ),
            format_kwargs={"ref0": "Stock"},
            aggregate_table=(
                _output_rows("Bull Researcher", ["thesis", "confidence", "catalysts"])
                + _output_rows("Bear Researcher", ["thesis", "confidence", "risks"])
            ),
        ),
        _llm_op(
            "Bear Rebuttal",
            ["Stock", "Bull Researcher", "Bear Researcher"],
            "You are the bear analyst defending the SELL case.",
            "",
            structural_outputs=_RISKS_OUT,
            prompt_template=(
                "Refine the SELL case for {ref0}. Rebut the bull's strongest points "
                "and reinforce the risks.\n\n{df}\n\nReturn thesis (paragraph), "
                "confidence (0-1), risks (comma-separated)."
            ),
            format_kwargs={"ref0": "Stock"},
            aggregate_table=(
                _output_rows("Bull Researcher", ["thesis", "confidence", "catalysts"])
                + _output_rows("Bear Researcher", ["thesis", "confidence", "risks"])
            ),
        ),
        _llm_op(
            "Risk Identifier",
            ["Stock", "Bull Rebuttal", "Bear Rebuttal"],
            "You identify investment risks.",
            "",
            structural_outputs=_RISK_ID_OUT,
            prompt_template=(
                "Enumerate the discrete risks raised by the bull and bear cases for "
                "{ref0}.\n\n{df}\n\nReturn risks (comma-separated list) and severity "
                "(low|medium|high)."
            ),
            format_kwargs={"ref0": "Stock"},
            aggregate_table=(
                _output_rows("Bull Rebuttal", ["thesis", "catalysts"])
                + _output_rows("Bear Rebuttal", ["thesis", "risks"])
            ),
        ),
        _llm_op(
            "Risk Synthesis",
            ["Stock", "Bull Rebuttal", "Bear Rebuttal", "Risk Identifier"],
            "You synthesize investment analysis into a balanced view.",
            "",
            structural_outputs=_SYNTH_OUT,
            prompt_template=(
                "Weigh the round-2 bull/bear cases and the identified risks for "
                "{ref0} below.\n\n{df}\n\nReturn balanced_view (paragraph), "
                "expected_value (number; positive = lean BUY, negative = lean SELL), "
                "guardrails (comma-separated stop/hedge conditions)."
            ),
            format_kwargs={"ref0": "Stock"},
            aggregate_table=(
                _output_rows("Bull Rebuttal", ["thesis", "confidence"])
                + _output_rows("Bear Rebuttal", ["thesis", "confidence"])
                + _output_rows("Risk Identifier", ["risks", "severity"])
            ),
        ),
        _llm_op(
            "Fund Manager Verdict",
            ["Stock", "Risk Synthesis"],
            "You are the fund manager. Output the decisive call. Do not default to HOLD unless strongly justified by the synthesis.",
            "",
            structural_outputs=_VERDICT_OUT,
            prompt_template=(
                "You are the fund manager. Read the head-of-risk synthesis for "
                "{ref0} below.\n\n{df}\n\nOutput verdict (BUY|SELL|HOLD) and a "
                "rationale of at most two sentences."
            ),
            format_kwargs={"ref0": "Stock"},
            aggregate_table=_output_rows(
                "Risk Synthesis", ["balanced_view", "expected_value", "guardrails"]
            ),
        ),
    ]


def generate_workflow(nl_request: str, context: dict | None = None) -> dict[str, Any]:
    """Map an NL request to a canonical Lumilake workflow DAG (Phase-1 rules)."""
    del context  # reserved for later phases
    symbols = extract_symbols(nl_request)
    intent = classify_intent(nl_request)

    if intent == "trading":
        ops = _trading_ops()
        return {
            "name": "nl2workflow-trading",
            "inputs": {"Stock": []},
            "ops": ops,
            "outputs": [{"name": "final_recommendation", "ref": "Fund Manager Verdict"}],
        }

    if intent == "compare":
        targets = symbols or ["AAPL", "NVDA"]
        ops = [
            _sql_op(
                f"Fundamentals Query {symbol}",
                ["Stock"],
                _FUNDAMENTALS_SQL.replace("{symbol}", symbol),
            )
            for symbol in targets
        ]
        return {
            "name": "nl2workflow-compare",
            "inputs": {"Stock": []},
            "ops": ops,
            "outputs": [
                {"name": "comparison", "ref": ops[-1]["id"]},
            ],
        }

    symbol = symbols[0] if symbols else None
    if symbol is not None:
        sql = {
            "profile": _PROFILE_SQL,
            "news": _NEWS_SQL,
            "fundamentals": _FUNDAMENTALS_SQL,
            "market": _MARKET_SQL,
        }[intent]
        op_id = {
            "profile": "Profile Query",
            "news": "News Query",
            "fundamentals": "Fundamentals Query",
            "market": "Market Query",
        }[intent]
        output_name = {"profile": "profile", "news": "news", "fundamentals": "fundamentals", "market": "market"}[intent]
        return {
            "name": f"nl2workflow-{intent}",
            "inputs": {"Stock": []},
            "ops": [_sql_op(op_id, ["Stock"], sql)],
            "outputs": [{"name": output_name, "ref": op_id}],
        }

    # No symbol detected: generic profile fetch over the whole universe.
    return {
        "name": "nl2workflow-profile",
        "inputs": {"Stock": []},
        "ops": [
            _sql_op(
                "Profile Query",
                [],
                "SELECT symbol, \"companyName\", sector, industry, \"marketCap\", beta "
                "FROM lumilake_demo.instrument_profile ORDER BY symbol LIMIT 10",
            )
        ],
        "outputs": [{"name": "profile", "ref": "Profile Query"}],
    }


def workflow_to_yaml(workflow: dict[str, Any]) -> str:
    """Serialize a workflow DAG to the Lumilake YAML submission format."""
    return yaml.safe_dump(workflow, sort_keys=False, allow_unicode=True)
