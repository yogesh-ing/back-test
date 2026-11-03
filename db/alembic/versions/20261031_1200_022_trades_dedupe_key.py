"""trades idempotency key (B1) — stop a resume from re-inserting history

QA 2026-10-30 measured the ``trades`` table at **6,476 rows for 2,878 real
round trips**: every restart restores a runner's closed-trades cache and
:class:`~backtest.forward.trade_persistence.LiveTradePersister` flushed it
again with a fresh ``trade_id``. ₹2,23,635.74 of the ledger was the same losses
counted up to 16 times, and any realized-P&L or win-rate figure read from this
table was wrong by a multiple.

``dedupe_key`` is the durable half of the fix — SHA-256 over the columns that
identify a round trip (portfolio, symbol, direction, entry/exit time,
entry/exit price, quantity), with a unique index so two writers can never both
land the same trade. It is deliberately **nullable**: the duplicates already in
the table would collide the moment it was backfilled, so the constraint guards
from here forward and :mod:`scripts.repair_trade_duplicates` owns the at-rest
cleanup as a separately-approved step. P&L is not in the key — a corrected fee
must not look like a new trade.

Additive only, no rows touched. The steps are guarded so this is safe on a
database that took the hand-applied SQL path instead (see revision 001's
header — one path, ``alembic_version`` or ``schema_migrations``, not both):

* Manual SQL  -> ``db/migrations/022_trades_dedupe_key.sql`` (PostgreSQL) and
  ``022_trades_dedupe_key.sqlite.sql``
* Alembic     -> this file

Revision ID: 022
Revises: 021
Create Date: 2026-10-31
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "022"
down_revision: Union[str, None] = "021"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_INDEX = "uq_trades_dedupe_key"


def _columns() -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns("trades")}


def upgrade() -> None:
    if "dedupe_key" not in _columns():
        op.add_column(
            "trades",
            sa.Column("dedupe_key", sa.String(64), nullable=True),
        )
    # IF NOT EXISTS exists on both engines, and keeps the unique index off the
    # pre-key rows: NULLs never conflict.
    op.execute(
        f"CREATE UNIQUE INDEX IF NOT EXISTS {_INDEX} ON trades (dedupe_key)"
    )


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {_INDEX}")
    if "dedupe_key" in _columns():
        op.drop_column("trades", "dedupe_key")
