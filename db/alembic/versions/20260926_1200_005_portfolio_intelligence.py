"""portfolio intelligence: alerts, greeks + regime history

Adds the audit/history tables behind the Portfolio Intelligence layer
(``docs/PORTFOLIO-INTELLIGENCE.md``):

* ``alerts`` — alert audit trail, one row per alert, updated through its
  lifecycle, plus which strategies were notified;
* ``portfolio_greeks_history`` — periodic aggregate Greeks snapshots;
* ``market_regime_history`` — VIX-band regime samples + transitions.

Alembic equivalent of the hand-applied
``db/migrations/005_portfolio_intelligence.sql``. Use ONE of the two paths::

    alembic stamp 005   # if you already applied the SQL by hand

Revision ID: 005
Revises: 004
Create Date: 2026-09-26
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "005"
down_revision: Union[str, None] = "004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "alerts",
        sa.Column("alert_id", sa.String(36), primary_key=True),
        sa.Column("alert_type", sa.String(50), nullable=False),
        sa.Column("alert_key", sa.String(200), nullable=False),
        sa.Column("severity", sa.String(20), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dismissed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dismissed_by", sa.String(100), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notified_strategies", sa.JSON(), nullable=True),
        sa.CheckConstraint(
            "severity IN ('critical','warning','info')", name="ck_alerts_severity"
        ),
    )
    op.create_index("idx_alerts_type", "alerts", ["alert_type"])
    op.create_index("idx_alerts_created", "alerts", [sa.text("created_at DESC")])
    op.create_index(
        "idx_alerts_active",
        "alerts",
        ["alert_type", "resolved_at"],
        postgresql_where=sa.text("resolved_at IS NULL"),
    )

    op.create_table(
        "portfolio_greeks_history",
        sa.Column("snapshot_id", sa.String(36), primary_key=True),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("net_delta", sa.Numeric(16, 2), nullable=True),
        sa.Column("net_gamma", sa.Numeric(16, 2), nullable=True),
        sa.Column("net_vega", sa.Numeric(16, 2), nullable=True),
        sa.Column("net_theta", sa.Numeric(16, 2), nullable=True),
        sa.Column("greeks_by_strategy", sa.JSON(), nullable=True),
        sa.Column("concentration_by_underlying", sa.JSON(), nullable=True),
        sa.Column("concentration_by_strike", sa.JSON(), nullable=True),
    )
    op.create_index(
        "idx_greeks_time", "portfolio_greeks_history", [sa.text("timestamp DESC")]
    )

    op.create_table(
        "market_regime_history",
        sa.Column("regime_id", sa.String(36), primary_key=True),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("regime", sa.String(20), nullable=False),
        sa.Column("vix", sa.Numeric(8, 2), nullable=True),
        sa.Column("realized_vol", sa.Numeric(8, 2), nullable=True),
        sa.Column("source", sa.String(64), nullable=True),
        sa.Column("previous_regime", sa.String(20), nullable=True),
        sa.Column(
            "regime_changed", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )
    op.create_index(
        "idx_regime_time", "market_regime_history", [sa.text("timestamp DESC")]
    )


def downgrade() -> None:
    op.drop_index("idx_regime_time", table_name="market_regime_history")
    op.drop_table("market_regime_history")
    op.drop_index("idx_greeks_time", table_name="portfolio_greeks_history")
    op.drop_table("portfolio_greeks_history")
    op.drop_index("idx_alerts_active", table_name="alerts")
    op.drop_index("idx_alerts_created", table_name="alerts")
    op.drop_index("idx_alerts_type", table_name="alerts")
    op.drop_table("alerts")
