-- =============================================================================
-- Forward Testing Simulator — trades idempotency key
-- Migration : 022_trades_dedupe_key
-- Engine    : PostgreSQL 13+
-- =============================================================================
--
-- WHAT THIS DOES
--   trades.dedupe_key  VARCHAR(64)  + a UNIQUE index on it.
--   The value is SHA-256 over the columns that identify one round trip:
--     (portfolio_id, symbol, direction, entry_time, exit_time,
--      entry_price, exit_price, quantity)
--   Written by backtest.forward.trade_persistence.LiveTradePersister, which
--   also looks the tuple up before inserting — this index is what makes that
--   check hold under a second writer.
--
--   WHY: B1 (QA 2026-10-30). Every restart restores a runner's closed-trades
--   cache and the persister flushed it again with a fresh trade_id, giving
--   6,476 rows for 2,878 real trades and ₹2,23,635.74 of phantom loss (up to
--   16 copies of one fill). Any realized-P&L or win-rate read from this table
--   was wrong by a multiple.
--
--   The column is NULLABLE on purpose: the duplicates already in the table
--   would collide the moment it was backfilled, so the constraint guards from
--   here forward and scripts/repair_trade_duplicates.py owns the at-rest
--   cleanup as a separately-approved, bak_-snapshotted step. P&L is NOT in the
--   key — a corrected fee must not read as a new trade.
--
-- IDEMPOTENCY
--   ADD COLUMN IF NOT EXISTS + CREATE UNIQUE INDEX IF NOT EXISTS: safely
--   re-runnable, and re-runnable on a database that already carries the column.
--   No rows are touched.
--
-- ROLLBACK
--   DROP INDEX IF EXISTS uq_trades_dedupe_key;
--   ALTER TABLE trades DROP COLUMN IF EXISTS dedupe_key;
--   DELETE FROM schema_migrations WHERE version = '022';
--
-- NOTE
--   The Alembic mirror is 20261031_1200_022_trades_dedupe_key.py and the SQLite
--   dev mirror is 022_trades_dedupe_key.sqlite.sql. Use ONE ledger —
--   alembic_version or schema_migrations, not both (see 001's header).
-- =============================================================================

BEGIN;

ALTER TABLE trades
    ADD COLUMN IF NOT EXISTS dedupe_key VARCHAR(64);

COMMENT ON COLUMN trades.dedupe_key IS
    'SHA-256 of (portfolio_id, symbol, direction, entry_time, exit_time, entry_price, exit_price, quantity); the idempotency key that keeps a resume from re-inserting a closed trade. NULL on rows written before 022.';

CREATE UNIQUE INDEX IF NOT EXISTS uq_trades_dedupe_key
    ON trades (dedupe_key);

-- =============================================================================
-- Record this migration
-- =============================================================================
INSERT INTO schema_migrations (version, description)
VALUES ('022', 'trades: dedupe_key + unique index — one row per round trip (B1)')
ON CONFLICT (version) DO NOTHING;

COMMIT;

-- =============================================================================
-- END 022_trades_dedupe_key.sql
-- =============================================================================
