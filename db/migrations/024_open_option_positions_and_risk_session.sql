-- Migration 024: Institutional PostgreSQL Single-Source-of-Truth (Sprint D / Ticket D1)
-- Additive relational tables for runner instances, open option structures/legs (with
-- 15:30 IST prev_close_mark), and cash-flow-neutral session risk state.
-- Zero existing view modifications (014/015 rule) and zero market_data_cache touches.

BEGIN;

CREATE TABLE IF NOT EXISTS runner_instances (
    instance_id        VARCHAR(64) PRIMARY KEY,
    portfolio_id       UUID REFERENCES portfolios(portfolio_id) ON DELETE SET NULL,
    name               VARCHAR(128) NOT NULL,
    strategy_name      VARCHAR(64) NOT NULL,
    mode               VARCHAR(16) NOT NULL,
    source             VARCHAR(32) NOT NULL,
    segment            VARCHAR(32),
    execution_broker   VARCHAR(32),
    status             VARCHAR(24) NOT NULL DEFAULT 'PAUSED',
    allocated_capital  NUMERIC(20, 4) NOT NULL DEFAULT 0,
    carry_policy       VARCHAR(16),
    config_json        JSONB NOT NULL DEFAULT '{}'::jsonb,
    runtime_state_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    prev_close_mark    NUMERIC(20, 8),
    last_mark_price    NUMERIC(20, 8),
    last_mark_at       TIMESTAMPTZ,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT ck_runner_instances_mode CHECK (mode IN ('paper', 'live')),
    CONSTRAINT ck_runner_instances_status CHECK (
        status IN ('RUNNING', 'PAUSED', 'STOPPED', 'RETIRED')
    ),
    CONSTRAINT ck_runner_instances_carry CHECK (
        carry_policy IS NULL OR carry_policy IN ('intraday', 'positional')
    )
);

CREATE INDEX IF NOT EXISTS ix_runner_instances_mode_status
    ON runner_instances (mode, status);

CREATE TABLE IF NOT EXISTS option_open_structures (
    structure_id        VARCHAR(64) PRIMARY KEY,
    instance_id         VARCHAR(64) NOT NULL,
    portfolio_id        UUID REFERENCES portfolios(portfolio_id) ON DELETE SET NULL,
    mode                VARCHAR(16) NOT NULL,
    strategy_name       VARCHAR(64) NOT NULL,
    structure_type      VARCHAR(64) NOT NULL,
    underlying          VARCHAR(32) NOT NULL,
    expiry              DATE NOT NULL,
    carry_policy        VARCHAR(16) NOT NULL DEFAULT 'intraday',
    status              VARCHAR(16) NOT NULL DEFAULT 'open',
    entry_session_date  DATE NOT NULL,
    session_date        DATE,
    entry_net_cost      NUMERIC(20, 4) NOT NULL DEFAULT 0,
    commission_total    NUMERIC(20, 4) NOT NULL DEFAULT 0,
    statutory_fees      NUMERIC(20, 4) NOT NULL DEFAULT 0,
    last_mark_net_pnl   NUMERIC(20, 4) NOT NULL DEFAULT 0,
    prior_close_net_pnl NUMERIC(20, 4) NOT NULL DEFAULT 0,
    prev_close_mark     NUMERIC(20, 8),
    stop_loss           NUMERIC(20, 8),
    target              NUMERIC(20, 8),
    opened_at           TIMESTAMPTZ NOT NULL,
    closed_at           TIMESTAMPTZ,
    last_mark_at        TIMESTAMPTZ,
    exit_reason         VARCHAR(64),
    CONSTRAINT ck_opt_open_struct_mode CHECK (mode IN ('paper', 'live')),
    CONSTRAINT ck_opt_open_struct_carry CHECK (
        carry_policy IN ('intraday', 'positional')
    ),
    CONSTRAINT ck_opt_open_struct_status CHECK (status IN ('open', 'closed'))
);

CREATE INDEX IF NOT EXISTS ix_opt_open_struct_instance_status
    ON option_open_structures (instance_id, status);

CREATE INDEX IF NOT EXISTS ix_opt_open_struct_mode_status
    ON option_open_structures (mode, status);

CREATE TABLE IF NOT EXISTS option_open_legs (
    position_id      VARCHAR(64) PRIMARY KEY,
    structure_id     VARCHAR(64) NOT NULL REFERENCES option_open_structures(structure_id) ON DELETE CASCADE,
    instance_id      VARCHAR(64) NOT NULL,
    instrument_type  VARCHAR(8) NOT NULL DEFAULT 'OPT',
    instrument_token VARCHAR(64),
    trading_symbol   VARCHAR(64) NOT NULL,
    underlying       VARCHAR(32) NOT NULL,
    option_type      VARCHAR(4) NOT NULL,
    strike           NUMERIC(20, 8) NOT NULL,
    expiry           DATE,
    side             VARCHAR(8) NOT NULL,
    quantity         INTEGER NOT NULL,
    lot_size         INTEGER NOT NULL,
    entry_price      NUMERIC(20, 8) NOT NULL,
    last_mark_price  NUMERIC(20, 8),
    prev_close_mark  NUMERIC(20, 8),
    unrealized_pnl   NUMERIC(20, 4) NOT NULL DEFAULT 0,
    realized_pnl     NUMERIC(20, 4) NOT NULL DEFAULT 0,
    commission       NUMERIC(20, 4) NOT NULL DEFAULT 0,
    status           VARCHAR(16) NOT NULL DEFAULT 'open',
    opened_at        TIMESTAMPTZ NOT NULL,
    closed_at        TIMESTAMPTZ,
    last_updated     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT ck_opt_open_legs_inst_type CHECK (instrument_type = 'OPT'),
    CONSTRAINT ck_opt_open_legs_opt_type CHECK (option_type IN ('CE', 'PE')),
    CONSTRAINT ck_opt_open_legs_side CHECK (side IN ('BUY', 'SELL')),
    CONSTRAINT ck_opt_open_legs_status CHECK (status IN ('open', 'closed'))
);

CREATE INDEX IF NOT EXISTS ix_opt_open_legs_structure
    ON option_open_legs (structure_id);

CREATE INDEX IF NOT EXISTS ix_opt_open_legs_instance_status
    ON option_open_legs (instance_id, status);

CREATE TABLE IF NOT EXISTS risk_session_state (
    session_date      DATE NOT NULL,
    slice_key         VARCHAR(64) NOT NULL,
    peak_daily_pnl    NUMERIC(20, 4) NOT NULL DEFAULT 0,
    current_daily_pnl NUMERIC(20, 4) NOT NULL DEFAULT 0,
    deployed_capital  NUMERIC(20, 4) NOT NULL DEFAULT 0,
    halted            BOOLEAN NOT NULL DEFAULT FALSE,
    halt_reason       TEXT,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (session_date, slice_key)
);

CREATE INDEX IF NOT EXISTS ix_risk_session_state_date
    ON risk_session_state (session_date);

CREATE TABLE IF NOT EXISTS schema_migrations (
    version     VARCHAR(32) PRIMARY KEY,
    description TEXT,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

INSERT INTO schema_migrations (version, description)
VALUES (
    '024',
    'Add runner_instances, option_open_structures, option_open_legs, and risk_session_state for PostgreSQL SSoT'
)
ON CONFLICT (version) DO NOTHING;

COMMIT;
