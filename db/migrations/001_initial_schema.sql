-- =============================================================================
-- Forward Testing Simulator — Initial Schema
-- Migration : 001_initial_schema
-- Engine    : PostgreSQL 13+   (SQLite-compatible subset; see notes below)
-- Author    : Step 1, instructions/forword-testing.md
-- =============================================================================
--
-- DESIGN NOTES
-- -----------------------------------------------------------------------------
-- 1. ENUMS AS CHECK CONSTRAINTS
--    Native PostgreSQL ENUM types are avoided on purpose. Step 2 of the plan
--    requires the same schema to run on SQLite for local development, and
--    SQLite has no ENUM type. VARCHAR + CHECK is portable to both engines and
--    is far easier to evolve (adding a value is an ALTER of the constraint,
--    not an ALTER TYPE that locks the catalog).
--
-- 2. MONEY vs PRICE PRECISION
--    - Prices / quantities : NUMERIC(20, 8)  (handles fractional units)
--    - Cash / P&L amounts  : NUMERIC(20, 4)  (currency, 4dp for fee accuracy)
--    - Ratios / percentages: NUMERIC(12, 6)
--    NEVER use DOUBLE PRECISION for money. Float rounding silently corrupts
--    reconciliation between the equity curve and the sum of trade P&L.
--
-- 3. PRIMARY KEYS
--    - Entity tables (portfolios, positions, orders, fills, trades) use UUID
--      so IDs can be generated client-side before the row is written. This
--      matters for orders: the Order object needs an ID at submit() time,
--      before the DB round-trip completes.
--    - Append-only time-series / log tables use BIGSERIAL. They are written
--      in bulk and never referenced by client code before insert.
--
-- 4. COLUMN NAME `ts` INSTEAD OF `timestamp`
--    The plan document specifies a `timestamp` column on several tables.
--    `timestamp` is a type name in SQL; using it as a column name forces
--    quoting everywhere and breaks some ORM reflection. Renamed to `ts`.
--    This is the only intentional deviation from the spec's column names.
--
-- 5. IDEMPOTENCY
--    Every statement uses IF NOT EXISTS so the file can be re-run safely.
--
-- =============================================================================

BEGIN;

-- -----------------------------------------------------------------------------
-- UUID generation
-- -----------------------------------------------------------------------------
-- PostgreSQL 13+ ships gen_random_uuid() in core, so no extension is needed.
-- For PG 11/12 the function lives in pgcrypto. We try to install it, but we
-- must NOT let a failure abort the migration: this whole file runs in one
-- transaction, and on managed hosts (RDS, Cloud SQL, Supabase) a non-superuser
-- role cannot CREATE EXTENSION. The DO block swallows the error and we then
-- assert that gen_random_uuid() is actually callable — a clear, early failure
-- beats 80 confusing "transaction is aborted" errors.
DO $$
BEGIN
    IF to_regproc('gen_random_uuid') IS NULL THEN
        BEGIN
            CREATE EXTENSION IF NOT EXISTS pgcrypto;
        EXCEPTION WHEN OTHERS THEN
            RAISE NOTICE 'Could not create pgcrypto extension: %', SQLERRM;
        END;
    END IF;

    IF to_regproc('gen_random_uuid') IS NULL THEN
        RAISE EXCEPTION
            'gen_random_uuid() is unavailable. Upgrade to PostgreSQL 13+, or ask a superuser to run: CREATE EXTENSION pgcrypto;';
    END IF;
END
$$;


-- -----------------------------------------------------------------------------
-- Migration bookkeeping
-- -----------------------------------------------------------------------------
-- Tracks which migration files have been applied when running SQL by hand.
-- Alembic maintains its own `alembic_version` table; this one is for the
-- manual path so both approaches can coexist without confusion.
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     VARCHAR(64)  PRIMARY KEY,
    description TEXT         NOT NULL,
    applied_at  TIMESTAMPTZ  NOT NULL DEFAULT now()
);


-- -----------------------------------------------------------------------------
-- Shared trigger function: keep updated_at honest
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;


-- =============================================================================
-- 1. PORTFOLIOS
-- =============================================================================
CREATE TABLE IF NOT EXISTS portfolios (
    portfolio_id     UUID          PRIMARY KEY DEFAULT gen_random_uuid(),
    name             VARCHAR(128)  NOT NULL,
    initial_capital  NUMERIC(20,4) NOT NULL,
    current_cash     NUMERIC(20,4) NOT NULL,
    base_currency    CHAR(3)       NOT NULL DEFAULT 'INR',
    status           VARCHAR(16)   NOT NULL DEFAULT 'active',
    created_at       TIMESTAMPTZ   NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ   NOT NULL DEFAULT now(),

    CONSTRAINT uq_portfolios_name        UNIQUE (name),
    CONSTRAINT ck_portfolios_status      CHECK (status IN ('active','paused','stopped')),
    CONSTRAINT ck_portfolios_capital_pos CHECK (initial_capital > 0)
);

COMMENT ON TABLE  portfolios IS 'One row per forward-testing run. The root aggregate.';
COMMENT ON COLUMN portfolios.current_cash IS 'Settled cash only. Position value is NOT included; see equity_curve.total_equity.';
COMMENT ON COLUMN portfolios.status IS 'active = loop trading | paused = loop running, no new orders | stopped = terminal';

DROP TRIGGER IF EXISTS trg_portfolios_updated_at ON portfolios;
CREATE TRIGGER trg_portfolios_updated_at
    BEFORE UPDATE ON portfolios
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE INDEX IF NOT EXISTS ix_portfolios_status ON portfolios (status);


-- =============================================================================
-- 2. POSITIONS
-- =============================================================================
CREATE TABLE IF NOT EXISTS positions (
    position_id         UUID          PRIMARY KEY DEFAULT gen_random_uuid(),
    portfolio_id        UUID          NOT NULL,
    symbol              VARCHAR(64)   NOT NULL,
    exchange            VARCHAR(16)   NOT NULL DEFAULT 'NSE',
    position_type       VARCHAR(8)    NOT NULL DEFAULT 'long',
    quantity            NUMERIC(20,8) NOT NULL DEFAULT 0,
    average_entry_price NUMERIC(20,8) NOT NULL,
    current_price       NUMERIC(20,8),
    unrealized_pnl      NUMERIC(20,4) NOT NULL DEFAULT 0,
    realized_pnl        NUMERIC(20,4) NOT NULL DEFAULT 0,
    commission_total    NUMERIC(20,4) NOT NULL DEFAULT 0,
    opened_at           TIMESTAMPTZ   NOT NULL DEFAULT now(),
    closed_at           TIMESTAMPTZ,
    last_updated        TIMESTAMPTZ   NOT NULL DEFAULT now(),
    status              VARCHAR(8)    NOT NULL DEFAULT 'open',

    CONSTRAINT fk_positions_portfolio
        FOREIGN KEY (portfolio_id) REFERENCES portfolios (portfolio_id)
        ON DELETE CASCADE,

    CONSTRAINT ck_positions_status  CHECK (status IN ('open','closed')),
    CONSTRAINT ck_positions_type    CHECK (position_type IN ('long','short')),
    CONSTRAINT ck_positions_avgpx   CHECK (average_entry_price >= 0),

    -- A closed position must have a close timestamp and vice versa.
    CONSTRAINT ck_positions_closed_consistency CHECK (
        (status = 'closed' AND closed_at IS NOT NULL) OR
        (status = 'open'   AND closed_at IS NULL)
    ),

    -- Direction must agree with the sign of quantity while the position lives.
    CONSTRAINT ck_positions_qty_sign CHECK (
        status = 'closed'
        OR (position_type = 'long'  AND quantity >= 0)
        OR (position_type = 'short' AND quantity <= 0)
    )
);

COMMENT ON TABLE  positions IS 'Net open exposure per symbol. Closed rows are retained as history.';
COMMENT ON COLUMN positions.quantity IS 'Signed: positive = long, negative = short. Zero once closed.';
COMMENT ON COLUMN positions.realized_pnl IS 'Accumulated from partial closes. Gross of commission; commission_total tracked separately.';

-- The critical invariant: at most ONE open position per portfolio+symbol.
-- A partial unique index enforces this while still allowing unlimited closed
-- history rows for the same pair.
CREATE UNIQUE INDEX IF NOT EXISTS uq_positions_one_open_per_symbol
    ON positions (portfolio_id, symbol)
    WHERE status = 'open';

CREATE INDEX IF NOT EXISTS ix_positions_portfolio_status ON positions (portfolio_id, status);
CREATE INDEX IF NOT EXISTS ix_positions_symbol           ON positions (symbol);
CREATE INDEX IF NOT EXISTS ix_positions_opened_at        ON positions (opened_at DESC);


-- =============================================================================
-- 3. ORDERS
-- =============================================================================
CREATE TABLE IF NOT EXISTS orders (
    order_id            UUID          PRIMARY KEY DEFAULT gen_random_uuid(),
    portfolio_id        UUID          NOT NULL,
    position_id         UUID,
    symbol              VARCHAR(64)   NOT NULL,
    exchange            VARCHAR(16)   NOT NULL DEFAULT 'NSE',
    side                VARCHAR(4)    NOT NULL,
    order_type          VARCHAR(16)   NOT NULL,
    quantity            NUMERIC(20,8) NOT NULL,
    filled_quantity     NUMERIC(20,8) NOT NULL DEFAULT 0,
    limit_price         NUMERIC(20,8),
    stop_price          NUMERIC(20,8),
    trailing_amount     NUMERIC(20,8),
    average_fill_price  NUMERIC(20,8),
    time_in_force       VARCHAR(8)    NOT NULL DEFAULT 'day',
    status              VARCHAR(16)   NOT NULL DEFAULT 'pending',
    rejection_reason    TEXT,
    client_order_id     VARCHAR(64),
    broker_order_id     VARCHAR(64),
    submitted_at        TIMESTAMPTZ   NOT NULL DEFAULT now(),
    filled_at           TIMESTAMPTZ,
    cancelled_at        TIMESTAMPTZ,
    updated_at          TIMESTAMPTZ   NOT NULL DEFAULT now(),

    CONSTRAINT fk_orders_portfolio
        FOREIGN KEY (portfolio_id) REFERENCES portfolios (portfolio_id)
        ON DELETE CASCADE,
    CONSTRAINT fk_orders_position
        FOREIGN KEY (position_id) REFERENCES positions (position_id)
        ON DELETE SET NULL,

    CONSTRAINT ck_orders_side   CHECK (side IN ('buy','sell')),
    CONSTRAINT ck_orders_type   CHECK (order_type IN ('market','limit','stop','stop_limit','trailing_stop')),
    CONSTRAINT ck_orders_tif    CHECK (time_in_force IN ('day','gtc','ioc','fok')),
    CONSTRAINT ck_orders_status CHECK (status IN ('pending','partial','filled','cancelled','rejected')),

    CONSTRAINT ck_orders_qty_pos     CHECK (quantity > 0),
    CONSTRAINT ck_orders_filled_qty  CHECK (filled_quantity >= 0 AND filled_quantity <= quantity),

    -- Price fields must be present for the order types that require them.
    CONSTRAINT ck_orders_limit_price_required CHECK (
        order_type NOT IN ('limit','stop_limit') OR limit_price IS NOT NULL
    ),
    CONSTRAINT ck_orders_stop_price_required CHECK (
        order_type NOT IN ('stop','stop_limit') OR stop_price IS NOT NULL
    ),
    CONSTRAINT ck_orders_trailing_required CHECK (
        order_type <> 'trailing_stop' OR trailing_amount IS NOT NULL
    ),

    -- A rejected order must say why.
    CONSTRAINT ck_orders_rejection_reason CHECK (
        status <> 'rejected' OR rejection_reason IS NOT NULL
    ),
    -- A filled order must be fully filled and stamped.
    CONSTRAINT ck_orders_filled_consistency CHECK (
        status <> 'filled' OR (filled_at IS NOT NULL AND filled_quantity = quantity)
    )
);

COMMENT ON TABLE  orders IS 'Order lifecycle records. Immutable except for status/fill progression.';
COMMENT ON COLUMN orders.client_order_id IS 'Idempotency key generated by the engine before submission.';
COMMENT ON COLUMN orders.broker_order_id IS 'Broker-assigned ID. NULL in pure simulation.';
COMMENT ON COLUMN orders.average_fill_price IS 'Weighted average across all fills. Denormalised from fills for query speed.';

DROP TRIGGER IF EXISTS trg_orders_updated_at ON orders;
CREATE TRIGGER trg_orders_updated_at
    BEFORE UPDATE ON orders
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- Idempotency: the engine must never submit the same client_order_id twice.
CREATE UNIQUE INDEX IF NOT EXISTS uq_orders_client_order_id
    ON orders (portfolio_id, client_order_id)
    WHERE client_order_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_orders_portfolio_status ON orders (portfolio_id, status);
CREATE INDEX IF NOT EXISTS ix_orders_symbol_submitted ON orders (symbol, submitted_at DESC);
CREATE INDEX IF NOT EXISTS ix_orders_position        ON orders (position_id);

-- Hot path: the execution loop scans working orders every tick.
CREATE INDEX IF NOT EXISTS ix_orders_working
    ON orders (portfolio_id, symbol)
    WHERE status IN ('pending','partial');


-- =============================================================================
-- 4. FILLS
-- =============================================================================
CREATE TABLE IF NOT EXISTS fills (
    fill_id          UUID          PRIMARY KEY DEFAULT gen_random_uuid(),
    order_id         UUID          NOT NULL,
    position_id      UUID,
    symbol           VARCHAR(64)   NOT NULL,
    side             VARCHAR(4)    NOT NULL,
    quantity         NUMERIC(20,8) NOT NULL,
    fill_price       NUMERIC(20,8) NOT NULL,
    commission       NUMERIC(20,4) NOT NULL DEFAULT 0,
    slippage_bps     NUMERIC(12,6) NOT NULL DEFAULT 0,
    slippage_amount  NUMERIC(20,4) NOT NULL DEFAULT 0,
    exchange_fees    NUMERIC(20,4) NOT NULL DEFAULT 0,
    regulatory_fees  NUMERIC(20,4) NOT NULL DEFAULT 0,
    liquidity_flag   VARCHAR(8),
    reference_price  NUMERIC(20,8),
    filled_at        TIMESTAMPTZ   NOT NULL DEFAULT now(),
    created_at       TIMESTAMPTZ   NOT NULL DEFAULT now(),

    CONSTRAINT fk_fills_order
        FOREIGN KEY (order_id) REFERENCES orders (order_id)
        ON DELETE CASCADE,
    CONSTRAINT fk_fills_position
        FOREIGN KEY (position_id) REFERENCES positions (position_id)
        ON DELETE SET NULL,

    CONSTRAINT ck_fills_side      CHECK (side IN ('buy','sell')),
    CONSTRAINT ck_fills_liquidity CHECK (liquidity_flag IS NULL OR liquidity_flag IN ('maker','taker')),
    CONSTRAINT ck_fills_qty_pos   CHECK (quantity > 0),
    CONSTRAINT ck_fills_price_pos CHECK (fill_price > 0),
    CONSTRAINT ck_fills_fees_nonneg CHECK (
        commission >= 0 AND exchange_fees >= 0 AND regulatory_fees >= 0
    )
);

COMMENT ON TABLE  fills IS 'Individual executions. Append-only and immutable after insert.';
COMMENT ON COLUMN fills.reference_price IS 'Pre-slippage decision price. fill_price - reference_price = realised slippage.';
COMMENT ON COLUMN fills.slippage_bps IS 'Signed basis points vs reference_price. Positive = adverse to the order side.';

CREATE INDEX IF NOT EXISTS ix_fills_order     ON fills (order_id);
CREATE INDEX IF NOT EXISTS ix_fills_position  ON fills (position_id);
CREATE INDEX IF NOT EXISTS ix_fills_filled_at ON fills (filled_at DESC);
CREATE INDEX IF NOT EXISTS ix_fills_symbol    ON fills (symbol, filled_at DESC);


-- =============================================================================
-- 5. TRADES  (matched round-trips: entry -> exit)
-- =============================================================================
CREATE TABLE IF NOT EXISTS trades (
    trade_id               UUID          PRIMARY KEY DEFAULT gen_random_uuid(),
    portfolio_id           UUID          NOT NULL,
    position_id            UUID,
    symbol                 VARCHAR(64)   NOT NULL,
    strategy_name          VARCHAR(64),
    direction              VARCHAR(8)    NOT NULL DEFAULT 'long',
    entry_order_id         UUID,
    exit_order_id          UUID,
    quantity               NUMERIC(20,8) NOT NULL,
    entry_price            NUMERIC(20,8) NOT NULL,
    exit_price             NUMERIC(20,8) NOT NULL,
    entry_time             TIMESTAMPTZ   NOT NULL,
    exit_time              TIMESTAMPTZ   NOT NULL,
    gross_pnl              NUMERIC(20,4) NOT NULL,
    net_pnl                NUMERIC(20,4) NOT NULL,
    commission_total       NUMERIC(20,4) NOT NULL DEFAULT 0,
    slippage_total         NUMERIC(20,4) NOT NULL DEFAULT 0,
    holding_period_minutes INTEGER,
    return_percentage      NUMERIC(12,6),
    exit_reason            VARCHAR(32),
    created_at             TIMESTAMPTZ   NOT NULL DEFAULT now(),

    CONSTRAINT fk_trades_portfolio
        FOREIGN KEY (portfolio_id) REFERENCES portfolios (portfolio_id)
        ON DELETE CASCADE,
    CONSTRAINT fk_trades_position
        FOREIGN KEY (position_id) REFERENCES positions (position_id)
        ON DELETE SET NULL,
    CONSTRAINT fk_trades_entry_order
        FOREIGN KEY (entry_order_id) REFERENCES orders (order_id)
        ON DELETE SET NULL,
    CONSTRAINT fk_trades_exit_order
        FOREIGN KEY (exit_order_id) REFERENCES orders (order_id)
        ON DELETE SET NULL,

    CONSTRAINT ck_trades_direction   CHECK (direction IN ('long','short')),
    CONSTRAINT ck_trades_qty_pos     CHECK (quantity > 0),
    CONSTRAINT ck_trades_time_order  CHECK (exit_time >= entry_time),
    CONSTRAINT ck_trades_exit_reason CHECK (
        exit_reason IS NULL OR exit_reason IN
        ('signal','stop_loss','take_profit','trailing_stop','time_stop','risk_limit','manual','eod_flat')
    )
);

COMMENT ON TABLE  trades IS 'Closed round-trips. Written only when a position is fully or partially closed.';
COMMENT ON COLUMN trades.gross_pnl IS 'Price P&L before costs: (exit-entry)*qty, sign-adjusted for direction.';
COMMENT ON COLUMN trades.net_pnl IS 'gross_pnl - commission_total - slippage_total. This is what hits the equity curve.';
COMMENT ON COLUMN trades.exit_reason IS 'Attribution for the trade analyzer (Step 18).';

CREATE INDEX IF NOT EXISTS ix_trades_portfolio_exit ON trades (portfolio_id, exit_time DESC);
CREATE INDEX IF NOT EXISTS ix_trades_symbol         ON trades (symbol);
CREATE INDEX IF NOT EXISTS ix_trades_strategy       ON trades (strategy_name);
CREATE INDEX IF NOT EXISTS ix_trades_net_pnl        ON trades (portfolio_id, net_pnl);


-- =============================================================================
-- 6. EQUITY_CURVE
-- =============================================================================
CREATE TABLE IF NOT EXISTS equity_curve (
    equity_id      BIGSERIAL     PRIMARY KEY,
    portfolio_id   UUID          NOT NULL,
    ts             TIMESTAMPTZ   NOT NULL,
    total_equity   NUMERIC(20,4) NOT NULL,
    cash           NUMERIC(20,4) NOT NULL,
    position_value NUMERIC(20,4) NOT NULL DEFAULT 0,
    daily_pnl      NUMERIC(20,4) NOT NULL DEFAULT 0,
    cumulative_pnl NUMERIC(20,4) NOT NULL DEFAULT 0,
    drawdown       NUMERIC(20,4) NOT NULL DEFAULT 0,
    drawdown_pct   NUMERIC(12,6) NOT NULL DEFAULT 0,

    CONSTRAINT fk_equity_portfolio
        FOREIGN KEY (portfolio_id) REFERENCES portfolios (portfolio_id)
        ON DELETE CASCADE,

    -- One snapshot per portfolio per instant. Makes the writer idempotent on
    -- restart/replay: use ON CONFLICT DO UPDATE.
    CONSTRAINT uq_equity_portfolio_ts UNIQUE (portfolio_id, ts)
);

COMMENT ON TABLE  equity_curve IS 'Mark-to-market snapshots. The authoritative performance time series.';
COMMENT ON COLUMN equity_curve.total_equity IS 'cash + position_value. Must reconcile with portfolios.current_cash + open positions.';
COMMENT ON COLUMN equity_curve.drawdown_pct IS 'Fractional (0.10 = 10%), not percent points. Precomputed to keep dashboards cheap.';

CREATE INDEX IF NOT EXISTS ix_equity_portfolio_ts ON equity_curve (portfolio_id, ts DESC);


-- =============================================================================
-- 7. MARKET_DATA_CACHE
-- =============================================================================
CREATE TABLE IF NOT EXISTS market_data_cache (
    data_id    BIGSERIAL     PRIMARY KEY,
    symbol     VARCHAR(64)   NOT NULL,
    exchange   VARCHAR(16)   NOT NULL DEFAULT 'NSE',
    timeframe  VARCHAR(8)    NOT NULL,
    ts         TIMESTAMPTZ   NOT NULL,
    open       NUMERIC(20,8) NOT NULL,
    high       NUMERIC(20,8) NOT NULL,
    low        NUMERIC(20,8) NOT NULL,
    close      NUMERIC(20,8) NOT NULL,
    volume     NUMERIC(20,4) NOT NULL DEFAULT 0,
    bid        NUMERIC(20,8),
    ask        NUMERIC(20,8),
    source     VARCHAR(32)   NOT NULL DEFAULT 'mstock',
    ingested_at TIMESTAMPTZ  NOT NULL DEFAULT now(),

    CONSTRAINT ck_mdc_timeframe CHECK (timeframe IN ('1min','3min','5min','15min','30min','60min','1hour','day','week','month')),

    -- OHLC sanity, enforced at the storage layer so bad ticks can never be
    -- persisted even if the Step 11 validator is bypassed.
    CONSTRAINT ck_mdc_ohlc CHECK (
        high >= low
        AND high >= open  AND high >= close
        AND low  <= open  AND low  <= close
    ),
    CONSTRAINT ck_mdc_prices_pos CHECK (open > 0 AND high > 0 AND low > 0 AND close > 0),
    CONSTRAINT ck_mdc_volume     CHECK (volume >= 0),
    CONSTRAINT ck_mdc_spread     CHECK (bid IS NULL OR ask IS NULL OR bid <= ask),

    -- Natural key. Re-fetching the same bar must upsert, not duplicate.
    CONSTRAINT uq_mdc_bar UNIQUE (symbol, exchange, timeframe, ts)
);

COMMENT ON TABLE  market_data_cache IS 'Local OHLCV cache. Survives restarts so warm-up windows do not re-hit the broker API.';
COMMENT ON COLUMN market_data_cache.ts IS 'Bar OPEN time, aligned to the timeframe boundary. Never bar close time.';

CREATE INDEX IF NOT EXISTS ix_mdc_symbol_tf_ts ON market_data_cache (symbol, timeframe, ts DESC);
CREATE INDEX IF NOT EXISTS ix_mdc_ts           ON market_data_cache (ts DESC);


-- =============================================================================
-- 8. PERFORMANCE_METRICS
-- =============================================================================
CREATE TABLE IF NOT EXISTS performance_metrics (
    metric_id               BIGSERIAL     PRIMARY KEY,
    portfolio_id            UUID          NOT NULL,
    calculation_date        DATE          NOT NULL,
    total_trades            INTEGER       NOT NULL DEFAULT 0,
    winning_trades          INTEGER       NOT NULL DEFAULT 0,
    losing_trades           INTEGER       NOT NULL DEFAULT 0,
    win_rate                NUMERIC(12,6),
    avg_win                 NUMERIC(20,4),
    avg_loss                NUMERIC(20,4),
    largest_win             NUMERIC(20,4),
    largest_loss            NUMERIC(20,4),
    profit_factor           NUMERIC(12,6),
    expectancy              NUMERIC(20,4),
    sharpe_ratio            NUMERIC(12,6),
    sortino_ratio           NUMERIC(12,6),
    max_drawdown            NUMERIC(20,4),
    max_drawdown_percentage NUMERIC(12,6),
    total_return            NUMERIC(20,4),
    total_return_percentage NUMERIC(12,6),
    total_commission        NUMERIC(20,4) NOT NULL DEFAULT 0,
    total_slippage          NUMERIC(20,4) NOT NULL DEFAULT 0,
    calculated_at           TIMESTAMPTZ   NOT NULL DEFAULT now(),

    CONSTRAINT fk_perf_portfolio
        FOREIGN KEY (portfolio_id) REFERENCES portfolios (portfolio_id)
        ON DELETE CASCADE,

    CONSTRAINT ck_perf_counts CHECK (
        total_trades >= 0 AND winning_trades >= 0 AND losing_trades >= 0
        AND winning_trades + losing_trades <= total_trades
    ),
    CONSTRAINT ck_perf_win_rate CHECK (win_rate IS NULL OR (win_rate >= 0 AND win_rate <= 1)),

    -- Recalculating a day overwrites it rather than appending.
    CONSTRAINT uq_perf_portfolio_date UNIQUE (portfolio_id, calculation_date)
);

COMMENT ON TABLE  performance_metrics IS 'Daily rollup produced by the Step 17 calculator. Derived data — safe to rebuild.';
COMMENT ON COLUMN performance_metrics.win_rate IS 'Fractional 0..1.';
COMMENT ON COLUMN performance_metrics.profit_factor IS 'gross profit / gross loss. NULL when there are no losses (undefined).';

CREATE INDEX IF NOT EXISTS ix_perf_portfolio_date ON performance_metrics (portfolio_id, calculation_date DESC);


-- =============================================================================
-- 9. STRATEGY_SIGNALS
-- =============================================================================
CREATE TABLE IF NOT EXISTS strategy_signals (
    signal_id           BIGSERIAL     PRIMARY KEY,
    portfolio_id        UUID          NOT NULL,
    symbol              VARCHAR(64)   NOT NULL,
    strategy_name       VARCHAR(64),
    signal_type         VARCHAR(8)    NOT NULL,
    direction           VARCHAR(8)    NOT NULL,
    strength            NUMERIC(12,6),
    target_position     NUMERIC(12,6),
    bar_ts              TIMESTAMPTZ,
    generated_at        TIMESTAMPTZ   NOT NULL DEFAULT now(),
    indicators_snapshot JSONB,
    executed            BOOLEAN       NOT NULL DEFAULT FALSE,
    order_id            UUID,
    skip_reason         TEXT,

    CONSTRAINT fk_signals_portfolio
        FOREIGN KEY (portfolio_id) REFERENCES portfolios (portfolio_id)
        ON DELETE CASCADE,
    CONSTRAINT fk_signals_order
        FOREIGN KEY (order_id) REFERENCES orders (order_id)
        ON DELETE SET NULL,

    CONSTRAINT ck_signals_type      CHECK (signal_type IN ('entry','exit')),
    CONSTRAINT ck_signals_direction CHECK (direction IN ('long','short','flat')),
    CONSTRAINT ck_signals_strength  CHECK (strength IS NULL OR (strength >= 0 AND strength <= 1)),
    CONSTRAINT ck_signals_target    CHECK (target_position IS NULL OR (target_position >= -1 AND target_position <= 1))
);

COMMENT ON TABLE  strategy_signals IS 'Audit log of every signal, executed or not. Key input to look-ahead-bias detection (Step 22).';
COMMENT ON COLUMN strategy_signals.bar_ts IS 'Open time of the COMPLETED bar that produced this signal. Must be < generated_at — this is the no-lookahead guarantee.';
COMMENT ON COLUMN strategy_signals.target_position IS 'Clipped to [-1, 1], matching the backtest engine signal contract.';
COMMENT ON COLUMN strategy_signals.skip_reason IS 'Why executed = false (risk rejection, zero size, market closed, ...).';

CREATE INDEX IF NOT EXISTS ix_signals_portfolio_gen ON strategy_signals (portfolio_id, generated_at DESC);
CREATE INDEX IF NOT EXISTS ix_signals_symbol        ON strategy_signals (symbol, generated_at DESC);
CREATE INDEX IF NOT EXISTS ix_signals_unexecuted    ON strategy_signals (portfolio_id) WHERE executed = FALSE;
CREATE INDEX IF NOT EXISTS ix_signals_indicators    ON strategy_signals USING GIN (indicators_snapshot);


-- =============================================================================
-- 10. SYSTEM_LOGS
-- =============================================================================
CREATE TABLE IF NOT EXISTS system_logs (
    log_id       BIGSERIAL    PRIMARY KEY,
    portfolio_id UUID,
    ts           TIMESTAMPTZ  NOT NULL DEFAULT now(),
    log_level    VARCHAR(16)  NOT NULL,
    component    VARCHAR(64)  NOT NULL,
    message      TEXT         NOT NULL,
    stack_trace  TEXT,
    context      JSONB,

    -- Logs outlive the portfolio they describe: SET NULL, never CASCADE.
    CONSTRAINT fk_logs_portfolio
        FOREIGN KEY (portfolio_id) REFERENCES portfolios (portfolio_id)
        ON DELETE SET NULL,

    CONSTRAINT ck_logs_level CHECK (log_level IN ('debug','info','warning','error','critical'))
);

COMMENT ON TABLE  system_logs IS 'Structured application log. Mirrors stdout logging for post-mortem queries.';
COMMENT ON COLUMN system_logs.component IS 'Emitting subsystem: order_executor, risk_manager, data_handler, ...';

CREATE INDEX IF NOT EXISTS ix_logs_ts             ON system_logs (ts DESC);
CREATE INDEX IF NOT EXISTS ix_logs_portfolio_ts   ON system_logs (portfolio_id, ts DESC);
CREATE INDEX IF NOT EXISTS ix_logs_level_ts       ON system_logs (log_level, ts DESC);
CREATE INDEX IF NOT EXISTS ix_logs_component      ON system_logs (component, ts DESC);
-- Errors are the common query; keep a small partial index for them.
CREATE INDEX IF NOT EXISTS ix_logs_errors
    ON system_logs (ts DESC)
    WHERE log_level IN ('error','critical');


-- =============================================================================
-- CONVENIENCE VIEWS
-- =============================================================================

-- Live snapshot of open exposure with mark-to-market values.
CREATE OR REPLACE VIEW v_open_positions AS
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

-- Most recent equity point per portfolio — what the dashboard header shows.
CREATE OR REPLACE VIEW v_portfolio_summary AS
SELECT
    pf.portfolio_id,
    pf.name,
    pf.status,
    pf.initial_capital,
    pf.current_cash,
    e.total_equity,
    e.cumulative_pnl,
    e.drawdown_pct,
    e.ts AS last_marked_at,
    (SELECT COUNT(*) FROM positions p
      WHERE p.portfolio_id = pf.portfolio_id AND p.status = 'open') AS open_positions,
    (SELECT COUNT(*) FROM trades t
      WHERE t.portfolio_id = pf.portfolio_id)                        AS total_trades
FROM portfolios pf
LEFT JOIN LATERAL (
    SELECT total_equity, cumulative_pnl, drawdown_pct, ts
    FROM equity_curve ec
    WHERE ec.portfolio_id = pf.portfolio_id
    ORDER BY ec.ts DESC
    LIMIT 1
) e ON TRUE;


-- =============================================================================
-- Record this migration
-- =============================================================================
INSERT INTO schema_migrations (version, description)
VALUES ('001', 'Initial forward testing schema: 10 tables, indexes, constraints, 2 views')
ON CONFLICT (version) DO NOTHING;

COMMIT;

-- =============================================================================
-- END 001_initial_schema.sql
-- =============================================================================
