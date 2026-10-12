-- =============================================================================
-- Forward Testing Simulator — Cost & Risk Settings (SQLite dev variant)
-- Migration : 006_broker_profiles
-- Engine    : SQLite 3.35+
-- =============================================================================
-- LOCAL DEVELOPMENT mirror of 006_broker_profiles.sql. Keep both in sync.
--   UUID/BIGSERIAL -> rowid PK, TIMESTAMPTZ -> TEXT (ISO-8601 UTC),
--   JSONB -> TEXT (JSON1 for queries), NUMERIC -> NUMERIC.
-- =============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS broker_profiles (
    profile_id         TEXT PRIMARY KEY,
    profile_name       TEXT NOT NULL,
    is_preset          INTEGER NOT NULL DEFAULT 0,
    currency           TEXT NOT NULL DEFAULT 'INR',
    default_segment    TEXT NOT NULL DEFAULT 'equity_delivery',
    commission_model   TEXT NOT NULL DEFAULT '{}',
    statutory_rates    TEXT NOT NULL DEFAULT '{}',
    minimum_commission NUMERIC(12, 2),
    validated_on       TEXT,
    contract_note_ref  TEXT,
    created_at         TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at         TEXT
);

CREATE TABLE IF NOT EXISTS broker_profile_audit (
    audit_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id    TEXT NOT NULL,
    field_changed TEXT NOT NULL,
    old_value     TEXT,
    new_value     TEXT,
    changed_by    TEXT DEFAULT 'admin',
    changed_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_broker_audit_profile
    ON broker_profile_audit (profile_id, changed_at);

INSERT OR IGNORE INTO schema_migrations (version, description)
VALUES ('006', 'cost & risk settings: broker profiles + audit trail');

COMMIT;

-- =============================================================================
-- END 006_broker_profiles.sqlite.sql
-- =============================================================================
