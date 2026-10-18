-- =============================================================================
-- Forward Testing Simulator — Option intent journal (Options V1 PRD decision 8)
-- Migration : 020_option_intent_journal
-- Engine    : PostgreSQL 13+
-- =============================================================================
--
-- WHAT THIS DOES
--   Creates ``option_intent_events``, the append-only record of every option
--   trade decision and its outcome:
--
--     intent_id     deterministic id hashed from the decision itself, so the
--                   same decision replayed after a restart hashes identically
--     kind          decided | submitted | filled | rejected | uncertain
--                   | reconciled | halted | executed
--     dedupe_key    idempotency key (intent + kind + leg); the UNIQUE index is
--                   what makes a second submit of one leg unrecordable
--
--   There are no foreign keys and no update path: the journal outlives the
--   structure rows so replay stays possible after the book is compacted.
--
-- IDEMPOTENCY
--   CREATE TABLE / INDEX IF NOT EXISTS + ON CONFLICT DO NOTHING, same
--   discipline as 004/018.
--
-- ALEMBIC EQUIVALENT
--   db/alembic/versions/20261008_1200_020_option_intent_journal.py — use ONE
--   path, not both. SQLite dev mirror: 020_option_intent_journal.sqlite.sql.
--
-- ROLLBACK
--   DROP TABLE option_intent_events;
--   DELETE FROM schema_migrations WHERE version = '020';
-- =============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS option_intent_events (
    event_id       BIGSERIAL    PRIMARY KEY,
    intent_id      VARCHAR(64)  NOT NULL,
    kind           VARCHAR(16)  NOT NULL,

    dedupe_key     VARCHAR(160),
    leg_index      INTEGER,
    structure_id   VARCHAR(36),
    strategy_name  VARCHAR(64),
    structure_type VARCHAR(32),
    underlying     VARCHAR(32),

    broker_order_id VARCHAR(64),
    client_order_id VARCHAR(64),

    detail         JSONB        NOT NULL DEFAULT '{}'::jsonb,
    recorded_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),

    CONSTRAINT ck_opt_intent_kind CHECK (
        kind IN ('decided','submitted','filled','rejected','uncertain',
                 'reconciled','halted','executed')
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS ix_opt_intent_events_dedupe
    ON option_intent_events (dedupe_key);
CREATE INDEX IF NOT EXISTS ix_opt_intent_events_intent
    ON option_intent_events (intent_id, event_id);
CREATE INDEX IF NOT EXISTS ix_opt_intent_events_recorded
    ON option_intent_events (recorded_at DESC);

-- =============================================================================
-- Record this migration
-- =============================================================================
INSERT INTO schema_migrations (version, description)
VALUES ('020', 'option_intent_events: append-only intent journal (Options V1 #18)')
ON CONFLICT (version) DO NOTHING;

COMMIT;

-- =============================================================================
-- END 020_option_intent_journal.sql
-- =============================================================================
