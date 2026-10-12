"""market_data_cache timeframe-leading index for freshness top-1 seeks

The data-freshness chip (topbar) asks, per timeframe: "what is the newest
stored bar?" ``SELECT ts ... WHERE timeframe=:tf ORDER BY ts DESC LIMIT 1``
is an O(log n) seek with this index — but WITHOUT it an empty timeframe
(e.g. ``1day`` after the 2026-10-03 purge) walks the whole 17M-row ts
index backwards: the old grouped query cost 13.7s, unacceptable on the
render path.

Timescale note: ``market_data_cache`` is a hypertable and rejects
``CREATE INDEX CONCURRENTLY`` ("hypertables do not support concurrent
index creation", 2026-10-04), so this is a plain build — it locks chunks
briefly. Apply it while no fetch job is writing.

Hand-applied equivalents: ``db/migrations/019_mdc_timeframe_ts_index.sql``
(PostgreSQL) and ``019_mdc_timeframe_ts_index.sqlite.sql`` — use ONE path,
not both.

Revision ID: 019
Revises: 018
Create Date: 2026-10-04
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "019"
down_revision: Union[str, None] = "018"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "ix_mdc_tf_ts",
        "market_data_cache",
        ["timeframe", sa.text("ts DESC")],
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_index("ix_mdc_tf_ts", table_name="market_data_cache")
