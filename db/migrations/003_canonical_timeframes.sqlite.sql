-- =============================================================================
-- Forward Testing Simulator — Canonical timeframe naming (SQLite dev variant)
-- Migration : 003_canonical_timeframes
-- Engine    : SQLite 3.35+
-- =============================================================================
--
-- LOCAL DEVELOPMENT mirror of 003_canonical_timeframes.sql. Keep both files
-- in sync when the change evolves.
--
-- DIFFERENCES FROM THE POSTGRES FILE (and why)
--   CHECK constraint -> full table rebuild. SQLite cannot drop or alter a
--                      table-level CHECK on an existing table; the standard
--                      12-step rebuild (create new, copy, drop, rename,
--                      recreate index) is the supported path.
--   The data remap (60min->1hour, day->1day, week->1week) and the deletion
--   of rows without a canonical equivalent (3min/30min/month) happen IN the
--   rebuild's COPY (a CASE on timeframe) — the old CHECK cannot be dropped
--   in place, and the new CHECK would reject untransformed old names.
--   Idempotency -> this file is intentionally NOT re-runnable (the rename
--   step fails on a second run), matching 002's convention: incremental
--   migrations are applied exactly once and tracked in schema_migrations.
-- =============================================================================

PRAGMA foreign_keys = ON;

BEGIN;

-- Rebuild market_data_cache with the canonical CHECK. The remap and the
-- deletion happen IN the copy (a CASE on timeframe) — the old CHECK cannot
-- be dropped in place on SQLite, and the new CHECK would reject the old
-- names if they were copied untransformed.
CREATE TABLE market_data_cache_new (
    data_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol      TEXT    NOT NULL,
    exchange    TEXT    NOT NULL DEFAULT 'NSE',
    timeframe   TEXT    NOT NULL,
    ts          TEXT    NOT NULL,
    open        NUMERIC NOT NULL,
    high        NUMERIC NOT NULL,
    low         NUMERIC NOT NULL,
    close       NUMERIC NOT NULL,
    volume      NUMERIC NOT NULL DEFAULT 0,
    bid         NUMERIC,
    ask         NUMERIC,
    source      TEXT    NOT NULL DEFAULT 'mstock',
    ingested_at TEXT    NOT NULL DEFAULT (datetime('now')),

    CONSTRAINT ck_mdc_timeframe CHECK (timeframe IN
        ('1min','5min','15min','1hour','4hour','1day','1week')),
    CONSTRAINT ck_mdc_ohlc CHECK (
        high >= low AND high >= open AND high >= close
        AND low <= open AND low <= close),
    CONSTRAINT ck_mdc_prices_pos CHECK (open > 0 AND high > 0 AND low > 0 AND close > 0),
    CONSTRAINT ck_mdc_volume     CHECK (volume >= 0),
    CONSTRAINT ck_mdc_spread     CHECK (bid IS NULL OR ask IS NULL OR bid <= ask),
    CONSTRAINT uq_mdc_bar UNIQUE (symbol, exchange, timeframe, ts)
);

-- Remap (60min->1hour, day->1day, week->1week) and drop rows without a
-- canonical equivalent (3min/30min/month) in a single transformed copy.
INSERT INTO market_data_cache_new
    (data_id, symbol, exchange, timeframe, ts, open, high, low, close,
     volume, bid, ask, source, ingested_at)
    SELECT data_id, symbol, exchange,
           CASE timeframe
               WHEN '60min' THEN '1hour'
               WHEN 'day'   THEN '1day'
               WHEN 'week'  THEN '1week'
               ELSE timeframe
           END,
           ts, open, high, low, close, volume, bid, ask, source, ingested_at
    FROM market_data_cache
    WHERE timeframe NOT IN ('3min', '30min', 'month');

DROP TABLE market_data_cache;
ALTER TABLE market_data_cache_new RENAME TO market_data_cache;

CREATE INDEX IF NOT EXISTS ix_mdc_symbol_tf_ts ON market_data_cache (symbol, timeframe, ts DESC);
CREATE INDEX IF NOT EXISTS ix_mdc_ts           ON market_data_cache (ts DESC);

INSERT OR IGNORE INTO schema_migrations (version, description)
VALUES ('003', 'market_data_cache: canonical timeframe naming (1min/5min/15min/1hour/4hour/1day/1week)');

COMMIT;
