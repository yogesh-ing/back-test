-- =============================================================================
-- Forward Testing Simulator — Backtest Run Ledger (SQLite dev variant)
-- Migration : 018_backtest_run_ledger
-- Engine    : SQLite 3.35+
-- =============================================================================
--
-- LOCAL DEVELOPMENT mirror of 018_backtest_run_ledger.sql. Keep both files
-- in sync when the change evolves.
--
-- DIFFERENCES FROM THE POSTGRES FILE (and why)
--   UUID/JSONB   -> TEXT columns (the ORM UUIDStr/JSONVariant twins behave
--                   the same way through create_all).
--   ADD COLUMN   -> SQLite has no IF NOT EXISTS for columns; the REFERENCES
--                   clause is inline (supported on ADD COLUMN when the
--                   default is NULL). Re-running fails on this line by
--                   design, matching 002's single-apply convention.
--   DESC index   -> literal "created_at DESC" works as-is.
--   Alembic path -> this chain is applied via
--                   db/alembic/versions/20261003_1200_018_backtest_run_ledger.py
--                   on dev DBs; this file is for the hand-applied path.
-- =============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS backtest_compare_runs (
    compare_id         TEXT PRIMARY KEY,
    comparison_mode    TEXT NOT NULL DEFAULT 'strategies'
        CHECK (comparison_mode IN ('strategies','generalization')),
    comparison_version INTEGER NOT NULL DEFAULT 1,
    config_snapshot    TEXT NOT NULL,
    data_source        TEXT,
    date_from          DATE,
    date_to            DATE,
    symbols_used       TEXT,
    engines_used       TEXT,
    provenance         TEXT,
    comparison_block   TEXT,
    slot_count         INTEGER NOT NULL CHECK (slot_count >= 0),
    slot_errors        TEXT,
    created_by         TEXT,  -- NOT NULL when web auth ships
    created_at         TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_backtest_compares_created
    ON backtest_compare_runs (created_at DESC);

CREATE TABLE IF NOT EXISTS backtest_runs (
    run_id              TEXT PRIMARY KEY,
    config_hash         TEXT NOT NULL,
    kind                TEXT NOT NULL DEFAULT 'single'
        CHECK (kind IN ('single','compare_slot','optimizer_baseline')),
    parent_compare_id   TEXT REFERENCES backtest_compare_runs (compare_id)
        ON DELETE SET NULL,
    optimization_run_id TEXT REFERENCES optimization_runs (run_id)
        ON DELETE SET NULL,
    strategy_id         TEXT NOT NULL,
    symbol              TEXT NOT NULL,
    timeframe           TEXT NOT NULL,
    date_from           DATE NOT NULL,
    date_to             DATE NOT NULL,
    capital             NUMERIC(20,4) NOT NULL,
    engine              TEXT NOT NULL,
    payload_version     INTEGER NOT NULL DEFAULT 1,
    params              TEXT NOT NULL,
    config              TEXT NOT NULL,
    readiness           TEXT,
    cost_shock          TEXT,
    metrics             TEXT,
    sharpe              NUMERIC(10,4),
    sortino             NUMERIC(10,4),
    calmar              NUMERIC(10,4),
    total_return        NUMERIC(10,4),
    cagr                NUMERIC(10,4),
    max_drawdown        NUMERIC(10,4),
    profit_factor       NUMERIC(10,4),
    win_rate            NUMERIC(5,2) CHECK (win_rate IS NULL OR (win_rate BETWEEN 0 AND 100)),
    total_trades        INTEGER,
    series_status       TEXT NOT NULL DEFAULT 'write_failed'
        CHECK (series_status IN ('present','evicted','write_failed')),
    data_source         TEXT,
    bars_count          INTEGER,
    fetched_first_ts    TIMESTAMP,
    fetched_last_ts     TIMESTAMP,
    data_fetch_date     DATE,
    provenance          TEXT,
    code_fingerprint    TEXT NOT NULL,
    created_by          TEXT,  -- NOT NULL when web auth ships
    created_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_backtest_runs_list
    ON backtest_runs (strategy_id, symbol, timeframe, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_backtest_runs_config
    ON backtest_runs (config_hash, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_backtest_runs_kind
    ON backtest_runs (kind, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_backtest_runs_opt
    ON backtest_runs (optimization_run_id) WHERE optimization_run_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_backtest_runs_compare
    ON backtest_runs (parent_compare_id) WHERE parent_compare_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS backtest_run_series (
    run_id        TEXT PRIMARY KEY REFERENCES backtest_runs (run_id) ON DELETE CASCADE,
    trades        TEXT,
    equity        TEXT,
    drawdown      TEXT,
    signals       TEXT,
    extras        TEXT,
    bytes_written INTEGER NOT NULL DEFAULT 0,
    stored_at     TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

ALTER TABLE parameter_presets
    ADD COLUMN backtest_run_id TEXT REFERENCES backtest_runs (run_id)
        ON DELETE SET NULL;

INSERT OR IGNORE INTO schema_migrations (version, description)
VALUES ('018', 'backtest run ledger: backtest_runs, backtest_compare_runs, backtest_run_series');

COMMIT;
