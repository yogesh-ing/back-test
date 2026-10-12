"""market_holidays: calendar schema and 2026 NSE holiday seed

Revision ID: 017
Revises: 016
Create Date: 2026-10-02
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "017"
down_revision: Union[str, None] = "016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "market_holidays",
        sa.Column("holiday_date", sa.Date(), primary_key=True, nullable=False),
        sa.Column("segment", sa.Text(), nullable=False, server_default="equity"),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("is_trading_holiday", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("source", sa.Text(), nullable=False, server_default="nse"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )


def downgrade() -> None:
    op.drop_table("market_holidays")
