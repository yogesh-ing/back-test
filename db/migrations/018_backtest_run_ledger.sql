-- =============================================================================
-- Forward Testing Simulator — Backtest Run Ledger
-- Migration : 018_backtest_run_ledger
-- Engine    : PostgreSQL 13+
-- =============================================================================
--
-- WHAT THIS DOES (docs/BACKTEST-RUN-PERSISTENCE-PRD.md §9)
--   Creates the append-only backtest ledger:
--     * backtest_compare_runs — run-many comparison parents (R1b)
--     * backtest_runs         — one immutable row per completed run (R1)
--     * backtest_run_series   — heavy trades/equity payloads, retention-capped (R2)
--   and adds the preset lineage column parameter_presets.backtest_run_id.
--
--   No data changes to existing tables. CREATE IF NOT EXISTS + ADD COLUMN IF
--   NOT EXISTS make the file safely re-runnable; index names are unique so a
--   second run is a no-op only where guarded — re-apply after checking, same
--   discipline as 015.
--
-- ALEMBIC EQUIVALENT
--   db/alembic/versions/20261003_1200_018_backtest_run_ledger.py — use ONE
--   path, not both (see revision 001's header). SQLite dev mirror:
--   018_backtest_run_ledger.sqlite.sql.
--
-- ROLLBACK
--   DROP TABLE backtest_run_series, backtest_runs, backtest_compare_runs;
--   ALTER TABLE parameter_presets DROP COLUMN backtest_run_id;
--   DELETE FROM schema_migrations WHERE version = '018';
-- =============================================================================

BEGIN;

-- -- R1b: comparison parent ----------------------------------------------------
CREATE TABLE IF NOT EXISTS backtest_compare_runs (
    compare_id         UUID        PRIMARY KEY,
    comparison_mode    VARCHAR(20) NOT NULL DEFAULT 'strategies',
    comparison_version INTEGER     NOT NULL DEFAULT 1,
    config_snapshot    JSONB       NOT NULL,
    data_source        VARCHAR(30),
    date_from          DATE,
    date_to            DATE,
    symbols_used       JSONB,
    engines_used       JSONB,
    provenance         JSONB,
    comparison_block   JSONB,
    slot_count         INTEGER     NOT NULL,
    slot_errors        JSONB,
    created_by         VARCHAR(100),  -- NOT NULL when web auth ships
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_bt_compares_mode CHECK (comparison_mode IN ('strategies','generalization')),
    CONSTRAINT ck_bt_compares_slots_nonneg CHECK (slot_count >= 0)
);
CREATE INDEX IF NOT EXISTS ix_backtest_compares_created
    ON backtest_compare_runs (created_at DESC);

-- -- R1: the run ledger ---------------------------------------------------------
CREATE TABLE IF NOT EXISTS backtest_runs (
    run_id              UUID         PRIMARY KEY,
    config_hash         VARCHAR(64)  NOT NULL,
    kind                VARCHAR(20)  NOT NULL DEFAULT 'single',
    parent_compare_id   UUID,
    optimization_run_id UUID,
    strategy_id         VARCHAR(100) NOT NULL,
    symbol              VARCHAR(30)  NOT NULL,
    timeframe           VARCHAR(10)  NOT NULL,
    date_from           DATE         NOT NULL,
    date_to             DATE         NOT NULL,
    capital             NUMERIC(20,4) NOT NULL,
    engine              VARCHAR(30)  NOT NULL,
    payload_version     INTEGER      NOT NULL DEFAULT 1,
    params              JSONB        NOT NULL,
    config              JSONB        NOT NULL,
    readiness           JSONB,
    cost_shock          JSONB,
    metrics             JSONB,
    sharpe              NUMERIC(10,4),
    sortino             NUMERIC(10,4),
    calmar              NUMERIC(10,4),
    total_return        NUMERIC(10,4),
    cagr                NUMERIC(10,4),
    max_drawdown        NUMERIC(10,4),
    profit_factor       NUMERIC(10,4),
    win_rate            NUMERIC(5,2),
    total_trades        INTEGER,
    series_status       VARCHAR(12)  NOT NULL DEFAULT 'write_failed',
    data_source         VARCHAR(30),
    bars_count          INTEGER,
    fetched_first_ts    TIMESTAMPTZ,
    fetched_last_ts     TIMESTAMPTZ,
    data_fetch_date     DATE,
    provenance          JSONB,
    code_fingerprint    JSONB        NOT NULL,
    created_by          VARCHAR(100),  -- NOT NULL when web auth ships
    created_at          TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CONSTRAINT ck_bt_runs_kind CHECK
        (kind IN ('single','compare_slot','optimizer_baseline')),
    CONSTRAINT ck_bt_runs_series_status CHECK
        (series_status IN ('present','evicted','write_failed')),
    CONSTRAINT ck_bt_runs_win_rate CHECK
        (win_rate IS NULL OR (win_rate >= 0 AND win_rate <= 100)),
    CONSTRAINT fk_bt_runs_compare FOREIGN KEY (parent_compare_id)
        REFERENCES backtest_compare_runs (compare_id) ON DELETE SET NULL,
    CONSTRAINT fk_bt_runs_opt_run FOREIGN KEY (optimization_run_id)
        REFERENCES optimization_runs (run_id) ON DELETE SET NULL
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

-- -- R2: heavy series (own transaction; parent tracks its lifecycle) ------------
CREATE TABLE IF NOT EXISTS backtest_run_series (
    run_id        UUID        PRIMARY KEY,
    trades        JSONB,
    equity        JSONB,
    drawdown      JSONB,
    signals       JSONB,
    extras        JSONB,
    bytes_written BIGINT      NOT NULL DEFAULT 0,
    stored_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT fk_bt_series_run FOREIGN KEY (run_id)
        REFERENCES backtest_runs (run_id) ON DELETE CASCADE
);

-- -- R3 lineage: preset ← backtest run -------------------------------------------
ALTER TABLE parameter_presets
    ADD COLUMN IF NOT EXISTS backtest_run_id UUID
        REFERENCES backtest_runs (run_id) ON DELETE SET NULL;

INSERT INTO schema_migrations (version) VALUES ('018')
    ON CONFLICT (version) DO NOTHING;

COMMIT;
