-- Migration 026: Recorded option-chain history (Plan V3 Phase 4, §7)
-- option_chain_history is the bar-aligned, parity-filtered replay asset a
-- ChainHistoryFeed prices option backtests from: observed premium, IV, Greeks
-- and five-level depth_json (JSONB). Only clean rows land here; parity/junk
-- rejects go to data/chain_rejected.log, never into this table.
-- Numbering: chains from 025 in both the alembic and SQL-twin lines.

BEGIN;

CREATE TABLE IF NOT EXISTS option_chain_history (
    id SERIAL PRIMARY KEY,
    bar_ts TIMESTAMPTZ NOT NULL,
    snapshot_ts TIMESTAMPTZ NOT NULL,
    source VARCHAR(16) NOT NULL,
    underlying VARCHAR(32) NOT NULL,
    expiry DATE NOT NULL,
    strike NUMERIC(20,4) NOT NULL,
    option_type VARCHAR(2) NOT NULL,
    instrument_token VARCHAR(64) NOT NULL,
    trading_symbol VARCHAR(64) NOT NULL,
    lot_size INTEGER,
    ltp NUMERIC(20,4),
    bid NUMERIC(20,4),
    ask NUMERIC(20,4),
    iv DOUBLE PRECISION,
    delta DOUBLE PRECISION,
    gamma DOUBLE PRECISION,
    theta DOUBLE PRECISION,
    vega DOUBLE PRECISION,
    volume INTEGER,
    oi INTEGER,
    spot NUMERIC(20,4),
    depth_json JSONB,
    CONSTRAINT ck_option_chain_history_type CHECK (option_type IN ('CE','PE')),
    CONSTRAINT uq_option_chain_history_contract_bar
        UNIQUE (underlying, expiry, option_type, strike, bar_ts, source)
);

CREATE INDEX IF NOT EXISTS ix_chain_history_bar ON option_chain_history (bar_ts);
CREATE INDEX IF NOT EXISTS ix_chain_history_underlying ON option_chain_history (underlying);
CREATE INDEX IF NOT EXISTS ix_chain_history_lookup
    ON option_chain_history (underlying, expiry, option_type, strike, bar_ts);

COMMIT;