"""multi-broker segments & execution routing (MIG-001, PRD-001 Phase B/C)

Multi-Broker Capital Allocation & Segmented Trading: a runner (portfolio)
may now belong to a *segment* (a capital partition mapped to one execution
broker), and every position/order remembers WHICH broker holds/filled it —
the audit trail LOM's venue-first rule and per-broker reconcile need.

* ``portfolios.segment``           — segment name ('' = no segment, legacy)
* ``portfolios.execution_broker``  — broker the runner routes orders to
  ('' = paper / legacy default broker)
* ``positions.broker``             — broker holding the position
  ('paper' = simulated)
* ``orders.broker``                — broker the order was sent to

No values are guessed for existing rows: everything backfills to the legacy
single-broker labels, which is exactly what those rows were.

This revision is the Alembic equivalent of the hand-applied
``db/migrations/008_multi_broker_segments.sql``. Use ONE of the two paths,
not both (see revision 001's header)::

    alembic stamp 006   # if you already applied the SQL by hand

Revision ID: 006
Revises: 005
Create Date: 2026-09-28
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "008"
down_revision: Union[str, None] = "005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "portfolios",
        sa.Column("segment", sa.String(length=64), server_default=sa.text("''"), nullable=False),
    )
    op.add_column(
        "portfolios",
        sa.Column(
            "execution_broker",
            sa.String(length=32),
            server_default=sa.text("''"),
            nullable=False,
        ),
    )
    op.add_column(
        "positions",
        sa.Column(
            "broker", sa.String(length=32), server_default=sa.text("'paper'"), nullable=False
        ),
    )
    op.add_column(
        "orders",
        sa.Column(
            "broker", sa.String(length=32), server_default=sa.text("'paper'"), nullable=False
        ),
    )
    # Per-broker views (risk cards, reconcile, intelligence by_broker).
    op.create_index("ix_positions_broker", "positions", ["broker"])
    op.create_index("ix_orders_broker", "orders", ["broker"])
    # Defensive backfill (normally a no-op: server defaults classify every
    # existing row at ALTER time as legacy single-broker/paper).
    op.execute("UPDATE portfolios SET segment='' WHERE segment IS NULL")
    op.execute("UPDATE portfolios SET execution_broker='' WHERE execution_broker IS NULL")
    op.execute("UPDATE positions SET broker='paper' WHERE broker IS NULL")
    op.execute("UPDATE orders SET broker='paper' WHERE broker IS NULL")


def downgrade() -> None:
    op.drop_index("ix_orders_broker", table_name="orders")
    op.drop_index("ix_positions_broker", table_name="positions")
    op.drop_column("orders", "broker")
    op.drop_column("positions", "broker")
    op.drop_column("portfolios", "execution_broker")
    op.drop_column("portfolios", "segment")
