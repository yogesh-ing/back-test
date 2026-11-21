-- Migration 026: Recorded option-chain history (Plan V3 Phase 4, §7) — SQLite
-- Bar-aligned, parity-filtered replay asset for ChainHistoryFeed. depth_json is
-- TEXT-backed JSON here (JSONB on PostgreSQL). Only clean rows land here.

CREATE TABLE IF NOT EXISTS option_chain_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bar_ts TIMESTAMP NOT NULL,
    snapshot_ts TIMESTAMP NOT NULL,
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
    iv REAL,
    delta REAL,
    gamma REAL,
    theta REAL,
    vega REAL,
    volume INTEGER,
    oi INTEGER,
    spot NUMERIC(20,4),
    depth_json TEXT,
    CONSTRAINT ck_option_chain_history_type CHECK (option_type IN ('CE','PE')),
    CONSTRAINT uq_option_chain_history_contract_bar
        UNIQUE (underlying, expiry, option_type, strike, bar_ts, source)
);

CREATE INDEX IF NOT EXISTS ix_chain_history_bar ON option_chain_history (bar_ts);
CREATE INDEX IF NOT EXISTS ix_chain_history_underlying ON option_chain_history (underlying);
CREATE INDEX IF NOT EXISTS ix_chain_history_lookup
    ON option_chain_history (underlying, expiry, option_type, strike, bar_ts);