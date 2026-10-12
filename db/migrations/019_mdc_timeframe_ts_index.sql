-- 019_mdc_timeframe_ts_index.sql (PostgreSQL / Timescale)
-- Hand-applied equivalent of alembic revision 019. Use ONE path, not both.
-- Serves the freshness chip's per-timeframe top-1 seek; without it an empty
-- timeframe scans the full ts index (13.7s measured on 17.4M rows).
-- NOTE: hypertables reject CREATE INDEX CONCURRENTLY — run while idle.
CREATE INDEX IF NOT EXISTS ix_mdc_tf_ts
    ON market_data_cache (timeframe, ts DESC);
