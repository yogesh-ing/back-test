-- =============================================================================
-- Forward Testing Simulator — Cost & Risk Settings Phase 2 (segments + kill switch)
-- Migration : 007_segments_kill_switch
-- Engine    : PostgreSQL 13+
-- =============================================================================
--
-- WHAT THIS DOES
--   Adds the Phase-2 tables of the certified Cost & Risk Settings panel
--   (docs/drafts/BROKER-CAPITAL-SETTINGS-PANEL.md §0):
--
--     segments        capital + mandate + broker + mode + risk_limits per segment
--     segment_audit   append-only who/when/old→new trail per field
--
--   Plus the GLOBAL LIVE KILL-SWITCH (v2 §0 #4): a sentinel row in
--   broker_profiles (profile_id = '__live_kill_switch__', commission_model
--   {"enabled": false}) — default OFF, overrides every segment mode=live.
--   The sentinel is INSERTed here so the switch exists from migration time;
--   the store also self-heals it if missing.
--
-- IDEMPOTENCY
--   CREATE TABLE/INDEX IF NOT EXISTS; INSERT ... ON CONFLICT DO NOTHING.
--
-- ROLLBACK
--   DROP TABLE IF EXISTS segment_audit;
--   DROP TABLE IF EXISTS segments;
--   DELETE FROM broker_profiles WHERE profile_id = '__live_kill_switch__';
--   DELETE FROM schema_migrations WHERE version = '007';
-- =============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS segments (
    segment_id        VARCHAR(50)  PRIMARY KEY,
    segment_name      VARCHAR(100) NOT NULL,
    mode              VARCHAR(10)  NOT NULL DEFAULT 'paper',
    allocated_capital NUMERIC(14, 2),
    broker_profile_id VARCHAR(50),
    risk_limits       JSONB        NOT NULL DEFAULT '{}'::jsonb,
    created_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS segment_audit (
    audit_id      BIGSERIAL   PRIMARY KEY,
    segment_id    VARCHAR(50) NOT NULL,
    field_changed VARCHAR(100) NOT NULL,
    old_value     TEXT,
    new_value     TEXT,
    changed_by    VARCHAR(50)  DEFAULT 'admin',
    changed_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_segment_audit_segment
    ON segment_audit (segment_id, changed_at);

-- The global live kill-switch sentinel (default OFF — fail closed).
INSERT INTO broker_profiles (profile_id, profile_name, is_preset, commission_model, statutory_rates)
VALUES ('__live_kill_switch__', 'Global live kill-switch', FALSE,
        '{"enabled": false}'::jsonb, '{}'::jsonb)
ON CONFLICT (profile_id) DO NOTHING;

INSERT INTO schema_migrations (version, description)
VALUES ('007', 'cost & risk settings phase 2: segments + global live kill-switch')
ON CONFLICT (version) DO NOTHING;

COMMIT;

-- =============================================================================
-- END 007_segments_kill_switch.sql
-- =============================================================================
