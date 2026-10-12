-- =============================================================================
-- Forward Testing Simulator — Add dhan to the portfolios source CHECK (SQLite dev variant)
-- Migration : 016_add_dhan_source
-- Engine    : SQLite 3.35+
-- =============================================================================
--
-- LOCAL DEVELOPMENT mirror of 016_add_dhan_source.sql. Keep both files
-- in sync when the change evolves.
--
-- DIFFERENCES FROM THE POSTGRES FILE (and why)
--   CHECK constraint -> full table rebuild. SQLite cannot drop or alter a
--                      CHECK on an existing table (002 added it as a
--                      column-level CHECK inside ADD COLUMN); the standard
--                      12-step rebuild (create new, copy, drop, rename,
--                      recreate index/trigger) is the supported path, the
--                      same technique 003 used for market_data_cache.
--   The rebuild carries the columns 001 + 002 + 008 gave this table
--   (mode, source, segment, execution_broker) and widens the source CHECK
--   to admit 'dhan'.
--   Idempotency -> this file is intentionally NOT re-runnable (the CREATE
--   TABLE step fails on a second run), matching 002's convention:
--   incremental migrations are applied exactly once and tracked in
--   schema_migrations.
--
--   Child tables (positions, orders, trades, equity_points) reference
--   portfolios by name; the swap keeps the name, so their FK clauses stay
--   valid. foreign_keys is disabled for the swap per the SQLite documented
--   procedure and re-enabled after the foreign_key_check.
--   The two views from 001 (v_open_positions, v_portfolio_summary) JOIN
--   portfolios, so SQLite refuses the DROP TABLE while they exist — they
--   are dropped first and recreated verbatim after the rename.
-- =============================================================================

PRAGMA foreign_keys = OFF;

BEGIN;

CREATE TABLE portfolios_new (
    portfolio_id     TEXT    PRIMARY KEY,
    name             TEXT    NOT NULL,
    initial_capital  NUMERIC NOT NULL,
    current_cash     NUMERIC NOT NULL,
    base_currency    TEXT    NOT NULL DEFAULT 'INR',
    status           TEXT    NOT NULL DEFAULT 'active',
    created_at       TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at       TEXT    NOT NULL DEFAULT (datetime('now')),
    mode             TEXT    NOT NULL DEFAULT 'paper'
        CHECK (mode IN ('paper','live')),
    source           TEXT    NOT NULL DEFAULT 'synthetic'
        CHECK (source IN ('synthetic','replay','mstock','dhan')),
    segment          TEXT    NOT NULL DEFAULT '',
    execution_broker TEXT    NOT NULL DEFAULT '',

    CONSTRAINT uq_portfolios_name        UNIQUE (name),
    CONSTRAINT ck_portfolios_status      CHECK (status IN ('active','paused','stopped')),
    CONSTRAINT ck_portfolios_capital_pos CHECK (initial_capital > 0)
);

-- Copy every row verbatim: all existing source values are valid under the
-- widened CHECK, so no data transformation is needed.
INSERT INTO portfolios_new
    (portfolio_id, name, initial_capital, current_cash, base_currency, status,
     created_at, updated_at, mode, source, segment, execution_broker)
SELECT
    portfolio_id, name, initial_capital, current_cash, base_currency, status,
    created_at, updated_at, mode, source, segment, execution_broker
  FROM portfolios;

-- 001's views JOIN portfolios — drop them first (SQLite errors on
-- DROP TABLE while a view references it), recreate verbatim below.
DROP VIEW IF EXISTS v_open_positions;
DROP VIEW IF EXISTS v_portfolio_summary;

DROP TABLE portfolios;

ALTER TABLE portfolios_new RENAME TO portfolios;

-- Index and trigger belonged to the dropped table — recreate them.
CREATE INDEX IF NOT EXISTS ix_portfolios_status ON portfolios (status);

CREATE TRIGGER IF NOT EXISTS trg_portfolios_updated_at
AFTER UPDATE ON portfolios
FOR EACH ROW
BEGIN
    UPDATE portfolios SET updated_at = datetime('now')
    WHERE portfolio_id = NEW.portfolio_id;
END;

-- Recreate 001's views verbatim (dropped above for the table swap).
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

PRAGMA foreign_key_check;

-- =============================================================================
-- Record this migration
-- =============================================================================
INSERT OR IGNORE INTO schema_migrations (version, description)
VALUES ('016', 'portfolios: widen source CHECK to admit dhan (multi-broker data unlock)');

COMMIT;

PRAGMA foreign_keys = ON;

-- =============================================================================
-- END 016_add_dhan_source.sqlite.sql
-- =============================================================================
