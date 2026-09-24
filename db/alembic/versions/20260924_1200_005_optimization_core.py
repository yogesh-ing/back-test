"""parameter optimization engine: core tables

Creates ``optimization_runs`` (one row per optimization job) and
``optimization_results`` (one row per tested parameter combination).

Adapted from the PRD "Migration 001" for this repository:

* chained onto revision ``004`` (the PRD draft used ``down_revision=None``,
  which would have created a second root next to the forward-testing chain);
* ``sa.DateTime(timezone=True)`` instead of the non-existent
  ``sqlalchemy.dialects.postgresql.TIMESTAMPTZ`` import;
* ``updated_at`` is maintained by the shared ``set_updated_at()`` trigger
  function from revision 001 (``onupdate=`` on ``op.create_table`` is a
  Python-side ORM hint and does nothing in the database);
* ``status``/``objective_function``/``method`` carry CHECK constraints, and
  ``status`` also allows ``draft`` and ``paused`` (setup page "Save as
  Draft", progress page "Pause");
* extra columns ``baseline_*``, ``analysis`` and ``robustness_score`` store
  the original-vs-optimized comparison and the sensitivity analysis;
* UUID keys default to ``gen_random_uuid()`` (core since PostgreSQL 13 — no
  ``uuid-ossp`` extension required). The application always supplies its own
  uuid4, so the SQLite dev path needs no server default.

Hand-applied equivalent: ``db/migrations/005_optimization_core.sql``. Use ONE
of the two paths (see revision 001's header); if you applied the SQL by hand::

    alembic stamp 005

Revision ID: 005
Revises: 004
Create Date: 2026-09-24
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "005"
down_revision: Union[str, None] = "004"
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
        "optimization_runs",
        _uuid_pk("run_id"),
        sa.Column("strategy_id", sa.String(100), nullable=False),
        sa.Column("bucket_id", sa.String(100), nullable=True),
        # Configuration
        sa.Column("objective_function", sa.String(50), nullable=False,
                  comment="sharpe, sortino, calmar, total_return, profit_factor, expectancy"),
        sa.Column("method", sa.String(50), nullable=False,
                  comment="grid, random, bayesian, genetic"),
        sa.Column("param_space", JSONB, nullable=False,
                  comment="Parameter definitions with min/max/step/current"),
        sa.Column("constraints", JSONB, nullable=True, comment="Array of constraint objects"),
        sa.Column("backtest_config", JSONB, nullable=False,
                  comment="Symbol, dates, capital, timeframe, engine, mode"),
        # Status tracking
        sa.Column("status", sa.String(20), nullable=False, server_default=sa.text("'pending'"),
                  comment="draft, pending, running, paused, completed, failed, cancelled"),
        sa.Column("started_at", TSTZ, nullable=True),
        sa.Column("completed_at", TSTZ, nullable=True),
        sa.Column("error_message", sa.Text, nullable=True),
        # Progress
        sa.Column("total_combinations", sa.Integer, nullable=True),
        sa.Column("tested_combinations", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("valid_combinations", sa.Integer, nullable=False, server_default=sa.text("0")),
        # Results summary
        sa.Column("best_params", JSONB, nullable=True),
        sa.Column("best_score", sa.Numeric(10, 4), nullable=True),
        sa.Column("best_metrics", JSONB, nullable=True,
                  comment="Full metrics dict for best result"),
        # Original-vs-optimized comparison (extension)
        sa.Column("baseline_params", JSONB, nullable=True,
                  comment="Parameters the strategy ran with before optimization"),
        sa.Column("baseline_metrics", JSONB, nullable=True),
        sa.Column("baseline_score", sa.Numeric(10, 4), nullable=True),
        # Walk-forward validation
        sa.Column("walk_forward_enabled", sa.Boolean, nullable=False,
                  server_default=sa.text("false")),
        sa.Column("walk_forward_config", JSONB, nullable=True,
                  comment="Train/test periods, step size"),
        sa.Column("walk_forward_results", JSONB, nullable=True,
                  comment="Per-split results + summary"),
        sa.Column("overfitted", sa.Boolean, nullable=True,
                  comment="True if WF shows significant degradation"),
        sa.Column("avg_train_score", sa.Numeric(10, 4), nullable=True),
        sa.Column("avg_test_score", sa.Numeric(10, 4), nullable=True),
        # Analytics (extension)
        sa.Column("analysis", JSONB, nullable=True,
                  comment="Sensitivity curves, equity curves, warning signs"),
        sa.Column("robustness_score", sa.Numeric(4, 2), nullable=True,
                  comment="0-10, flatness of the sensitivity curves"),
        # Metadata
        sa.Column("created_by", sa.String(100), nullable=True),
        sa.Column("created_at", TSTZ, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TSTZ, nullable=False, server_default=sa.func.now()),
        sa.Column("task_id", sa.String(100), nullable=True,
                  comment="Background worker / task id"),
        sa.CheckConstraint(
            "status IN ('draft','pending','running','paused','completed','failed','cancelled')",
            name="ck_opt_runs_status",
        ),
        sa.CheckConstraint(
            "objective_function IN ('sharpe','sortino','calmar','total_return',"
            "'profit_factor','expectancy')",
            name="ck_opt_runs_objective",
        ),
        sa.CheckConstraint(
            "method IN ('grid','random','bayesian','genetic')", name="ck_opt_runs_method"
        ),
        sa.CheckConstraint("tested_combinations >= 0", name="ck_opt_runs_tested_nonneg"),
        sa.CheckConstraint("valid_combinations >= 0", name="ck_opt_runs_valid_nonneg"),
        comment="Main table for optimization runs",
    )

    op.create_table(
        "optimization_results",
        _uuid_pk("result_id"),
        sa.Column("run_id", UUID, nullable=False),
        sa.Column("params", JSONB, nullable=False, comment="Parameter set for this combination"),
        # Core metrics
        sa.Column("sharpe", sa.Numeric(10, 4), nullable=True),
        sa.Column("sortino", sa.Numeric(10, 4), nullable=True),
        sa.Column("calmar", sa.Numeric(10, 4), nullable=True),
        sa.Column("total_return", sa.Numeric(10, 4), nullable=True,
                  comment="Decimal format: 0.124 = 12.4%"),
        sa.Column("cagr", sa.Numeric(10, 4), nullable=True),
        sa.Column("max_drawdown", sa.Numeric(10, 4), nullable=True,
                  comment="Negative decimal: -0.083 = -8.3%"),
        sa.Column("drawdown_duration_days", sa.Integer, nullable=True),
        # Trade statistics
        sa.Column("profit_factor", sa.Numeric(10, 4), nullable=True),
        sa.Column("win_rate", sa.Numeric(5, 2), nullable=True, comment="Percentage: 54.2"),
        sa.Column("total_trades", sa.Integer, nullable=True),
        sa.Column("winning_trades", sa.Integer, nullable=True),
        sa.Column("losing_trades", sa.Integer, nullable=True),
        sa.Column("expectancy", sa.Numeric(10, 2), nullable=True,
                  comment="Average PnL per closed trade in currency"),
        sa.Column("avg_win", sa.Numeric(10, 2), nullable=True),
        sa.Column("avg_loss", sa.Numeric(10, 2), nullable=True),
        sa.Column("largest_win", sa.Numeric(10, 2), nullable=True),
        sa.Column("largest_loss", sa.Numeric(10, 2), nullable=True),
        # Risk metrics
        sa.Column("volatility", sa.Numeric(10, 4), nullable=True,
                  comment="Annualized volatility"),
        sa.Column("downside_deviation", sa.Numeric(10, 4), nullable=True),
        # Execution quality
        sa.Column("avg_holding_time_minutes", sa.Integer, nullable=True),
        sa.Column("avg_slippage", sa.Numeric(10, 4), nullable=True),
        # Constraint validation
        sa.Column("constraints_met", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("constraint_violations", JSONB, nullable=True,
                  comment="Array of violated constraint details"),
        # Scoring
        sa.Column("objective_score", sa.Numeric(10, 4), nullable=False,
                  comment="Value of objective function for this result"),
        sa.Column("rank", sa.Integer, nullable=True,
                  comment="Rank among constraint-passing results (1 = best)"),
        sa.Column("full_result", JSONB, nullable=True,
                  comment="Equity curve etc. for the best/baseline rows"),
        sa.Column("created_at", TSTZ, nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["run_id"], ["optimization_runs.run_id"],
            name="fk_opt_results_run", ondelete="CASCADE",
        ),
        sa.CheckConstraint("rank IS NULL OR rank >= 1", name="ck_opt_results_rank_pos"),
        sa.CheckConstraint(
            "win_rate IS NULL OR (win_rate >= 0 AND win_rate <= 100)",
            name="ck_opt_results_win_rate",
        ),
        comment="Individual parameter combination results",
    )

    if _is_postgres():
        # Shared trigger function from revision 001 — CREATE OR REPLACE keeps
        # this revision self-sufficient on databases that were stamped.
        op.execute(
            """
            CREATE OR REPLACE FUNCTION set_updated_at()
            RETURNS TRIGGER AS $$
            BEGIN
                NEW.updated_at = now();
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;
            """
        )
        op.execute(
            """
            CREATE TRIGGER trg_optimization_runs_updated_at
            BEFORE UPDATE ON optimization_runs
            FOR EACH ROW EXECUTE FUNCTION set_updated_at();
            """
        )


def downgrade() -> None:
    if _is_postgres():
        op.execute(
            "DROP TRIGGER IF EXISTS trg_optimization_runs_updated_at ON optimization_runs"
        )
        # set_updated_at() is owned by revision 001 — never drop it here.
    op.drop_table("optimization_results")
    op.drop_table("optimization_runs")
