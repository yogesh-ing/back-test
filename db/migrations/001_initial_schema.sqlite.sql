-- =============================================================================
-- Forward Testing Simulator — Initial Schema (SQLite dev variant)
-- Migration : 001_initial_schema
-- Engine    : SQLite 3.35+
-- =============================================================================
--
-- This is the LOCAL DEVELOPMENT mirror of 001_initial_schema.sql.
-- PostgreSQL is the production target; keep both files in sync when the
-- schema changes.
--
-- DIFFERENCES FROM THE POSTGRES FILE (and why)
-- -----------------------------------------------------------------------------
--  UUID           -> TEXT. SQLite has no UUID type and no gen_random_uuid().
--                   The application layer MUST supply the id (uuid4().hex or
--                   str(uuid4())). The ORM does this already via a Python-side
--                   default, so behaviour is identical from the app's view.
--  BIGSERIAL      -> INTEGER PRIMARY KEY AUTOINCREMENT (SQLite's rowid alias).
--  TIMESTAMPTZ    -> TEXT storing ISO-8601 UTC. SQLite has no real date type
--                   and NO timezone awareness. Always write UTC.
--  JSONB          -> TEXT. Use json_extract() for querying (JSON1 extension).
--  NUMERIC(p,s)   -> NUMERIC. SQLite ignores precision/scale and stores these
--                   as REAL/INTEGER. THIS IS A REAL LIMITATION: money is not
--                   exact here. Fine for dev, never for production accounting.
--  COMMENT ON     -> removed (unsupported); comments are inline instead.
--  GIN index      -> removed (no equivalent).
--  Trigger syntax -> rewritten to SQLite's CREATE TRIGGER form.
--  LATERAL join   -> rewritten as correlated subqueries in v_portfolio_summary.
--
-- IMPORTANT: SQLite does NOT enforce foreign keys unless you turn them on,
-- per connection, every time:
--     PRAGMA foreign_keys = ON;
-- =============================================================================

PRAGMA foreign_keys = ON;

BEGIN;

CREATE TABLE IF NOT EXISTS schema_migrations (
    version     TEXT PRIMARY KEY,
    description TEXT NOT NULL,
    applied_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- =============================================================================
-- 1. PORTFOLIOS
-- =============================================================================
CREATE TABLE IF NOT EXISTS portfolios (
    portfolio_id    TEXT    PRIMARY KEY,
    name            TEXT    NOT NULL,
    initial_capital NUMERIC NOT NULL,
    current_cash    NUMERIC NOT NULL,
    base_currency   TEXT    NOT NULL DEFAULT 'INR',
    status          TEXT    NOT NULL DEFAULT 'active',
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT    NOT NULL DEFAULT (datetime('now')),

    CONSTRAINT uq_portfolios_name        UNIQUE (name),
    CONSTRAINT ck_portfolios_status      CHECK (status IN ('active','paused','stopped')),
    CONSTRAINT ck_portfolios_capital_pos CHECK (initial_capital > 0)
);

CREATE INDEX IF NOT EXISTS ix_portfolios_status ON portfolios (status);

CREATE TRIGGER IF NOT EXISTS trg_portfolios_updated_at
AFTER UPDATE ON portfolios
FOR EACH ROW
BEGIN
    UPDATE portfolios SET updated_at = datetime('now')
    WHERE portfolio_id = NEW.portfolio_id;
END;

-- =============================================================================
-- 2. POSITIONS
-- =============================================================================
CREATE TABLE IF NOT EXISTS positions (
    position_id         TEXT    PRIMARY KEY,
    portfolio_id        TEXT    NOT NULL,
    symbol              TEXT    NOT NULL,
    exchange            TEXT    NOT NULL DEFAULT 'NSE',
    position_type       TEXT    NOT NULL DEFAULT 'long',
    quantity            NUMERIC NOT NULL DEFAULT 0,
    average_entry_price NUMERIC NOT NULL,
    current_price       NUMERIC,
    unrealized_pnl      NUMERIC NOT NULL DEFAULT 0,
    realized_pnl        NUMERIC NOT NULL DEFAULT 0,
    commission_total    NUMERIC NOT NULL DEFAULT 0,
    opened_at           TEXT    NOT NULL DEFAULT (datetime('now')),
    closed_at           TEXT,
    last_updated        TEXT    NOT NULL DEFAULT (datetime('now')),
    status              TEXT    NOT NULL DEFAULT 'open',

    CONSTRAINT fk_positions_portfolio FOREIGN KEY (portfolio_id)
        REFERENCES portfolios (portfolio_id) ON DELETE CASCADE,

    CONSTRAINT ck_positions_status CHECK (status IN ('open','closed')),
    CONSTRAINT ck_positions_type   CHECK (position_type IN ('long','short')),
    CONSTRAINT ck_positions_avgpx  CHECK (average_entry_price >= 0),
    CONSTRAINT ck_positions_closed_consistency CHECK (
        (status = 'closed' AND closed_at IS NOT NULL) OR
        (status = 'open'   AND closed_at IS NULL)
    ),
    CONSTRAINT ck_positions_qty_sign CHECK (
        status = 'closed'
        OR (position_type = 'long'  AND quantity >= 0)
        OR (position_type = 'short' AND quantity <= 0)
    )
);

-- Partial index: at most one OPEN position per portfolio+symbol.
CREATE UNIQUE INDEX IF NOT EXISTS uq_positions_one_open_per_symbol
    ON positions (portfolio_id, symbol) WHERE status = 'open';
CREATE INDEX IF NOT EXISTS ix_positions_portfolio_status ON positions (portfolio_id, status);
CREATE INDEX IF NOT EXISTS ix_positions_symbol           ON positions (symbol);
CREATE INDEX IF NOT EXISTS ix_positions_opened_at        ON positions (opened_at DESC);

-- =============================================================================
-- 3. ORDERS
-- =============================================================================
CREATE TABLE IF NOT EXISTS orders (
    order_id           TEXT    PRIMARY KEY,
    portfolio_id       TEXT    NOT NULL,
    position_id        TEXT,
    symbol             TEXT    NOT NULL,
    exchange           TEXT    NOT NULL DEFAULT 'NSE',
    side               TEXT    NOT NULL,
    order_type         TEXT    NOT NULL,
    quantity           NUMERIC NOT NULL,
    filled_quantity    NUMERIC NOT NULL DEFAULT 0,
    limit_price        NUMERIC,
    stop_price         NUMERIC,
    trailing_amount    NUMERIC,
    average_fill_price NUMERIC,
    time_in_force      TEXT    NOT NULL DEFAULT 'day',
    status             TEXT    NOT NULL DEFAULT 'pending',
    rejection_reason   TEXT,
    client_order_id    TEXT,
    broker_order_id    TEXT,
    submitted_at       TEXT    NOT NULL DEFAULT (datetime('now')),
    filled_at          TEXT,
    cancelled_at       TEXT,
    updated_at         TEXT    NOT NULL DEFAULT (datetime('now')),

    CONSTRAINT fk_orders_portfolio FOREIGN KEY (portfolio_id)
        REFERENCES portfolios (portfolio_id) ON DELETE CASCADE,
    CONSTRAINT fk_orders_position FOREIGN KEY (position_id)
        REFERENCES positions (position_id) ON DELETE SET NULL,

    CONSTRAINT ck_orders_side   CHECK (side IN ('buy','sell')),
    CONSTRAINT ck_orders_type   CHECK (order_type IN ('market','limit','stop','stop_limit','trailing_stop')),
    CONSTRAINT ck_orders_tif    CHECK (time_in_force IN ('day','gtc','ioc','fok')),
    CONSTRAINT ck_orders_status CHECK (status IN ('pending','partial','filled','cancelled','rejected')),
    CONSTRAINT ck_orders_qty_pos    CHECK (quantity > 0),
    CONSTRAINT ck_orders_filled_qty CHECK (filled_quantity >= 0 AND filled_quantity <= quantity),
    CONSTRAINT ck_orders_limit_price_required CHECK (
        order_type NOT IN ('limit','stop_limit') OR limit_price IS NOT NULL),
    CONSTRAINT ck_orders_stop_price_required CHECK (
        order_type NOT IN ('stop','stop_limit') OR stop_price IS NOT NULL),
    CONSTRAINT ck_orders_trailing_required CHECK (
        order_type <> 'trailing_stop' OR trailing_amount IS NOT NULL),
    CONSTRAINT ck_orders_rejection_reason CHECK (
        status <> 'rejected' OR rejection_reason IS NOT NULL),
    CONSTRAINT ck_orders_filled_consistency CHECK (
        status <> 'filled' OR (filled_at IS NOT NULL AND filled_quantity = quantity))
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_orders_client_order_id
    ON orders (portfolio_id, client_order_id) WHERE client_order_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_orders_portfolio_status ON orders (portfolio_id, status);
CREATE INDEX IF NOT EXISTS ix_orders_symbol_submitted ON orders (symbol, submitted_at DESC);
CREATE INDEX IF NOT EXISTS ix_orders_position         ON orders (position_id);
CREATE INDEX IF NOT EXISTS ix_orders_working
    ON orders (portfolio_id, symbol) WHERE status IN ('pending','partial');

CREATE TRIGGER IF NOT EXISTS trg_orders_updated_at
AFTER UPDATE ON orders
FOR EACH ROW
BEGIN
    UPDATE orders SET updated_at = datetime('now') WHERE order_id = NEW.order_id;
END;

-- =============================================================================
-- 4. FILLS
-- =============================================================================
CREATE TABLE IF NOT EXISTS fills (
    fill_id         TEXT    PRIMARY KEY,
    order_id        TEXT    NOT NULL,
    position_id     TEXT,
    symbol          TEXT    NOT NULL,
    side            TEXT    NOT NULL,
    quantity        NUMERIC NOT NULL,
    fill_price      NUMERIC NOT NULL,
    commission      NUMERIC NOT NULL DEFAULT 0,
    slippage_bps    NUMERIC NOT NULL DEFAULT 0,
    slippage_amount NUMERIC NOT NULL DEFAULT 0,
    exchange_fees   NUMERIC NOT NULL DEFAULT 0,
    regulatory_fees NUMERIC NOT NULL DEFAULT 0,
    liquidity_flag  TEXT,
    reference_price NUMERIC,
    filled_at       TEXT    NOT NULL DEFAULT (datetime('now')),
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),

    CONSTRAINT fk_fills_order FOREIGN KEY (order_id)
        REFERENCES orders (order_id) ON DELETE CASCADE,
    CONSTRAINT fk_fills_position FOREIGN KEY (position_id)
        REFERENCES positions (position_id) ON DELETE SET NULL,

    CONSTRAINT ck_fills_side      CHECK (side IN ('buy','sell')),
    CONSTRAINT ck_fills_liquidity CHECK (liquidity_flag IS NULL OR liquidity_flag IN ('maker','taker')),
    CONSTRAINT ck_fills_qty_pos   CHECK (quantity > 0),
    CONSTRAINT ck_fills_price_pos CHECK (fill_price > 0),
    CONSTRAINT ck_fills_fees_nonneg CHECK (
        commission >= 0 AND exchange_fees >= 0 AND regulatory_fees >= 0)
);

CREATE INDEX IF NOT EXISTS ix_fills_order     ON fills (order_id);
CREATE INDEX IF NOT EXISTS ix_fills_position  ON fills (position_id);
CREATE INDEX IF NOT EXISTS ix_fills_filled_at ON fills (filled_at DESC);
CREATE INDEX IF NOT EXISTS ix_fills_symbol    ON fills (symbol, filled_at DESC);

-- =============================================================================
-- 5. TRADES
-- =============================================================================
CREATE TABLE IF NOT EXISTS trades (
    trade_id               TEXT    PRIMARY KEY,
    portfolio_id           TEXT    NOT NULL,
    position_id            TEXT,
    symbol                 TEXT    NOT NULL,
    strategy_name          TEXT,
    direction              TEXT    NOT NULL DEFAULT 'long',
    entry_order_id         TEXT,
    exit_order_id          TEXT,
    quantity               NUMERIC NOT NULL,
    entry_price            NUMERIC NOT NULL,
    exit_price             NUMERIC NOT NULL,
    entry_time             TEXT    NOT NULL,
    exit_time              TEXT    NOT NULL,
    gross_pnl              NUMERIC NOT NULL,
    net_pnl                NUMERIC NOT NULL,
    commission_total       NUMERIC NOT NULL DEFAULT 0,
    slippage_total         NUMERIC NOT NULL DEFAULT 0,
    holding_period_minutes INTEGER,
    return_percentage      NUMERIC,
    exit_reason            TEXT,
    created_at             TEXT    NOT NULL DEFAULT (datetime('now')),

    CONSTRAINT fk_trades_portfolio FOREIGN KEY (portfolio_id)
        REFERENCES portfolios (portfolio_id) ON DELETE CASCADE,
    CONSTRAINT fk_trades_position FOREIGN KEY (position_id)
        REFERENCES positions (position_id) ON DELETE SET NULL,
    CONSTRAINT fk_trades_entry_order FOREIGN KEY (entry_order_id)
        REFERENCES orders (order_id) ON DELETE SET NULL,
    CONSTRAINT fk_trades_exit_order FOREIGN KEY (exit_order_id)
        REFERENCES orders (order_id) ON DELETE SET NULL,

    CONSTRAINT ck_trades_direction  CHECK (direction IN ('long','short')),
    CONSTRAINT ck_trades_qty_pos    CHECK (quantity > 0),
    CONSTRAINT ck_trades_time_order CHECK (exit_time >= entry_time),
    CONSTRAINT ck_trades_exit_reason CHECK (
        exit_reason IS NULL OR exit_reason IN
        ('signal','stop_loss','take_profit','trailing_stop','time_stop','risk_limit','manual','eod_flat'))
);

CREATE INDEX IF NOT EXISTS ix_trades_portfolio_exit ON trades (portfolio_id, exit_time DESC);
CREATE INDEX IF NOT EXISTS ix_trades_symbol         ON trades (symbol);
CREATE INDEX IF NOT EXISTS ix_trades_strategy       ON trades (strategy_name);
CREATE INDEX IF NOT EXISTS ix_trades_net_pnl        ON trades (portfolio_id, net_pnl);

-- =============================================================================
-- 6. EQUITY_CURVE
-- =============================================================================
CREATE TABLE IF NOT EXISTS equity_curve (
    equity_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    portfolio_id   TEXT    NOT NULL,
    ts             TEXT    NOT NULL,
    total_equity   NUMERIC NOT NULL,
    cash           NUMERIC NOT NULL,
    position_value NUMERIC NOT NULL DEFAULT 0,
    daily_pnl      NUMERIC NOT NULL DEFAULT 0,
    cumulative_pnl NUMERIC NOT NULL DEFAULT 0,
    drawdown       NUMERIC NOT NULL DEFAULT 0,
    drawdown_pct   NUMERIC NOT NULL DEFAULT 0,

    CONSTRAINT fk_equity_portfolio FOREIGN KEY (portfolio_id)
        REFERENCES portfolios (portfolio_id) ON DELETE CASCADE,
    CONSTRAINT uq_equity_portfolio_ts UNIQUE (portfolio_id, ts)
);

CREATE INDEX IF NOT EXISTS ix_equity_portfolio_ts ON equity_curve (portfolio_id, ts DESC);

-- =============================================================================
-- 7. MARKET_DATA_CACHE
-- =============================================================================
CREATE TABLE IF NOT EXISTS market_data_cache (
    data_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol      TEXT    NOT NULL,
    exchange    TEXT    NOT NULL DEFAULT 'NSE',
    timeframe   TEXT    NOT NULL,
    ts          TEXT    NOT NULL,
    open        NUMERIC NOT NULL,
    high        NUMERIC NOT NULL,
    low         NUMERIC NOT NULL,
    close       NUMERIC NOT NULL,
    volume      NUMERIC NOT NULL DEFAULT 0,
    bid         NUMERIC,
    ask         NUMERIC,
    source      TEXT    NOT NULL DEFAULT 'mstock',
    ingested_at TEXT    NOT NULL DEFAULT (datetime('now')),

    CONSTRAINT ck_mdc_timeframe CHECK (timeframe IN
        ('1min','3min','5min','15min','30min','60min','1hour','day','week','month')),
    CONSTRAINT ck_mdc_ohlc CHECK (
        high >= low AND high >= open AND high >= close
        AND low <= open AND low <= close),
    CONSTRAINT ck_mdc_prices_pos CHECK (open > 0 AND high > 0 AND low > 0 AND close > 0),
    CONSTRAINT ck_mdc_volume     CHECK (volume >= 0),
    CONSTRAINT ck_mdc_spread     CHECK (bid IS NULL OR ask IS NULL OR bid <= ask),
    CONSTRAINT uq_mdc_bar UNIQUE (symbol, exchange, timeframe, ts)
);

CREATE INDEX IF NOT EXISTS ix_mdc_symbol_tf_ts ON market_data_cache (symbol, timeframe, ts DESC);
CREATE INDEX IF NOT EXISTS ix_mdc_ts           ON market_data_cache (ts DESC);

-- =============================================================================
-- 8. PERFORMANCE_METRICS
-- =============================================================================
CREATE TABLE IF NOT EXISTS performance_metrics (
    metric_id               INTEGER PRIMARY KEY AUTOINCREMENT,
    portfolio_id            TEXT    NOT NULL,
    calculation_date        TEXT    NOT NULL,
    total_trades            INTEGER NOT NULL DEFAULT 0,
    winning_trades          INTEGER NOT NULL DEFAULT 0,
    losing_trades           INTEGER NOT NULL DEFAULT 0,
    win_rate                NUMERIC,
    avg_win                 NUMERIC,
    avg_loss                NUMERIC,
    largest_win             NUMERIC,
    largest_loss            NUMERIC,
    profit_factor           NUMERIC,
    expectancy              NUMERIC,
    sharpe_ratio            NUMERIC,
    sortino_ratio           NUMERIC,
    max_drawdown            NUMERIC,
    max_drawdown_percentage NUMERIC,
    total_return            NUMERIC,
    total_return_percentage NUMERIC,
    total_commission        NUMERIC NOT NULL DEFAULT 0,
    total_slippage          NUMERIC NOT NULL DEFAULT 0,
    calculated_at           TEXT    NOT NULL DEFAULT (datetime('now')),

    CONSTRAINT fk_perf_portfolio FOREIGN KEY (portfolio_id)
        REFERENCES portfolios (portfolio_id) ON DELETE CASCADE,
    CONSTRAINT ck_perf_counts CHECK (
        total_trades >= 0 AND winning_trades >= 0 AND losing_trades >= 0
        AND winning_trades + losing_trades <= total_trades),
    CONSTRAINT ck_perf_win_rate CHECK (win_rate IS NULL OR (win_rate >= 0 AND win_rate <= 1)),
    CONSTRAINT uq_perf_portfolio_date UNIQUE (portfolio_id, calculation_date)
);

CREATE INDEX IF NOT EXISTS ix_perf_portfolio_date ON performance_metrics (portfolio_id, calculation_date DESC);

-- =============================================================================
-- 9. STRATEGY_SIGNALS
-- =============================================================================
CREATE TABLE IF NOT EXISTS strategy_signals (
    signal_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    portfolio_id        TEXT    NOT NULL,
    symbol              TEXT    NOT NULL,
    strategy_name       TEXT,
    signal_type         TEXT    NOT NULL,
    direction           TEXT    NOT NULL,
    strength            NUMERIC,
    target_position     NUMERIC,
    bar_ts              TEXT,
    generated_at        TEXT    NOT NULL DEFAULT (datetime('now')),
    indicators_snapshot TEXT,   -- JSON document; query with json_extract()
    executed            INTEGER NOT NULL DEFAULT 0,
    order_id            TEXT,
    skip_reason         TEXT,

    CONSTRAINT fk_signals_portfolio FOREIGN KEY (portfolio_id)
        REFERENCES portfolios (portfolio_id) ON DELETE CASCADE,
    CONSTRAINT fk_signals_order FOREIGN KEY (order_id)
        REFERENCES orders (order_id) ON DELETE SET NULL,

    CONSTRAINT ck_signals_type      CHECK (signal_type IN ('entry','exit')),
    CONSTRAINT ck_signals_direction CHECK (direction IN ('long','short','flat')),
    CONSTRAINT ck_signals_strength  CHECK (strength IS NULL OR (strength >= 0 AND strength <= 1)),
    CONSTRAINT ck_signals_target    CHECK (target_position IS NULL OR (target_position >= -1 AND target_position <= 1)),
    CONSTRAINT ck_signals_executed  CHECK (executed IN (0,1))
);

CREATE INDEX IF NOT EXISTS ix_signals_portfolio_gen ON strategy_signals (portfolio_id, generated_at DESC);
CREATE INDEX IF NOT EXISTS ix_signals_symbol        ON strategy_signals (symbol, generated_at DESC);
CREATE INDEX IF NOT EXISTS ix_signals_unexecuted    ON strategy_signals (portfolio_id) WHERE executed = 0;

-- =============================================================================
-- 10. SYSTEM_LOGS
-- =============================================================================
CREATE TABLE IF NOT EXISTS system_logs (
    log_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    portfolio_id TEXT,
    ts           TEXT NOT NULL DEFAULT (datetime('now')),
    log_level    TEXT NOT NULL,
    component    TEXT NOT NULL,
    message      TEXT NOT NULL,
    stack_trace  TEXT,
    context      TEXT,   -- JSON document

    CONSTRAINT fk_logs_portfolio FOREIGN KEY (portfolio_id)
        REFERENCES portfolios (portfolio_id) ON DELETE SET NULL,
    CONSTRAINT ck_logs_level CHECK (log_level IN ('debug','info','warning','error','critical'))
);

CREATE INDEX IF NOT EXISTS ix_logs_ts           ON system_logs (ts DESC);
CREATE INDEX IF NOT EXISTS ix_logs_portfolio_ts ON system_logs (portfolio_id, ts DESC);
CREATE INDEX IF NOT EXISTS ix_logs_level_ts     ON system_logs (log_level, ts DESC);
CREATE INDEX IF NOT EXISTS ix_logs_component    ON system_logs (component, ts DESC);
CREATE INDEX IF NOT EXISTS ix_logs_errors       ON system_logs (ts DESC)
    WHERE log_level IN ('error','critical');

-- =============================================================================
-- VIEWS
-- =============================================================================
CREATE VIEW IF NOT EXISTS v_open_positions AS
SELECT
    p.position_id,
    p.portfolio_id,
    pf.name AS portfolio_name,
    p.symbol,
    p.position_type,
    p.quantity,
    p.average_entry_price,
    p.current_price,
    (p.quantity * COALESCE(p.current_price, p.average_entry_price)) AS market_value,
    (p.quantity * p.average_entry_price)                            AS cost_basis,
    p.unrealized_pnl,
    p.realized_pnl,
    p.opened_at
FROM positions p
JOIN portfolios pf ON pf.portfolio_id = p.portfolio_id
WHERE p.status = 'open';

-- No LATERAL in SQLite: correlated scalar subqueries instead.
CREATE VIEW IF NOT EXISTS v_portfolio_summary AS
SELECT
    pf.portfolio_id,
    pf.name,
    pf.status,
    pf.initial_capital,
    pf.current_cash,
    (SELECT ec.total_equity   FROM equity_curve ec WHERE ec.portfolio_id = pf.portfolio_id ORDER BY ec.ts DESC LIMIT 1) AS total_equity,
    (SELECT ec.cumulative_pnl FROM equity_curve ec WHERE ec.portfolio_id = pf.portfolio_id ORDER BY ec.ts DESC LIMIT 1) AS cumulative_pnl,
    (SELECT ec.drawdown_pct   FROM equity_curve ec WHERE ec.portfolio_id = pf.portfolio_id ORDER BY ec.ts DESC LIMIT 1) AS drawdown_pct,
    (SELECT ec.ts             FROM equity_curve ec WHERE ec.portfolio_id = pf.portfolio_id ORDER BY ec.ts DESC LIMIT 1) AS last_marked_at,
    (SELECT COUNT(*) FROM positions p WHERE p.portfolio_id = pf.portfolio_id AND p.status = 'open') AS open_positions,
    (SELECT COUNT(*) FROM trades t   WHERE t.portfolio_id = pf.portfolio_id)                        AS total_trades
FROM portfolios pf;

INSERT OR IGNORE INTO schema_migrations (version, description)
VALUES ('001', 'Initial forward testing schema: 10 tables, indexes, constraints, 2 views');

COMMIT;