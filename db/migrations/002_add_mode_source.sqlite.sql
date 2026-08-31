-- =============================================================================
-- Forward Testing Simulator — Add mode/source to portfolios (SQLite dev variant)
-- Migration : 002_add_mode_source
-- Engine    : SQLite 3.35+
-- =============================================================================
--
-- LOCAL DEVELOPMENT mirror of 002_add_mode_source.sql. Keep both files in
-- sync when the change evolves.
--
-- DIFFERENCES FROM THE POSTGRES FILE (and why)
--   CHECK placement -> column-level inside ADD COLUMN. SQLite has no
--                      ALTER TABLE ... ADD CONSTRAINT, so a table-level
--                      CHECK cannot be added to an existing table without a
--                      full table rebuild. Functionally identical for the app.
--   VARCHAR(16)     -> TEXT. SQLite has no VARCHAR (see 001's header).
--   Idempotency     -> this file is intentionally NOT re-runnable: SQLite's
--                      ALTER TABLE ... ADD COLUMN has no IF NOT EXISTS.
--                      Incremental migrations are applied exactly once and
--                      tracked in schema_migrations (unlike 001, which is a
--                      bootstrap script and re-runnable by design).
-- =============================================================================

PRAGMA foreign_keys = ON;

BEGIN;

ALTER TABLE portfolios
    ADD COLUMN mode TEXT NOT NULL DEFAULT 'paper'
        CHECK (mode IN ('paper','live'));

ALTER TABLE portfolios
    ADD COLUMN source TEXT NOT NULL DEFAULT 'synthetic'
        CHECK (source IN ('synthetic','replay','mstock'));

-- Backfill: normalise any row left unclassified (normally a no-op —
-- ADD COLUMN ... NOT NULL DEFAULT already backfilled existing rows).
UPDATE portfolios
   SET mode   = 'paper',
       source = 'synthetic'
 WHERE mode IS NULL OR source IS NULL;

INSERT OR IGNORE INTO schema_migrations (version, description)
VALUES ('002', 'portfolios: add mode (paper|live) and source (synthetic|replay|mstock) columns');

COMMIT;
