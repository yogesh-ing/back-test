"""widen portfolios.source CHECK to admit dhan

Dhan is a registered broker with its own live feed (DhanLiveFeed /
DhanBarFeed) and its own taxonomy tag. The 2026-10-01 unlock made
``source='dhan'`` a spawnable runner source, so a dhan-fed book is a
legitimate portfolio row — the 002-era CHECK would have rejected it at
persistence time even though every layer above the DB accepts it.

No data changes: existing rows keep their values (all of them remain valid
under the widened CHECK).

This revision is the Alembic equivalent of the hand-applied
``db/migrations/016_add_dhan_source.sql``. Use ONE of the two paths, not
both (see revision 001's header):

* Manual SQL  -> tracked in the ``schema_migrations`` table
* Alembic     -> tracked in ``alembic_version``

If you applied the SQL by hand and want to adopt Alembic afterwards, stamp
the database instead of running the upgrade::

    alembic stamp 016

Revision ID: 016
Revises: 015
Create Date: 2026-10-01
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "016"
down_revision: Union[str, None] = "015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_WIDENED = "source IN ('synthetic','replay','mstock','dhan')"
_NARROWED = "source IN ('synthetic','replay','mstock')"


def upgrade() -> None:
    # alembic.op has no drop_check_constraint — raw SQL, as in revision 003.
    # DROP IF EXISTS keeps the step idempotent alongside the SQL-file path.
    op.execute("ALTER TABLE portfolios DROP CONSTRAINT IF EXISTS ck_portfolios_source")
    op.create_check_constraint("ck_portfolios_source", "portfolios", _WIDENED)


def downgrade() -> None:
    # Restores the pre-016 CHECK. Fails if a dhan row exists — delete or
    # re-tag those rows first (they are the rows this migration legitimized).
    op.execute("ALTER TABLE portfolios DROP CONSTRAINT IF EXISTS ck_portfolios_source")
    op.create_check_constraint("ck_portfolios_source", "portfolios", _NARROWED)
