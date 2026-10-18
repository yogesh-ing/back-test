-- =============================================================================
-- Forward Testing Simulator — Option intent journal (SQLite dev variant)
-- Migration : 020_option_intent_journal
-- Engine    : SQLite 3.35+
-- =============================================================================
--
-- LOCAL DEVELOPMENT mirror of 020_option_intent_journal.sql. Keep both files
-- in sync when the change evolves.
--
-- DIFFERENCES FROM THE POSTGRES FILE (and why)
--   BIGSERIAL  -> INTEGER PRIMARY KEY. SQLite only autoincrements a column
--                 declared exactly INTEGER PRIMARY KEY (the rowid alias);
--                 this is the same variant the ORM applies (BigIntPK).
--   JSONB      -> TEXT storing JSON (json_extract() for querying).
--   TIMESTAMPTZ-> TEXT storing ISO-8601 UTC, default CURRENT_TIMESTAMP.
--   The CHECK constraint and indexes are identical in meaning.
--
-- The dev/test path usually gets this table from ``create_all`` (see
-- ``OptionIntentEvent.ensure_schema``); this file is the hand-applied route.
-- =============================================================================

PRAGMA foreign_keys = ON;

BEGIN;

CREATE TABLE IF NOT EXISTS option_intent_events (
    event_id        INTEGER PRIMARY KEY,
    intent_id       TEXT    NOT NULL,
    kind            TEXT    NOT NULL
        CHECK (kind IN ('decided','submitted','filled','rejected','uncertain',
                        'reconciled','halted','executed')),

    dedupe_key      TEXT,
    leg_index       INTEGER,
    structure_id    TEXT,
    strategy_name   TEXT,
    structure_type  TEXT,
    underlying      TEXT,

    broker_order_id TEXT,
    client_order_id TEXT,

    detail          TEXT    NOT NULL DEFAULT '{}',
    recorded_at     TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX IF NOT EXISTS ix_opt_intent_events_dedupe
    ON option_intent_events (dedupe_key);
CREATE INDEX IF NOT EXISTS ix_opt_intent_events_intent
    ON option_intent_events (intent_id, event_id);
CREATE INDEX IF NOT EXISTS ix_opt_intent_events_recorded
    ON option_intent_events (recorded_at DESC);

INSERT OR IGNORE INTO schema_migrations (version, description)
VALUES ('020', 'option_intent_events: append-only intent journal (Options V1 #18)');

COMMIT;

-- =============================================================================
-- END 020_option_intent_journal.sqlite.sql
-- =============================================================================
