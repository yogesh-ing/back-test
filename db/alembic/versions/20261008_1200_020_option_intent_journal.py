"""option intent journal: append-only ``option_intent_events``

Options V1 PRD decision 8 / Phase 1 item 6. One row per trade decision and
its outcome, keyed by a deterministic ``intent_id`` hashed from the decision
itself — the unique ``dedupe_key`` index is the idempotency guard that lets
the execution seam refuse a second ``place_order`` for a leg it already
submitted, instead of blindly retrying an uncertain one.

No foreign keys and no update path: the journal outlives structure rows, so
portfolio state stays derivable by replay after the book is compacted.

Hand-applied equivalents: ``db/migrations/020_option_intent_journal.sql``
(PostgreSQL) and ``020_option_intent_journal.sqlite.sql`` — use ONE path, not
both (see revision 001's header).

Revision ID: 020
Revises: 019
Create Date: 2026-10-08
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "020"
down_revision: Union[str, None] = "019"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: Mirrors models.JSONVariant (JSONB on PostgreSQL, JSON elsewhere).
JSONB = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
BIGPK = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "option_intent_events",
        sa.Column("event_id", BIGPK, primary_key=True, nullable=False),
        sa.Column("intent_id", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("dedupe_key", sa.String(160), nullable=True),
        sa.Column("leg_index", sa.Integer(), nullable=True),
        sa.Column("structure_id", sa.String(36), nullable=True),
        sa.Column("strategy_name", sa.String(64), nullable=True),
        sa.Column("structure_type", sa.String(32), nullable=True),
        sa.Column("underlying", sa.String(32), nullable=True),
        sa.Column("broker_order_id", sa.String(64), nullable=True),
        sa.Column("client_order_id", sa.String(64), nullable=True),
        sa.Column("detail", JSONB, nullable=False, server_default=sa.text("'{}'")),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "kind IN ('decided','submitted','filled','rejected','uncertain',"
            "'reconciled','halted','executed')",
            name="ck_opt_intent_kind",
        ),
    )
    op.create_index(
        "ix_opt_intent_events_dedupe", "option_intent_events", ["dedupe_key"], unique=True
    )
    op.create_index(
        "ix_opt_intent_events_intent", "option_intent_events", ["intent_id", "event_id"]
    )
    op.create_index(
        "ix_opt_intent_events_recorded",
        "option_intent_events",
        [sa.literal_column("recorded_at DESC")],
    )


def downgrade() -> None:
    for name in (
        "ix_opt_intent_events_recorded",
        "ix_opt_intent_events_intent",
        "ix_opt_intent_events_dedupe",
    ):
        op.drop_index(name, table_name="option_intent_events")
    op.drop_table("option_intent_events")
