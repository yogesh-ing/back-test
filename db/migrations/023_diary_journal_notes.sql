-- Migration 023: PnL Diary Operator Journal (PostgreSQL)
-- Strictly segregates Paper vs Live journal entries by IST session_date.

BEGIN;

CREATE TABLE IF NOT EXISTS diary_notes (
    note_id        UUID PRIMARY KEY,
    mode           VARCHAR(16) NOT NULL,
    session_date   DATE NOT NULL,
    strategy_name  VARCHAR(64),
    portfolio_id   UUID,
    instance_label VARCHAR(128),
    regime_tags    JSONB NOT NULL DEFAULT '[]'::jsonb,
    note_text      TEXT NOT NULL DEFAULT '',
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT ck_diary_notes_mode CHECK (mode IN ('paper', 'live'))
);

CREATE INDEX IF NOT EXISTS ix_diary_notes_mode_session
    ON diary_notes (mode, session_date);

CREATE INDEX IF NOT EXISTS ix_diary_notes_strategy
    ON diary_notes (mode, strategy_name);

INSERT INTO schema_migrations (version, description)
VALUES ('023', 'Add diary_notes table for book-segregated PnL Diary operator journal')
ON CONFLICT (version) DO NOTHING;

COMMIT;
