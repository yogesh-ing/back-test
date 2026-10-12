-- =============================================================================
-- Forward Testing Simulator — Portfolio Intelligence & Alerts
-- Migration : 005_portfolio_intelligence
-- Engine    : PostgreSQL 13+
-- =============================================================================
--
-- WHAT THIS DOES
--   Adds the three audit/history tables behind the Portfolio Intelligence
--   layer (docs/PORTFOLIO-INTELLIGENCE.md):
--
--     alerts                    alert audit trail, one row per alert, updated
--                               through its lifecycle (resolved / dismissed /
--                               reviewed) + which strategies were notified
--     portfolio_greeks_history  periodic aggregate Greeks snapshots
--                               (net_delta share-equivalents, net_gamma Δ per
--                               1% move, net_vega ₹/IV pt, net_theta ₹/day)
--     market_regime_history     VIX-band regime samples + transitions
--
--   Rows are written fail-soft by IntelligencePersister — a missing table or
--   unreachable DB degrades to in-memory history only; it never blocks alerts.
--
-- IDEMPOTENCY
--   CREATE TABLE/INDEX IF NOT EXISTS + ON CONFLICT DO NOTHING.
--
-- ROLLBACK
--   DROP TABLE IF EXISTS market_regime_history;
--   DROP TABLE IF EXISTS portfolio_greeks_history;
--   DROP TABLE IF EXISTS alerts;
--   DELETE FROM schema_migrations WHERE version = '005';
-- =============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS alerts (
    alert_id            UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    alert_type          VARCHAR(50)  NOT NULL,
    alert_key           VARCHAR(200) NOT NULL,
    severity            VARCHAR(20)  NOT NULL
        CONSTRAINT ck_alerts_severity CHECK (severity IN ('critical','warning','info')),
    message             TEXT         NOT NULL,
    data                JSONB        NOT NULL DEFAULT '{}'::jsonb,

    created_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ,
    resolved_at         TIMESTAMPTZ,
    dismissed_at        TIMESTAMPTZ,
    dismissed_by        VARCHAR(100),
    reviewed_at         TIMESTAMPTZ,

    -- Which strategies were notified (and whether their callback succeeded)
    notified_strategies JSONB
);

CREATE INDEX IF NOT EXISTS idx_alerts_type    ON alerts (alert_type);
CREATE INDEX IF NOT EXISTS idx_alerts_created ON alerts (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_alerts_active  ON alerts (alert_type, resolved_at)
    WHERE resolved_at IS NULL;

CREATE TABLE IF NOT EXISTS portfolio_greeks_history (
    snapshot_id                 UUID          PRIMARY KEY DEFAULT gen_random_uuid(),
    timestamp                   TIMESTAMPTZ   NOT NULL,

    net_delta                   NUMERIC(16,2),
    net_gamma                   NUMERIC(16,2),
    net_vega                    NUMERIC(16,2),
    net_theta                   NUMERIC(16,2),

    greeks_by_strategy          JSONB,
    concentration_by_underlying JSONB,
    concentration_by_strike     JSONB
);

CREATE INDEX IF NOT EXISTS idx_greeks_time ON portfolio_greeks_history (timestamp DESC);

CREATE TABLE IF NOT EXISTS market_regime_history (
    regime_id       UUID          PRIMARY KEY DEFAULT gen_random_uuid(),
    timestamp       TIMESTAMPTZ   NOT NULL,

    regime          VARCHAR(20)   NOT NULL,  -- low_vol | moderate_vol | high_vol | unknown
    vix             NUMERIC(8,2),
    realized_vol    NUMERIC(8,2),
    source          VARCHAR(64),             -- manual | feed:INDIAVIX | realized_vol_proxy:NIFTY

    previous_regime VARCHAR(20),
    regime_changed  BOOLEAN       NOT NULL DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS idx_regime_time ON market_regime_history (timestamp DESC);

INSERT INTO schema_migrations (version, description)
VALUES ('005', 'portfolio intelligence: alerts, greeks + regime history')
ON CONFLICT (version) DO NOTHING;

COMMIT;

-- =============================================================================
-- END 005_portfolio_intelligence.sql
-- =============================================================================
