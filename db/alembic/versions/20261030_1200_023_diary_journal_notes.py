"""PnL Diary operator journal: ``diary_notes``

Slice 5 of ``docs/PNL-DIARY-AND-EDGE-SUITE-PLAN.md``. Persists book-segregated
(``mode IN ('paper','live')``) IST session notes, regime tags, and optional
strategy / spawned-instance annotations across restarts on both PostgreSQL and
SQLite.

Hand-applied equivalents: ``db/migrations/023_diary_journal_notes.sql``
(PostgreSQL) and ``023_diary_journal_notes.sqlite.sql``.

Revision ID: 023
Revises: 022
Create Date: 2026-10-30
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "023"
down_revision: Union[str, None] = "022"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

UUID_COL = sa.String(36).with_variant(postgresql.UUID(as_uuid=False), "postgresql")
JSONB_COL = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "diary_notes",
        sa.Column("note_id", UUID_COL, primary_key=True, nullable=False),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("session_date", sa.Date(), nullable=False),
        sa.Column("strategy_name", sa.String(64), nullable=True),
        sa.Column("portfolio_id", UUID_COL, nullable=True),
        sa.Column("instance_label", sa.String(128), nullable=True),
        sa.Column(
            "regime_tags",
            JSONB_COL,
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
        sa.Column(
            "note_text",
            sa.Text(),
            nullable=False,
            server_default=sa.text("''"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint("mode IN ('paper','live')", name="ck_diary_notes_mode"),
    )
    op.create_index(
        "ix_diary_notes_mode_session",
        "diary_notes",
        ["mode", "session_date"],
    )
    op.create_index(
        "ix_diary_notes_strategy",
        "diary_notes",
        ["mode", "strategy_name"],
    )


def downgrade() -> None:
    op.drop_index("ix_diary_notes_strategy", table_name="diary_notes")
    op.drop_index("ix_diary_notes_mode_session", table_name="diary_notes")
    op.drop_table("diary_notes")
