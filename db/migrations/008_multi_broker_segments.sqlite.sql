-- =============================================================================
-- Forward Testing Simulator — Multi-broker segments & routing (SQLite dev variant)
-- Migration : 008_multi_broker_segments
-- Engine    : SQLite 3.35+
-- =============================================================================
--
-- LOCAL DEVELOPMENT mirror of 008_multi_broker_segments.sql. Keep both files
-- in sync when the change evolves.
--
-- DIFFERENCES FROM THE POSTGRES FILE (and why)
--   VARCHAR(n)  -> TEXT. SQLite has no VARCHAR (see 001's header).
--   Idempotency -> this file is intentionally NOT re-runnable: SQLite's
--                  ALTER TABLE ... ADD COLUMN has no IF NOT EXISTS.
--                  Incremental migrations are applied exactly once and
--                  tracked in schema_migrations.
-- =============================================================================

PRAGMA foreign_keys = ON;

BEGIN;

ALTER TABLE portfolios
    ADD COLUMN segment TEXT NOT NULL DEFAULT '';

ALTER TABLE portfolios
    ADD COLUMN execution_broker TEXT NOT NULL DEFAULT '';

ALTER TABLE positions
    ADD COLUMN broker TEXT NOT NULL DEFAULT 'paper';

ALTER TABLE orders
    ADD COLUMN broker TEXT NOT NULL DEFAULT 'paper';

-- Per-broker views (risk cards, reconcile, intelligence by_broker).
CREATE INDEX IF NOT EXISTS ix_positions_broker ON positions (broker);
CREATE INDEX IF NOT EXISTS ix_orders_broker    ON orders (broker);

-- Backfill: normalise any row left unclassified (normally a no-op —
-- ADD COLUMN ... NOT NULL DEFAULT already backfilled existing rows).
UPDATE portfolios SET segment = ''          WHERE segment IS NULL;
UPDATE portfolios SET execution_broker = '' WHERE execution_broker IS NULL;
UPDATE positions  SET broker = 'paper'      WHERE broker IS NULL;
UPDATE orders     SET broker = 'paper'      WHERE broker IS NULL;

INSERT OR IGNORE INTO schema_migrations (version, description)
VALUES ('008', 'multi-broker: portfolios segment/execution_broker; positions/orders broker');

COMMIT;
