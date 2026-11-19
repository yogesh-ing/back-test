-- Migration 025: Options model backtest provenance (SQLite)
-- SQLite ALTER TABLE ADD COLUMN has no IF NOT EXISTS; run once.

ALTER TABLE backtest_runs ADD COLUMN pricing_feed VARCHAR(32);
ALTER TABLE backtest_runs ADD COLUMN model_params TEXT;