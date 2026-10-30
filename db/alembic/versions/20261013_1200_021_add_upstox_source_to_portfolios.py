"""widen portfolios.source CHECK to admit upstox

Upstox is a registered broker with its own live feed (UpstoxLiveFeed /
UpstoxBarFeed) and its own taxonomy tag. This unlock makes
``source='upstox'`` a spawnable runner source, so an Upstox-fed book is a
legitimate portfolio row — the pre-016 CHECK would have rejected it at
persistence time even though every layer above the DB accepts it.

No data changes: existing rows keep their values (all of them remain valid
under the widened CHECK).

**This revision was numbered 020 until 2026-10-30 (BUG-38 / B11).** Two files
declared ``revision = "020"`` on the same parent ``019``, so the chain had two
heads, ``alembic upgrade`` refused to resolve it, and every later schema fix
had nowhere to chain. ``020`` stays with ``option_intent_journal`` because the
live ``forward_test`` ledger physically has that table (probed 2026-10-30:
``option_intent_events`` plus all three indexes) while ``ck_portfolios_source``
there still reads ``('synthetic','replay','mstock','dhan')`` — i.e. the journal
half landed and this half did not. Linear now: ``019 → 020 → 021``.

Both steps are safe to run on a database that already has the objects: the
journal's ``upgrade`` skips when the table exists, and this one drops the
constraint ``IF EXISTS`` before re-adding it. So ``alembic upgrade 019:021`` on
live is a no-op plus the widening — no manual stamp needed. (``stamp`` still
works if you prefer it: ``020`` means the journal now, ``021`` means this.)

Hand-applied note: this revision is the Alembic equivalent of
``db/migrations/020_add_upstox_source.sql``. Use ONE of the two paths, not both
(see revision 001's header) — and keep in mind they are *separate ledgers*: the
SQL files record into ``schema_migrations`` under their own key ``020``, which
is why those files were not renumbered with this one.

Revision ID: 021
Revises: 020
Create Date: 2026-10-13 (renumbered 2026-10-30)
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "021"
down_revision: Union[str, None] = "020"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_WIDENED = "source IN ('synthetic','replay','mstock','dhan','upstox')"
_NARROWED = "source IN ('synthetic','replay','mstock','dhan')"


def upgrade() -> None:
    # alembic.op has no drop_check_constraint — raw SQL, as in revision 003.
    # DROP IF EXISTS keeps the step idempotent alongside the SQL-file path.
    op.execute("ALTER TABLE portfolios DROP CONSTRAINT IF EXISTS ck_portfolios_source")
    op.create_check_constraint("ck_portfolios_source", "portfolios", _WIDENED)


def downgrade() -> None:
    # Restores the 016-era CHECK (upstox out). Fails if an upstox row exists —
    # delete or re-tag those rows first (they are the rows this migration
    # legitimized).
    op.execute("ALTER TABLE portfolios DROP CONSTRAINT IF EXISTS ck_portfolios_source")
    op.create_check_constraint("ck_portfolios_source", "portfolios", _NARROWED)
