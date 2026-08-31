"""add mode and source to portfolios

Adds the run-classification columns to the portfolios root aggregate:

* ``mode``   — ``'paper'`` | ``'live'`` (simulated fills vs real broker orders)
* ``source`` — ``'synthetic'`` | ``'replay'`` | ``'mstock'`` (bar origin)

This revision is the Alembic equivalent of the hand-applied
``db/migrations/002_add_mode_source.sql``. Use ONE of the two paths, not both
(see revision 001's header):

* Manual SQL  -> tracked in the ``schema_migrations`` table
* Alembic     -> tracked in ``alembic_version``

If you applied the SQL by hand and want to adopt Alembic afterwards, stamp
the database instead of running the upgrade::

    alembic stamp 002

Existing rows are classified ``paper``/``synthetic`` by the server defaults;
the UPDATE is a defensive backfill (ticket P1.1).

Revision ID: 002
Revises: 001
Create Date: 2026-08-31
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "002"
down_revision: Union[str, None] = "001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "portfolios",
        sa.Column("mode", sa.String(length=16), server_default=sa.text("'paper'"), nullable=False),
    )
    op.add_column(
        "portfolios",
        sa.Column(
            "source", sa.String(length=16), server_default=sa.text("'synthetic'"), nullable=False
        ),
    )
    op.create_check_constraint("ck_portfolios_mode", "portfolios", "mode IN ('paper','live')")
    op.create_check_constraint(
        "ck_portfolios_source", "portfolios", "source IN ('synthetic','replay','mstock')"
    )
    # Defensive backfill (normally a no-op: the server defaults already
    # classify every existing row at ALTER time).
    op.execute(
        "UPDATE portfolios SET mode='paper', source='synthetic' "
        "WHERE mode IS NULL OR source IS NULL"
    )


def downgrade() -> None:
    op.drop_check_constraint("ck_portfolios_source", "portfolios")
    op.drop_check_constraint("ck_portfolios_mode", "portfolios")
    op.drop_column("portfolios", "source")
    op.drop_column("portfolios", "mode")
