-- =============================================================================
-- Forward Testing Simulator — Portfolio Intelligence & Alerts (SQLite dev variant)
-- Migration : 005_portfolio_intelligence
-- Engine    : SQLite 3.35+
-- =============================================================================
--
-- LOCAL DEVELOPMENT mirror of 005_portfolio_intelligence.sql. Keep both files
-- in sync when the change evolves.
--
-- DIFFERENCES FROM THE POSTGRES FILE (and why)
--   UUID         -> TEXT (application generates uuid4 ids).
--   TIMESTAMPTZ  -> TEXT storing ISO-8601 UTC.
--   JSONB        -> TEXT. Use json_extract() for querying (JSON1 extension).
--   The partial index on active alerts is a plain composite index here.
-- =============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS alerts (
    alert_id            TEXT PRIMARY KEY,
    alert_type          TEXT NOT NULL,
    alert_key           TEXT NOT NULL,
    severity            TEXT NOT NULL
        CHECK (severity IN ('critical','warning','info')),
    message             TEXT NOT NULL,
    data                TEXT NOT NULL DEFAULT '{}',
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT,
    resolved_at         TEXT,
    dismissed_at        TEXT,
    dismissed_by        TEXT,
    reviewed_at         TEXT,
    notified_strategies TEXT
);

CREATE INDEX IF NOT EXISTS idx_alerts_type    ON alerts (alert_type);
CREATE INDEX IF NOT EXISTS idx_alerts_created ON alerts (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_alerts_active  ON alerts (alert_type, resolved_at);

CREATE TABLE IF NOT EXISTS portfolio_greeks_history (
    snapshot_id                 TEXT PRIMARY KEY,
    timestamp                   TEXT NOT NULL,
    net_delta                   NUMERIC,
    net_gamma                   NUMERIC,
    net_vega                    NUMERIC,
    net_theta                   NUMERIC,
    greeks_by_strategy          TEXT,
    concentration_by_underlying TEXT,
    concentration_by_strike     TEXT
);

CREATE INDEX IF NOT EXISTS idx_greeks_time ON portfolio_greeks_history (timestamp DESC);

CREATE TABLE IF NOT EXISTS market_regime_history (
    regime_id       TEXT PRIMARY KEY,
    timestamp       TEXT NOT NULL,
    regime          TEXT NOT NULL,
    vix             NUMERIC,
    realized_vol    NUMERIC,
    source          TEXT,
    previous_regime TEXT,
    regime_changed  BOOLEAN NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_regime_time ON market_regime_history (timestamp DESC);

INSERT OR IGNORE INTO schema_migrations (version, description)
VALUES ('005', 'portfolio intelligence: alerts, greeks + regime history');

COMMIT;

-- =============================================================================
-- END 005_portfolio_intelligence.sqlite.sql
-- =============================================================================
