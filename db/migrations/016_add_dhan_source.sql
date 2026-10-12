-- =============================================================================
-- Forward Testing Simulator — Add dhan to the portfolios source CHECK
-- Migration : 016_add_dhan_source
-- Engine    : PostgreSQL 13+
-- =============================================================================
--
-- WHAT THIS DOES
--   Widens the run-classification vocabulary for ``portfolios.source``:
--     source : 'synthetic' | 'replay' | 'mstock' | 'dhan'
--
--   WHY: Dhan is a registered broker (brokers.session_manager) with its own
--   live feed (DhanLiveFeed / DhanBarFeed) and its own taxonomy tag. The
--   2026-10-01 unlock made ``source='dhan'`` a spawnable runner source, so a
--   dhan-fed book is now a legitimate portfolio row — the 002-era CHECK
--   would reject it at persistence time even though every layer above the
--   DB accepts it.
--
--   No data changes: existing rows keep their values (all of them remain
--   valid under the widened CHECK). The UPDATE is a defensive no-op kept
--   for symmetry with 002.
--
-- IDEMPOTENCY
--   DROP CONSTRAINT IF EXISTS + ADD CONSTRAINT make the file safely
--   re-runnable, matching 002's convention.
--
-- ROLLBACK
--   ALTER TABLE portfolios
--     DROP CONSTRAINT IF EXISTS ck_portfolios_source;
--   ALTER TABLE portfolios
--     ADD CONSTRAINT ck_portfolios_source
--       CHECK (source IN ('synthetic','replay','mstock'));
--   DELETE FROM schema_migrations WHERE version = '016';
--   (fails only if a dhan row exists — delete or re-tag those rows first)
--
-- NOTE
--   The SQLite dev mirror is 016_add_dhan_source.sqlite.sql — keep both in
--   sync (SQLite cannot alter a CHECK in place, so the mirror rebuilds the
--   table; see that file's header).
-- =============================================================================

BEGIN;

ALTER TABLE portfolios
    DROP CONSTRAINT IF EXISTS ck_portfolios_source;

ALTER TABLE portfolios
    ADD CONSTRAINT ck_portfolios_source
        CHECK (source IN ('synthetic','replay','mstock','dhan'));

-- Backfill: normalise any row left unclassified (normally a no-op — kept
-- for symmetry with 002).
UPDATE portfolios
   SET source = 'synthetic'
 WHERE source IS NULL;

COMMENT ON COLUMN portfolios.source IS
    'synthetic = generated bars | replay = historical DB | mstock = live broker feed | dhan = live broker feed (second venue).';

-- =============================================================================
-- Record this migration
-- =============================================================================
INSERT INTO schema_migrations (version, description)
VALUES ('016', 'portfolios: widen source CHECK to admit dhan (multi-broker data unlock)')
ON CONFLICT (version) DO NOTHING;

COMMIT;

-- =============================================================================
-- END 016_add_dhan_source.sql
-- =============================================================================
