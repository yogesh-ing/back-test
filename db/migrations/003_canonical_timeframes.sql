-- =============================================================================
-- Forward Testing Simulator — Canonical timeframe naming
-- Migration : 003_canonical_timeframes
-- Engine    : PostgreSQL 13+
-- =============================================================================
--
-- WHAT THIS DOES
--   market_data_cache.timeframe was written in several vocabularies over the
--   app's life (UI: 1D/1H/4H/1W/15M/5M, mStock ingest: 1min/60min/day/week,
--   configs: 1min). A '1H' backtest could never find its '60min' bars.
--   Ticket P4.3 fixes this to ONE canonical set, resolved with the lead:
--
--       1min | 5min | 15min | 1hour | 4hour | 1day | 1week
--
--   Existing rows are remapped where an exact equivalent exists
--   (60min->1hour, day->1day, week->1week); rows in timeframes with no
--   canonical equivalent (3min, 30min, month) are deleted — the table is a
--   re-ingestable cache (fetch_nifty500_historical.py), not a ledger.
--
-- IDEMPOTENCY
--   DROP CONSTRAINT IF EXISTS + re-ADD make the file safely re-runnable;
--   the UPDATEs are no-ops on a second run.
--
-- ROLLBACK
--   ALTER TABLE market_data_cache DROP CONSTRAINT IF EXISTS ck_mdc_timeframe;
--   ALTER TABLE market_data_cache ADD CONSTRAINT ck_mdc_timeframe
--       CHECK (timeframe IN ('1min','3min','5min','15min','30min','60min','1hour','day','week','month'));
--   (row deletions are NOT undone — they are re-ingestable cache data;
--   re-run the ingest to restore them)
--   DELETE FROM schema_migrations WHERE version = '003';
--
-- NOTE
--   The SQLite dev mirror is 003_canonical_timeframes.sqlite.sql — keep both
--   in sync (SQLite cannot alter a CHECK in place; the mirror rebuilds the
--   table; see that file's header).
-- =============================================================================

BEGIN;

-- Drop the old CHECK FIRST — the remaps below would violate it
-- (e.g. 'day' -> '1day' is not a value the old CHECK admits).
ALTER TABLE market_data_cache
    DROP CONSTRAINT IF EXISTS ck_mdc_timeframe;

-- Remap rows to the canonical names (exact 1:1 equivalents only)
UPDATE market_data_cache SET timeframe = '1hour' WHERE timeframe = '60min';
UPDATE market_data_cache SET timeframe = '1day'  WHERE timeframe = 'day';
UPDATE market_data_cache SET timeframe = '1week' WHERE timeframe = 'week';

-- No canonical equivalent: drop the rows (re-ingestable cache)
DELETE FROM market_data_cache WHERE timeframe IN ('3min', '30min', 'month');

ALTER TABLE market_data_cache
    ADD CONSTRAINT ck_mdc_timeframe
    CHECK (timeframe IN ('1min', '5min', '15min', '1hour', '4hour', '1day', '1week'));

-- =============================================================================
-- Record this migration
-- =============================================================================
INSERT INTO schema_migrations (version, description)
VALUES ('003', 'market_data_cache: canonical timeframe naming (1min/5min/15min/1hour/4hour/1day/1week)')
ON CONFLICT (version) DO NOTHING;

COMMIT;

-- =============================================================================
-- END 003_canonical_timeframes.sql
-- =============================================================================
