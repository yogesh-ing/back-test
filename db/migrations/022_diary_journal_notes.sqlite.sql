-- Migration 022: PnL Diary Operator Journal (SQLite)

CREATE TABLE IF NOT EXISTS diary_notes (
    note_id        VARCHAR(36) PRIMARY KEY NOT NULL,
    mode           VARCHAR(16) NOT NULL,
    session_date   DATE NOT NULL,
    strategy_name  VARCHAR(64),
    portfolio_id   VARCHAR(36),
    instance_label VARCHAR(128),
    regime_tags    TEXT NOT NULL DEFAULT '[]',
    note_text      TEXT NOT NULL DEFAULT '',
    created_at     TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at     TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT ck_diary_notes_mode CHECK (mode IN ('paper', 'live'))
);

CREATE INDEX IF NOT EXISTS ix_diary_notes_mode_session
    ON diary_notes (mode, session_date);

CREATE INDEX IF NOT EXISTS ix_diary_notes_strategy
    ON diary_notes (mode, strategy_name);

INSERT OR IGNORE INTO schema_migrations (version, description)
VALUES ('022', 'Add diary_notes table for book-segregated PnL Diary operator journal');
