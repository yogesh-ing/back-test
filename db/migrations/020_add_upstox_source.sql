-- =============================================================================
-- Forward Testing Simulator — Add upstox to the portfolios source CHECK
-- Migration : 020_add_upstox_source
-- Engine    : PostgreSQL 13+
-- =============================================================================
--
-- WHAT THIS DOES
--   Widens the run-classification vocabulary for ``portfolios.source``:
--     source : 'synthetic' | 'replay' | 'mstock' | 'dhan' | 'upstox'
--
--   WHY: Upstox is a registered broker (brokers.session_manager) with its own
--   live feed (UpstoxLiveFeed / UpstoxBarFeed) and its own taxonomy tag. With
--   this unlock ``source='upstox'`` becomes a spawnable runner source, so an
--   Upstox-fed book is a legitimate portfolio row — the 002-era CHECK (last
--   widened by 016 for dhan) would reject it at persistence time even though
--   every layer above the DB accepts it.
--
--   No data changes: existing rows keep their values (all of them remain
--   valid under the widened CHECK). The UPDATE is a defensive no-op kept
--   for symmetry with 002/016.
--
-- IDEMPOTENCY
--   DROP CONSTRAINT IF EXISTS + ADD CONSTRAINT make the file safely
--   re-runnable, matching 016's convention.
--
-- ROLLBACK
--   ALTER TABLE portfolios
--     DROP CONSTRAINT IF EXISTS ck_portfolios_source;
--   ALTER TABLE portfolios
--     ADD CONSTRAINT ck_portfolios_source
--       CHECK (source IN ('synthetic','replay','mstock','dhan'));
--   DELETE FROM schema_migrations WHERE version = '020';
--   (fails only if an upstox row exists — delete or re-tag those rows first)
--
-- NOTE
--   The SQLite dev mirror is 020_add_upstox_source.sqlite.sql — keep both in
--   sync (SQLite cannot alter a CHECK in place, so the mirror rebuilds the
--   table; see that file's header).
-- =============================================================================

BEGIN;

ALTER TABLE portfolios
    DROP CONSTRAINT IF EXISTS ck_portfolios_source;

ALTER TABLE portfolios
    ADD CONSTRAINT ck_portfolios_source
        CHECK (source IN ('synthetic','replay','mstock','dhan','upstox'));

-- Backfill: normalise any row left unclassified (normally a no-op — kept
-- for symmetry with 002/016).
UPDATE portfolios
   SET source = 'synthetic'
 WHERE source IS NULL;

COMMENT ON COLUMN portfolios.source IS
    'synthetic = generated bars | replay = historical DB | mstock = live broker feed | dhan = live broker feed (second venue) | upstox = live broker feed (third venue).';

-- =============================================================================
-- Record this migration
-- =============================================================================
INSERT INTO schema_migrations (version, description)
VALUES ('020', 'portfolios: widen source CHECK to admit upstox (third broker data unlock)')
ON CONFLICT (version) DO NOTHING;

COMMIT;

-- =============================================================================
-- END 020_add_upstox_source.sql
-- =============================================================================
