-- 019_mdc_timeframe_ts_index.sqlite.sql (SQLite)
-- Hand-applied equivalent of alembic revision 019. Use ONE path, not both.
CREATE INDEX IF NOT EXISTS ix_mdc_tf_ts
    ON market_data_cache (timeframe, ts DESC);
