-- =============================================================================
-- Forward Testing Simulator — Multi-broker segments & execution routing
-- Migration : 008_multi_broker_segments
-- Engine    : PostgreSQL 14+
-- =============================================================================
--
-- PRD-001 (Multi-Broker Capital Allocation & Segmented Trading), MIG-001.
--
--   portfolios.segment          : segment name ('' = no segment, legacy)
--   portfolios.execution_broker : broker the runner routes orders to
--                                 ('' = paper / legacy default broker)
--   positions.broker            : broker holding the position ('paper' = sim)
--   orders.broker               : broker the order was sent to
--
-- Existing rows backfill to the legacy single-broker labels — never guessed.
--
-- ROLLBACK
--   ALTER TABLE orders     DROP COLUMN IF EXISTS broker;
--   ALTER TABLE positions  DROP COLUMN IF EXISTS broker;
--   ALTER TABLE portfolios DROP COLUMN IF EXISTS execution_broker,
--                          DROP COLUMN IF EXISTS segment;
--   DROP INDEX IF EXISTS ix_positions_broker;
--   DROP INDEX IF EXISTS ix_orders_broker;
--
-- The SQLite dev mirror is 008_multi_broker_segments.sqlite.sql — keep both
-- in sync when the change evolves.
-- =============================================================================

BEGIN;

ALTER TABLE portfolios
    ADD COLUMN IF NOT EXISTS segment          VARCHAR(64) NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS execution_broker VARCHAR(32) NOT NULL DEFAULT '';

ALTER TABLE positions
    ADD COLUMN IF NOT EXISTS broker VARCHAR(32) NOT NULL DEFAULT 'paper';

ALTER TABLE orders
    ADD COLUMN IF NOT EXISTS broker VARCHAR(32) NOT NULL DEFAULT 'paper';

-- Per-broker views (risk cards, reconcile, intelligence by_broker).
CREATE INDEX IF NOT EXISTS ix_positions_broker ON positions (broker);
CREATE INDEX IF NOT EXISTS ix_orders_broker    ON orders (broker);

-- Backfill: normalise any row left unclassified (normally a no-op —
-- ADD COLUMN ... NOT NULL DEFAULT already backfilled existing rows).
UPDATE portfolios SET segment = ''            WHERE segment IS NULL;
UPDATE portfolios SET execution_broker = ''   WHERE execution_broker IS NULL;
UPDATE positions  SET broker = 'paper'        WHERE broker IS NULL;
UPDATE orders     SET broker = 'paper'        WHERE broker IS NULL;

INSERT INTO schema_migrations (version, description)
VALUES ('008', 'multi-broker: portfolios segment/execution_broker; positions/orders broker')
ON CONFLICT (version) DO NOTHING;

COMMIT;
