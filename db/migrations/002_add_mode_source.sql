-- =============================================================================
-- Forward Testing Simulator — Add mode/source to portfolios
-- Migration : 002_add_mode_source
-- Engine    : PostgreSQL 13+
-- =============================================================================
--
-- WHAT THIS DOES
--   Adds two run-classification columns to the portfolios root aggregate:
--     mode   : 'paper' | 'live'        — simulated fills vs real broker orders
--     source : 'synthetic' | 'replay' | 'mstock' — where the run's bars come from
--
-- Both columns are NOT NULL with server defaults, so every existing row is
-- classified the moment the ALTER lands (the whole pre-002 history of this
-- app was paper/synthetic). The follow-up UPDATE is a defensive no-op in a
-- normal database — it exists so a row written while the ALTER is in flight
-- can never remain unclassified (backfill requirement of ticket P1.1).
--
-- IDEMPOTENCY
--   ADD COLUMN IF NOT EXISTS + DROP/ADD CONSTRAINT make the file safely
--   re-runnable, matching 001's convention.
--
-- ROLLBACK
--   ALTER TABLE portfolios
--     DROP CONSTRAINT IF EXISTS ck_portfolios_mode,
--     DROP CONSTRAINT IF EXISTS ck_portfolios_source,
--     DROP COLUMN IF EXISTS mode,
--     DROP COLUMN IF EXISTS source;
--   DELETE FROM schema_migrations WHERE version = '002';
--   (no dedicated rollback file: this migration is purely additive and
--   non-destructive — 001's rollback file exists only because 001 creates
--   the entire schema)
--
-- NOTE
--   The SQLite dev mirror is 002_add_mode_source.sqlite.sql — keep both in
--   sync (SQLite cannot add a table-level CHECK, so its checks are
--   column-level; see that file's header).
-- =============================================================================

BEGIN;

ALTER TABLE portfolios
    ADD COLUMN IF NOT EXISTS mode   VARCHAR(16) NOT NULL DEFAULT 'paper',
    ADD COLUMN IF NOT EXISTS source VARCHAR(16) NOT NULL DEFAULT 'synthetic';

ALTER TABLE portfolios
    DROP CONSTRAINT IF EXISTS ck_portfolios_mode,
    DROP CONSTRAINT IF EXISTS ck_portfolios_source;

ALTER TABLE portfolios
    ADD CONSTRAINT ck_portfolios_mode   CHECK (mode IN ('paper','live')),
    ADD CONSTRAINT ck_portfolios_source CHECK (source IN ('synthetic','replay','mstock'));

-- Backfill: normalise any row left unclassified (normally a no-op — the
-- server defaults already fill existing rows at ALTER time).
UPDATE portfolios
   SET mode   = 'paper',
       source = 'synthetic'
 WHERE mode IS NULL OR source IS NULL;

COMMENT ON COLUMN portfolios.mode   IS 'paper = simulated fills | live = real broker orders.';
COMMENT ON COLUMN portfolios.source IS 'synthetic = generated bars | replay = historical DB | mstock = live broker feed.';

-- =============================================================================
-- Record this migration
-- =============================================================================
INSERT INTO schema_migrations (version, description)
VALUES ('002', 'portfolios: add mode (paper|live) and source (synthetic|replay|mstock) columns')
ON CONFLICT (version) DO NOTHING;

COMMIT;

-- =============================================================================
-- END 002_add_mode_source.sql
-- =============================================================================
