-- Migration 020: Data Coverage Summary Table
-- Purpose: Replace slow GROUP BY scans on 46M-row hypertable with instant summary lookups
-- Date: 2026-10-18
-- Impact: Coverage checks go from 14s → <100ms

BEGIN;

-- Create summary table
CREATE TABLE IF NOT EXISTS data_coverage_summary (
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    bar_count INTEGER NOT NULL DEFAULT 0,
    from_date DATE,
    to_date DATE,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now(),
    PRIMARY KEY (symbol, timeframe)
);

-- Index for fast lookups
CREATE INDEX IF NOT EXISTS idx_coverage_lookup
    ON data_coverage_summary (symbol, timeframe);

-- Backfill from existing market_data_cache (may take several minutes on 46M rows)
-- This is a one-time operation; future updates are incremental
INSERT INTO data_coverage_summary (symbol, timeframe, bar_count, from_date, to_date, updated_at)
SELECT symbol,
       timeframe,
       COUNT(*)::int as bar_count,
       MIN(ts)::date as from_date,
       MAX(ts)::date as to_date,
       now() as updated_at
FROM market_data_cache
GROUP BY symbol, timeframe
ON CONFLICT (symbol, timeframe) DO NOTHING;

-- Grant permissions to app user
GRANT ALL PRIVILEGES ON TABLE data_coverage_summary TO ft_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO ft_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO ft_app;

COMMIT;
