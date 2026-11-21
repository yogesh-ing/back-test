"""Recorded option-chain history: ``option_chain_history`` (V3 Phase 4)

Plan V3 Phase 4 (``docs/OPTIONS-MODEL-BACKTEST-PLAN-V3.md`` §7). The replay
asset a ``ChainHistoryFeed`` prices option backtests from: bar-aligned,
parity-filtered chain observations carrying the observed premium, IV, Greeks
and five-level ``depth_json`` (JSONB on PostgreSQL). Only clean rows land here;
parity/junk rejects are logged to ``data/chain_rejected.log``, never stored.

Numbering: chains from 025 in both the alembic and SQL-twin lines.

Hand-applied equivalents: ``db/migrations/026_option_chain_history.sql``
(PostgreSQL) and ``026_option_chain_history.sqlite.sql``.

Revision ID: 026
Revises: 025
Create Date: 2026-11-20
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "026"
down_revision: Union[str, None] = "025"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

JSONB_COL = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "option_chain_history",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("bar_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("snapshot_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("underlying", sa.String(32), nullable=False),
        sa.Column("expiry", sa.Date(), nullable=False),
        sa.Column("strike", sa.Numeric(20, 4), nullable=False),
        sa.Column("option_type", sa.String(2), nullable=False),
        sa.Column("instrument_token", sa.String(64), nullable=False),
        sa.Column("trading_symbol", sa.String(64), nullable=False),
        sa.Column("lot_size", sa.Integer(), nullable=True),
        sa.Column("ltp", sa.Numeric(20, 4), nullable=True),
        sa.Column("bid", sa.Numeric(20, 4), nullable=True),
        sa.Column("ask", sa.Numeric(20, 4), nullable=True),
        sa.Column("iv", sa.Float(), nullable=True),
        sa.Column("delta", sa.Float(), nullable=True),
        sa.Column("gamma", sa.Float(), nullable=True),
        sa.Column("theta", sa.Float(), nullable=True),
        sa.Column("vega", sa.Float(), nullable=True),
        sa.Column("volume", sa.Integer(), nullable=True),
        sa.Column("oi", sa.Integer(), nullable=True),
        sa.Column("spot", sa.Numeric(20, 4), nullable=True),
        sa.Column("depth_json", JSONB_COL, nullable=True),
        sa.CheckConstraint("option_type IN ('CE', 'PE')", name="ck_option_chain_history_type"),
        sa.UniqueConstraint(
            "underlying", "expiry", "option_type", "strike", "bar_ts", "source",
            name="uq_option_chain_history_contract_bar",
        ),
    )
    op.create_index("ix_chain_history_bar", "option_chain_history", ["bar_ts"])
    op.create_index("ix_chain_history_underlying", "option_chain_history", ["underlying"])
    op.create_index(
        "ix_chain_history_lookup",
        "option_chain_history",
        ["underlying", "expiry", "option_type", "strike", "bar_ts"],
    )


def downgrade() -> None:
    op.drop_index("ix_chain_history_lookup", table_name="option_chain_history")
    op.drop_index("ix_chain_history_underlying", table_name="option_chain_history")
    op.drop_index("ix_chain_history_bar", table_name="option_chain_history")
    op.drop_table("option_chain_history")