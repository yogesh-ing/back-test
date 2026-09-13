-- =============================================================================
-- Forward Testing Simulator — Option trade structures (Gap-Analysis G4.2)
-- Migration : 004_add_trade_structures
-- Engine    : PostgreSQL 13+
-- =============================================================================
--
-- WHAT THIS DOES
--   Adds the durable record for the options paper book. Before this table
--   the OptionPaperBroker lived in process memory only, so a server restart
--   lost every open structure. One row per executed structure:
--
--     entry_cost   net premium paid to open (debit positive, credit negative)
--     fees_paid    statutory fee stack charged at entry (STT, exchange
--                  transaction, SEBI, stamp duty, GST)
--     legs         JSONB snapshot of every leg, enough to rehydrate the
--                  in-memory StructurePosition byte-for-byte on startup
--
--   status lifecycle: 'open' -> 'closed' (manual square-off) | 'expired'
--   (expiry pipeline settlement).
--
-- IDEMPOTENCY
--   CREATE TABLE IF NOT EXISTS + ON CONFLICT DO NOTHING make the file safely
--   re-runnable, matching 001/002's convention.
--
-- ROLLBACK (Gap PRD risk-mitigation script)
--   DROP TABLE IF EXISTS trade_structures;
--   DELETE FROM schema_migrations WHERE version = '004';
-- =============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS trade_structures (
    structure_id   UUID         PRIMARY KEY,
    strategy_name  VARCHAR(64)  NOT NULL,
    structure_type VARCHAR(32)  NOT NULL,
    underlying     VARCHAR(32)  NOT NULL,
    expiry         DATE,
    status         VARCHAR(16)  NOT NULL DEFAULT 'open',

    opened_at      TIMESTAMPTZ  NOT NULL,
    closed_at      TIMESTAMPTZ,

    entry_cost     NUMERIC(20,4) NOT NULL DEFAULT 0,
    fees_paid      NUMERIC(20,4) NOT NULL DEFAULT 0,
    realized_pnl   NUMERIC(20,4) NOT NULL DEFAULT 0,

    legs           JSONB        NOT NULL DEFAULT '{}'::jsonb,
    created_at     TIMESTAMPTZ  NOT NULL DEFAULT now(),

    CONSTRAINT ck_trade_structures_status
        CHECK (status IN ('open', 'closed', 'expired'))
);

CREATE INDEX IF NOT EXISTS ix_trade_structures_status     ON trade_structures (status);
CREATE INDEX IF NOT EXISTS ix_trade_structures_underlying ON trade_structures (underlying);
CREATE INDEX IF NOT EXISTS ix_trade_structures_opened     ON trade_structures (opened_at DESC);

-- =============================================================================
-- Record this migration
-- =============================================================================
INSERT INTO schema_migrations (version, description)
VALUES ('004', 'trade_structures: durable option structure book (Gap G4.2)')
ON CONFLICT (version) DO NOTHING;

COMMIT;

-- =============================================================================
-- END 004_add_trade_structures.sql
-- =============================================================================
