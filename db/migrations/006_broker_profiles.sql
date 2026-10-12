-- =============================================================================
-- Forward Testing Simulator — Cost & Risk Settings (broker profiles)
-- Migration : 006_broker_profiles
-- Engine    : PostgreSQL 13+
-- =============================================================================
--
-- WHAT THIS DOES
--   Adds the two tables behind the certified Cost & Risk Settings panel
--   (docs/drafts/BROKER-CAPITAL-SETTINGS-PANEL.md §0):
--
--     broker_profiles        editable broker cost models; presets seeded from
--                            config/brokers.yaml with is_preset = TRUE
--     broker_profile_audit   append-only who/when/old→new trail per field
--
--   JSONB payload shapes mirror backtest.simulator.fees.BrokerProfile kwargs
--   (commission_model, statutory_rates) so the fee engine needs no adapter.
--
-- IDEMPOTENCY
--   CREATE TABLE/INDEX IF NOT EXISTS.
--
-- ROLLBACK
--   DROP TABLE IF EXISTS broker_profile_audit;
--   DROP TABLE IF EXISTS broker_profiles;
--   DELETE FROM schema_migrations WHERE version = '006';
-- =============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS broker_profiles (
    profile_id         VARCHAR(50)  PRIMARY KEY,
    profile_name       VARCHAR(100) NOT NULL,
    is_preset          BOOLEAN      NOT NULL DEFAULT FALSE,
    currency           VARCHAR(3)   NOT NULL DEFAULT 'INR',
    default_segment    VARCHAR(30)  NOT NULL DEFAULT 'equity_delivery',
    commission_model   JSONB        NOT NULL DEFAULT '{}'::jsonb,
    statutory_rates    JSONB        NOT NULL DEFAULT '{}'::jsonb,
    minimum_commission NUMERIC(12, 2),
    validated_on       DATE,
    contract_note_ref  VARCHAR(100),
    created_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at         TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS broker_profile_audit (
    audit_id      BIGSERIAL   PRIMARY KEY,
    profile_id    VARCHAR(50) NOT NULL,
    field_changed VARCHAR(100) NOT NULL,
    old_value     TEXT,
    new_value     TEXT,
    changed_by    VARCHAR(50)  DEFAULT 'admin',
    changed_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_broker_audit_profile
    ON broker_profile_audit (profile_id, changed_at);

INSERT INTO schema_migrations (version, description)
VALUES ('006', 'cost & risk settings: broker profiles + audit trail')
ON CONFLICT (version) DO NOTHING;

COMMIT;

-- =============================================================================
-- END 006_broker_profiles.sql
-- =============================================================================
