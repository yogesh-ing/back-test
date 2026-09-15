-- =============================================================================
-- Forward Testing Simulator — Option trade structures (SQLite dev variant)
-- Migration : 004_add_trade_structures
-- Engine    : SQLite 3.35+
-- =============================================================================
--
-- LOCAL DEVELOPMENT mirror of 004_add_trade_structures.sql. Keep both files
-- in sync when the change evolves.
--
-- DIFFERENCES FROM THE POSTGRES FILE (and why)
--   UUID         -> TEXT. SQLite has no UUID type.
--   TIMESTAMPTZ  -> TEXT storing ISO-8601 UTC.
--   JSONB        -> TEXT. Use json_extract() for querying (JSON1 extension).
--   The CHECK constraint and indexes are identical in meaning.
-- =============================================================================

PRAGMA foreign_keys = ON;

BEGIN;

CREATE TABLE IF NOT EXISTS trade_structures (
    structure_id   TEXT    PRIMARY KEY,
    strategy_name  TEXT    NOT NULL,
    structure_type TEXT    NOT NULL,
    underlying     TEXT    NOT NULL,
    expiry         TEXT,
    status         TEXT    NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'closed', 'expired')),

    opened_at      TEXT    NOT NULL,
    closed_at      TEXT,

    entry_cost     NUMERIC NOT NULL DEFAULT 0,
    fees_paid      NUMERIC NOT NULL DEFAULT 0,
    realized_pnl   NUMERIC NOT NULL DEFAULT 0,

    legs           TEXT    NOT NULL DEFAULT '{}',
    created_at     TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS ix_trade_structures_status     ON trade_structures (status);
CREATE INDEX IF NOT EXISTS ix_trade_structures_underlying ON trade_structures (underlying);
CREATE INDEX IF NOT EXISTS ix_trade_structures_opened     ON trade_structures (opened_at DESC);

INSERT OR IGNORE INTO schema_migrations (version, description)
VALUES ('004', 'trade_structures: durable option structure book (Gap G4.2)');

COMMIT;

-- =============================================================================
-- END 004_add_trade_structures.sqlite.sql
-- =============================================================================
