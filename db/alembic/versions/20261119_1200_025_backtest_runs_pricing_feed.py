"""Options model backtest provenance: ``backtest_runs.pricing_feed`` + ``model_params``

Plan V3 Phase 3 (``docs/OPTIONS-MODEL-BACKTEST-PLAN-V3.md``). Every persisted
run must state how its options were priced: ``pricing_feed`` is ``model_bs``
for Black-Scholes model runs (quarantined from certify/deploy), ``NULL`` for
legacy equity runs. ``model_params`` holds the feed configuration (IV source,
premium, spread floors, exit knobs) as JSONB.

Numbering note: the plan assigned "025" assuming alembic head 024; the actual
alembic head is 023 and the SQL-twin chain already used 024 for Sprint D
(open option positions, hand-applied, no alembic revision). This migration
takes 025 in BOTH chains, chaining from 023, so the two numbering lines never
collide.

Hand-applied equivalents: ``db/migrations/025_backtest_runs_pricing_feed.sql``
(PostgreSQL) and ``025_backtest_runs_pricing_feed.sqlite.sql``.

Revision ID: 025
Revises: 023
Create Date: 2026-11-19
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "025"
down_revision: Union[str, None] = "023"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

JSONB_COL = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.add_column(
        "backtest_runs",
        sa.Column("pricing_feed", sa.String(32), nullable=True),
    )
    op.add_column(
        "backtest_runs",
        sa.Column("model_params", JSONB_COL, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("backtest_runs", "model_params")
    op.drop_column("backtest_runs", "pricing_feed")