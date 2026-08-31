"""canonical timeframe naming for market_data_cache

Fixes the timeframe drift (ticket P4.3): the UI (1D/1H/4H/...), the mStock
ingest (1min/60min/day/week) and the configs (1min) each spoke their own
vocabulary, so a ``1H`` backtest could never find its ``60min`` bars. The
ONE canonical set, resolved with the lead:

    1min | 5min | 15min | 1hour | 4hour | 1day | 1week

Existing rows are remapped where an exact equivalent exists
(60min->1hour, day->1day, week->1week); rows in timeframes without a
canonical equivalent (3min, 30min, month) are deleted — the table is a
re-ingestable cache, not a ledger.

This revision is the Alembic equivalent of the hand-applied
``db/migrations/003_canonical_timeframes.sql``. Use ONE of the two paths,
not both (see revision 001's header):

* Manual SQL  -> tracked in the ``schema_migrations`` table
* Alembic     -> tracked in ``alembic_version``

If you applied the SQL by hand and want to adopt Alembic afterwards, stamp
the database instead of running the upgrade::

    alembic stamp 003

Revision ID: 003
Revises: 002
Create Date: 2026-08-31
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "003"
down_revision: Union[str, None] = "002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Drop the old CHECK FIRST — the remaps below would violate it
    # (e.g. 'day' -> '1day' is not a value the old CHECK admits).
    op.drop_check_constraint("ck_mdc_timeframe", "market_data_cache")

    # Remap rows to the canonical names (exact 1:1 equivalents only)
    op.execute(
        "UPDATE market_data_cache SET timeframe='1hour' WHERE timeframe='60min'"
    )
    op.execute("UPDATE market_data_cache SET timeframe='1day' WHERE timeframe='day'")
    op.execute("UPDATE market_data_cache SET timeframe='1week' WHERE timeframe='week'")
    # No canonical equivalent: drop the rows (re-ingestable cache)
    op.execute(
        "DELETE FROM market_data_cache WHERE timeframe IN ('3min', '30min', 'month')"
    )

    op.create_check_constraint(
        "ck_mdc_timeframe",
        "market_data_cache",
        "timeframe IN ('1min', '5min', '15min', '1hour', '4hour', '1day', '1week')",
    )


def downgrade() -> None:
    # Restores the pre-003 CHECK. Row deletions are NOT undone — they are
    # re-ingestable cache data (re-run the ingest to restore them).
    op.drop_check_constraint("ck_mdc_timeframe", "market_data_cache")
    op.create_check_constraint(
        "ck_mdc_timeframe",
        "market_data_cache",
        "timeframe IN ('1min','3min','5min','15min','30min','60min','1hour','day','week','month')",
    )
