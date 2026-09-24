"""parameter optimization engine: presets + audit tables

Creates ``parameter_presets`` (named parameter sets, incl. the automatic
pre-apply snapshots used for one-click rollback) and ``optimization_audit``
(who applied which parameters to which runner, when).

Adapted from the PRD "Migration 002": chained onto ``005``, timezone-aware
``DateTime`` columns, a CHECK on ``parameter_presets.source`` (adds the
``snapshot`` source for rollback snapshots) and on
``optimization_audit.applied_to_mode``, and the ``set_updated_at()``
trigger for ``parameter_presets.updated_at``.

Hand-applied equivalent: ``db/migrations/006_optimization_audit_presets.sql``.

Revision ID: 006
Revises: 005
Create Date: 2026-09-24
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "006"
down_revision: Union[str, None] = "005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

UUID = sa.String(36).with_variant(postgresql.UUID(as_uuid=False), "postgresql")
JSONB = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
TSTZ = sa.DateTime(timezone=True)


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _uuid_pk(name: str) -> sa.Column:
    default = sa.text("gen_random_uuid()") if _is_postgres() else None
    return sa.Column(name, UUID, primary_key=True, server_default=default)


def upgrade() -> None:
    op.create_table(
        "parameter_presets",
        _uuid_pk("preset_id"),
        sa.Column("strategy_id", sa.String(100), nullable=False),
        sa.Column("name", sa.String(100), nullable=False,
                  comment="Conservative, Aggressive, Optimized_2024Q1, etc."),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("params", JSONB, nullable=False, comment="Complete parameter set"),
        sa.Column("source", sa.String(50), nullable=False,
                  comment="optimization, manual, default, import, snapshot"),
        sa.Column("optimization_run_id", UUID, nullable=True,
                  comment="Optimization run this preset came from"),
        sa.Column("backtest_metrics", JSONB, nullable=True,
                  comment="Metrics when this preset was created/validated"),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.text("true"),
                  comment="False if deprecated/archived"),
        sa.Column("applied_count", sa.Integer, nullable=False, server_default=sa.text("0"),
                  comment="How many times applied to runners"),
        sa.Column("last_applied_at", TSTZ, nullable=True),
        sa.Column("created_by", sa.String(100), nullable=True),
        sa.Column("created_at", TSTZ, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TSTZ, nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["optimization_run_id"], ["optimization_runs.run_id"],
            name="fk_presets_opt_run", ondelete="SET NULL",
        ),
        sa.UniqueConstraint("strategy_id", "name", name="uq_strategy_preset_name"),
        sa.CheckConstraint(
            "source IN ('optimization','manual','default','import','snapshot')",
            name="ck_presets_source",
        ),
        sa.CheckConstraint("applied_count >= 0", name="ck_presets_applied_nonneg"),
        comment="Saved parameter presets for strategies",
    )

    op.create_table(
        "optimization_audit",
        _uuid_pk("audit_id"),
        sa.Column("run_id", UUID, nullable=True,
                  comment="Optimization run (if applicable)"),
        sa.Column("strategy_id", sa.String(100), nullable=False),
        sa.Column("action", sa.String(50), nullable=False,
                  comment="run_started, run_completed, run_cancelled, params_applied, "
                          "params_rolled_back, preset_saved, ..."),
        sa.Column("action_details", JSONB, nullable=True),
        sa.Column("old_params", JSONB, nullable=True, comment="Parameters before change"),
        sa.Column("new_params", JSONB, nullable=True, comment="Parameters after change"),
        sa.Column("params_diff", JSONB, nullable=True, comment="Computed diff for quick review"),
        sa.Column("applied_to_bucket", sa.String(100), nullable=True,
                  comment="Runner instance / bucket affected"),
        sa.Column("applied_to_mode", sa.String(20), nullable=True, comment="paper or live"),
        sa.Column("runner_restarted", sa.Boolean, nullable=True),
        sa.Column("expected_impact", JSONB, nullable=True,
                  comment="Expected metric changes from optimization"),
        sa.Column("actual_impact", JSONB, nullable=True,
                  comment="Measured metric changes after X days (backfilled)"),
        sa.Column("requires_approval", sa.Boolean, nullable=False,
                  server_default=sa.text("false")),
        sa.Column("approved_by", sa.String(100), nullable=True),
        sa.Column("approved_at", TSTZ, nullable=True),
        sa.Column("user_id", sa.String(100), nullable=True),
        sa.Column("timestamp", TSTZ, nullable=False, server_default=sa.func.now()),
        sa.Column("ip_address", sa.String(45), nullable=True),
        sa.Column("user_agent", sa.String(255), nullable=True),
        sa.ForeignKeyConstraint(
            ["run_id"], ["optimization_runs.run_id"],
            name="fk_audit_opt_run", ondelete="SET NULL",
        ),
        sa.CheckConstraint(
            "applied_to_mode IS NULL OR applied_to_mode IN ('paper', 'live')",
            name="ck_audit_mode",
        ),
        comment="Audit log for all optimization and parameter change actions",
    )

    if _is_postgres():
        op.execute(
            """
            CREATE TRIGGER trg_parameter_presets_updated_at
            BEFORE UPDATE ON parameter_presets
            FOR EACH ROW EXECUTE FUNCTION set_updated_at();
            """
        )


def downgrade() -> None:
    if _is_postgres():
        op.execute(
            "DROP TRIGGER IF EXISTS trg_parameter_presets_updated_at ON parameter_presets"
        )
    op.drop_table("optimization_audit")
    op.drop_table("parameter_presets")
