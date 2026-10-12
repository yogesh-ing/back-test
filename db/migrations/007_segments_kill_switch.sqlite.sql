-- =============================================================================
-- Forward Testing Simulator — Cost & Risk Settings Phase 2 (SQLite dev variant)
-- Migration : 007_segments_kill_switch
-- Engine    : SQLite 3.35+
-- =============================================================================
-- LOCAL DEVELOPMENT mirror of 007_segments_kill_switch.sql. Keep both in sync.
--   BIGSERIAL -> rowid PK, TIMESTAMPTZ -> TEXT (ISO-8601 UTC),
--   JSONB -> TEXT (JSON1 for queries), NUMERIC -> NUMERIC.
-- =============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS segments (
    segment_id        TEXT PRIMARY KEY,
    segment_name      TEXT NOT NULL,
    mode              TEXT NOT NULL DEFAULT 'paper',
    allocated_capital NUMERIC(14, 2),
    broker_profile_id TEXT,
    risk_limits       TEXT NOT NULL DEFAULT '{}',
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at        TEXT
);

CREATE TABLE IF NOT EXISTS segment_audit (
    audit_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    segment_id    TEXT NOT NULL,
    field_changed TEXT NOT NULL,
    old_value     TEXT,
    new_value     TEXT,
    changed_by    TEXT DEFAULT 'admin',
    changed_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_segment_audit_segment
    ON segment_audit (segment_id, changed_at);

-- The global live kill-switch sentinel (default OFF — fail closed).
INSERT OR IGNORE INTO broker_profiles (profile_id, profile_name, is_preset, commission_model, statutory_rates)
VALUES ('__live_kill_switch__', 'Global live kill-switch', 0, '{"enabled": false}', '{}');

INSERT OR IGNORE INTO schema_migrations (version, description)
VALUES ('007', 'cost & risk settings phase 2: segments + global live kill-switch');

COMMIT;

-- =============================================================================
-- END 007_segments_kill_switch.sqlite.sql
-- =============================================================================
