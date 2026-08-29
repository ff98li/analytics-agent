-- Demo data for the Q2 trading-agent workflow (lumilake_demo schema).
-- Tables match the SQL templates in Lumilake/examples/templates/yaml/trading-agent.yaml.

BEGIN;
SET LOCAL lock_timeout = '10s';

CREATE SCHEMA IF NOT EXISTS lumilake_demo;

CREATE TABLE IF NOT EXISTS lumilake_demo.instrument_profile (
    symbol       text PRIMARY KEY,
    "companyName" text NOT NULL,
    sector       text NOT NULL,
    industry     text NOT NULL,
    "marketCap"  numeric NOT NULL,
    beta         numeric NOT NULL
);

CREATE TABLE IF NOT EXISTS lumilake_demo.financial_income_statement (
    symbol     text NOT NULL,
    date       date NOT NULL,
    revenue    numeric NOT NULL,
    "netIncome" numeric NOT NULL,
    eps        numeric NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS fis_symbol_date_key
    ON lumilake_demo.financial_income_statement (symbol, date);

CREATE TABLE IF NOT EXISTS lumilake_demo.ohlc_10m (
    symbol    text NOT NULL,
    "timestamp" timestamptz NOT NULL,
    open      numeric NOT NULL,
    high      numeric NOT NULL,
    low       numeric NOT NULL,
    close     numeric NOT NULL,
    volume    bigint NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ohlc_symbol_ts_key
    ON lumilake_demo.ohlc_10m (symbol, "timestamp");

CREATE TABLE IF NOT EXISTS lumilake_demo.market_metrics (
    symbol  text NOT NULL,
    metric  jsonb NOT NULL,
    version int NOT NULL DEFAULT 1
);
CREATE UNIQUE INDEX IF NOT EXISTS market_metrics_symbol_version_key
    ON lumilake_demo.market_metrics (symbol, version);

CREATE TABLE IF NOT EXISTS lumilake_demo.news_metadata (
    id             bigint GENERATED ALWAYS AS IDENTITY,
    symbol         text NOT NULL,
    title          text NOT NULL,
    summary        text,
    text           text,
    "publishedDate" timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS news_symbol_date_idx
    ON lumilake_demo.news_metadata (symbol, "publishedDate" DESC);
CREATE UNIQUE INDEX IF NOT EXISTS news_seed_natural_key
    ON lumilake_demo.news_metadata (symbol, title, "publishedDate");

-- ── Seeds ─────────────────────────────────────────────────────────────────────

INSERT INTO lumilake_demo.instrument_profile
    (symbol, "companyName", sector, industry, "marketCap", beta)
VALUES
    ('AAPL', 'Apple Inc.', 'Technology', 'Consumer Electronics', 3200000000000, 1.24),
    ('NVDA', 'NVIDIA Corporation', 'Technology', 'Semiconductors', 2900000000000, 1.75)
ON CONFLICT (symbol) DO UPDATE SET
    "companyName" = EXCLUDED."companyName",
    sector = EXCLUDED.sector,
    industry = EXCLUDED.industry,
    "marketCap" = EXCLUDED."marketCap",
    beta = EXCLUDED.beta;

INSERT INTO lumilake_demo.financial_income_statement
    (symbol, date, revenue, "netIncome", eps)
SELECT s.symbol, v.q::date, v.rev, v.ni, v.eps
FROM (VALUES
        ('AAPL',  '2025-10-01'::date, 94930000000, 14740000000, 0.97),
        ('AAPL',  '2026-01-01'::date, 124300000000, 36330000000, 2.40),
        ('AAPL',  '2026-04-01'::date, 95400000000, 18000000000, 1.18),
        ('AAPL',  '2026-07-01'::date, 100200000000, 21000000000, 1.38),
        ('NVDA',  '2025-10-01'::date, 35082000000, 19309000000, 0.78),
        ('NVDA',  '2026-01-01'::date, 39331000000, 22091000000, 0.89),
        ('NVDA',  '2026-04-01'::date, 44200000000, 23600000000, 0.95),
        ('NVDA',  '2026-07-01'::date, 48000000000, 26000000000, 1.05)
     ) AS v(symbol, q, rev, ni, eps)
JOIN (VALUES ('AAPL'), ('NVDA')) AS s(symbol) ON s.symbol = v.symbol
ON CONFLICT (symbol, date) DO UPDATE SET
    revenue = EXCLUDED.revenue,
    "netIncome" = EXCLUDED."netIncome",
    eps = EXCLUDED.eps;

-- Deterministic 10-minute bars, 2026-08-01 .. 2026-08-14 (market hours).
INSERT INTO lumilake_demo.ohlc_10m
    (symbol, "timestamp", open, high, low, close, volume)
SELECT
    s.symbol,
    ts,
    ROUND((base + 0.4 * sin(i / 23.0) + (i % 9) * 0.05)::numeric, 2),
    ROUND((base + 0.9 + 0.4 * sin(i / 23.0) + (i % 9) * 0.05)::numeric, 2),
    ROUND((base - 0.9 + 0.4 * sin(i / 23.0) + (i % 9) * 0.05)::numeric, 2),
    ROUND((base + 0.4 * cos(i / 31.0) + (i % 5) * 0.08)::numeric, 2),
    (1000 + (i * 7919) % 48000)::bigint
FROM generate_series('2026-08-01 09:30+08', '2026-08-14 16:00+08', interval '10 minutes') AS ts
CROSS JOIN (VALUES ('AAPL', 190.0), ('NVDA', 118.0)) AS s(symbol, base)
CROSS JOIN LATERAL (
    SELECT (EXTRACT(EPOCH FROM ts) / 600)::bigint AS i
) AS t
WHERE EXTRACT(DOW FROM ts) NOT IN (0, 6)
  AND ts::time BETWEEN '09:30' AND '16:00'
ON CONFLICT (symbol, "timestamp") DO UPDATE SET
    open = EXCLUDED.open,
    high = EXCLUDED.high,
    low = EXCLUDED.low,
    close = EXCLUDED.close,
    volume = EXCLUDED.volume;

INSERT INTO lumilake_demo.market_metrics (symbol, metric, version)
VALUES
    ('AAPL', '{"52WeekHigh": 260.10, "52WeekLow": 164.08, "beta": 1.24, "peTTM": 32.1, "epsGrowthTTMYoy": 9.4}'::jsonb, 1),
    ('NVDA', '{"52WeekHigh": 192.07, "52WeekLow": 75.61, "beta": 1.75, "peTTM": 41.3, "epsGrowthTTMYoy": 32.8}'::jsonb, 1)
ON CONFLICT (symbol, version) DO UPDATE SET
    metric = EXCLUDED.metric;

INSERT INTO lumilake_demo.news_metadata (symbol, title, summary, text, "publishedDate")
VALUES
    ('AAPL', 'Apple services revenue hits record high', 'Services segment grew 14% YoY.',
     'Apple reported record services revenue this quarter, driven by App Store and iCloud.', '2026-08-14 08:00+08'),
    ('AAPL', 'iPhone supply chain normalizes', 'Component lead times return to pre-2020 levels.',
     'Supply chain data suggests iPhone build plans are on track for the fall launch.', '2026-08-13 09:30+08'),
    ('AAPL', 'Analysts raise Apple price targets', 'Consensus target moved up 6%.',
     'Several analysts raised price targets citing AI-driven upgrade cycle expectations.', '2026-08-12 21:15+08'),
    ('AAPL', 'Apple expands on-device AI features', 'New models run locally on recent devices.',
     'Apple announced expanded on-device AI capabilities for its next OS release.', '2026-08-11 10:00+08'),
    ('AAPL', 'Regulatory review of App Store continues', 'EU examines fee structure changes.',
     'Regulators continue reviewing App Store fee changes announced earlier this year.', '2026-08-10 18:45+08'),
    ('NVDA', 'NVIDIA data-center demand remains strong', 'Hyperscaler capex guides raised again.',
     'Major cloud providers raised capital expenditure guidance, sustaining GPU demand.', '2026-08-14 09:00+08'),
    ('NVDA', 'Next-generation accelerator ramps', 'New platform enters volume production.',
     'NVIDIA confirmed its next-generation accelerator platform entered volume production.', '2026-08-13 22:00+08'),
    ('NVDA', 'AI inference workloads drive growth', 'Inference now over half of data-center revenue.',
     'Management noted inference workloads now exceed half of data-center revenue.', '2026-08-12 20:30+08'),
    ('NVDA', 'Export policy uncertainty persists', 'License requirements under review.',
     'Policy watchers flag ongoing uncertainty around export licensing for advanced chips.', '2026-08-11 15:00+08'),
    ('NVDA', 'NVIDIA expands software ecosystem', 'Enterprise AI software adoption accelerates.',
     'Enterprise adoption of NVIDIA''s AI software stack accelerated this quarter.', '2026-08-10 11:30+08')
ON CONFLICT (symbol, title, "publishedDate") DO UPDATE SET
    summary = EXCLUDED.summary,
    text = EXCLUDED.text;

COMMIT;
