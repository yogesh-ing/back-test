-- =============================================================================
-- Forward Testing Simulator — trades idempotency key (SQLite dev mirror)
-- Migration : 022_trades_dedupe_key
-- Engine    : SQLite 3.35+
-- =============================================================================
--
-- Mirror of 022_trades_dedupe_key.sql / alembic revision 022. Same shape, same
-- meaning: dedupe_key VARCHAR(64) NULL, unique over the round-trip identity
-- hash written by LiveTradePersister. NULLs are distinct in a SQLite unique
-- index, so pre-022 rows never collide. See the PostgreSQL file's header for
-- the B1 measurement and the rollback.
--
-- RE-RUN
--   SQLite has no ADD COLUMN IF NOT EXISTS. A second run fails with
--   "duplicate column name: dedupe_key" — harmless, the schema is already
--   there; skip straight to the ledger INSERT. The Alembic mirror IS
--   re-runnable (it inspects the table first).
-- =============================================================================

BEGIN;

ALTER TABLE trades ADD COLUMN dedupe_key VARCHAR(64);

CREATE UNIQUE INDEX IF NOT EXISTS uq_trades_dedupe_key
    ON trades (dedupe_key);

INSERT OR IGNORE INTO schema_migrations (version, description)
VALUES ('022', 'trades: dedupe_key + unique index — one row per round trip (B1)');

COMMIT;

-- =============================================================================
-- END 022_trades_dedupe_key.sqlite.sql
-- =============================================================================
