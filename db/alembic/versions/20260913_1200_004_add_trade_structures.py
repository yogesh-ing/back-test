"""option trade structures table (Gap-Analysis G4.2)

The options paper book lived in process memory only, so a server restart
lost every open structure. This revision adds ``trade_structures`` — one
row per executed structure with its legs serialised to JSON — so the web
app can rehydrate open positions on startup.

Status lifecycle: ``open`` -> ``closed`` (manual square-off) | ``expired``
(expiry pipeline settlement).

This revision is the Alembic equivalent of the hand-applied
``db/migrations/004_add_trade_structures.sql``. Use ONE of the two paths,
not both (see revision 001's header)::

    alembic stamp 004   # if you already applied the SQL by hand

Revision ID: 004
Revises: 003
Create Date: 2026-09-13
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "004"
down_revision: Union[str, None] = "003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "trade_structures",
        sa.Column("structure_id", sa.String(36), primary_key=True),
        sa.Column("strategy_name", sa.String(64), nullable=False),
        sa.Column("structure_type", sa.String(32), nullable=False),
        sa.Column("underlying", sa.String(32), nullable=False),
        sa.Column("expiry", sa.Date(), nullable=True),
        sa.Column(
            "status",
            sa.String(16),
            nullable=False,
            server_default=sa.text("'open'"),
        ),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "entry_cost", sa.Numeric(20, 4), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "fees_paid", sa.Numeric(20, 4), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "realized_pnl", sa.Numeric(20, 4), nullable=False, server_default=sa.text("0")
        ),
        sa.Column("legs", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "status IN ('open', 'closed', 'expired')",
            name="ck_trade_structures_status",
        ),
    )
    op.create_index(
        "ix_trade_structures_status", "trade_structures", ["status"]
    )
    op.create_index(
        "ix_trade_structures_underlying", "trade_structures", ["underlying"]
    )
    op.create_index(
        "ix_trade_structures_opened",
        "trade_structures",
        [sa.text("opened_at DESC")],
    )


def downgrade() -> None:
    # Gap PRD risk-mitigation rollback.
    op.drop_index("ix_trade_structures_opened", table_name="trade_structures")
    op.drop_index("ix_trade_structures_underlying", table_name="trade_structures")
    op.drop_index("ix_trade_structures_status", table_name="trade_structures")
    op.drop_table("trade_structures")
