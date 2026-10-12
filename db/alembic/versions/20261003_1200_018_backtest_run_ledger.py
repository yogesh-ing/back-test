"""backtest run ledger: backtest_runs, backtest_compare_runs, backtest_run_series

Implements docs/BACKTEST-RUN-PERSISTENCE-PRD.md (migration scope §9):
three ledger tables + the ``parameter_presets.backtest_run_id`` lineage FK,
one revision. Append-only rows; heavy series live in their own table and are
retention-capped (N=500 per strategy/symbol/timeframe group).

Hand-applied equivalents: ``db/migrations/018_backtest_run_ledger.sql``
(PostgreSQL) and ``018_backtest_run_ledger.sqlite.sql`` — use ONE path, not
both (see revision 001's header).

Revision ID: 018
Revises: 017
Create Date: 2026-10-03
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "018"
down_revision: Union[str, None] = "017"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

UUID = sa.String(36).with_variant(postgresql.UUID(as_uuid=False), "postgresql")
JSONB = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
#: Mirrors models.Score / win_rate columns (optimizer RESULT_METRIC_COLUMNS).
SCORE = sa.Numeric(precision=10, scale=4)
PCT = sa.Numeric(precision=5, scale=2)
MONEY = sa.Numeric(precision=20, scale=4)
BIGPK = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    # -- R1b: comparison parent (created first; backtest_runs FKs into it) ----
    op.create_table(
        "backtest_compare_runs",
        sa.Column("compare_id", UUID, primary_key=True, nullable=False),
        sa.Column(
            "comparison_mode",
            sa.String(20),
            nullable=False,
            server_default=sa.text("'strategies'"),
        ),
        sa.Column(
            "comparison_version", sa.Integer(), nullable=False, server_default=sa.text("1")
        ),
        sa.Column("config_snapshot", JSONB, nullable=False),
        sa.Column("data_source", sa.String(30), nullable=True),
        sa.Column("date_from", sa.Date(), nullable=True),
        sa.Column("date_to", sa.Date(), nullable=True),
        sa.Column("symbols_used", JSONB, nullable=True),
        sa.Column("engines_used", JSONB, nullable=True),
        sa.Column("provenance", JSONB, nullable=True),
        sa.Column("comparison_block", JSONB, nullable=True),
        sa.Column("slot_count", sa.Integer(), nullable=False),
        sa.Column("slot_errors", JSONB, nullable=True),
        sa.Column("created_by", sa.String(100), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "comparison_mode IN ('strategies','generalization')",
            name="ck_bt_compares_mode",
        ),
        sa.CheckConstraint("slot_count >= 0", name="ck_bt_compares_slots_nonneg"),
    )
    op.create_index(
        "ix_backtest_compares_created",
        "backtest_compare_runs",
        [sa.literal_column("created_at DESC")],
    )

    # -- R1: the run ledger -----------------------------------------------------
    op.create_table(
        "backtest_runs",
        sa.Column("run_id", UUID, primary_key=True, nullable=False),
        sa.Column("config_hash", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False, server_default=sa.text("'single'")),
        sa.Column("parent_compare_id", UUID, nullable=True),
        sa.Column("optimization_run_id", UUID, nullable=True),
        sa.Column("strategy_id", sa.String(100), nullable=False),
        sa.Column("symbol", sa.String(30), nullable=False),
        sa.Column("timeframe", sa.String(10), nullable=False),
        sa.Column("date_from", sa.Date(), nullable=False),
        sa.Column("date_to", sa.Date(), nullable=False),
        sa.Column("capital", MONEY, nullable=False),
        sa.Column("engine", sa.String(30), nullable=False),
        sa.Column(
            "payload_version", sa.Integer(), nullable=False, server_default=sa.text("1")
        ),
        sa.Column("params", JSONB, nullable=False),
        sa.Column("config", JSONB, nullable=False),
        sa.Column("readiness", JSONB, nullable=True),
        sa.Column("cost_shock", JSONB, nullable=True),
        sa.Column("metrics", JSONB, nullable=True),
        sa.Column("sharpe", SCORE, nullable=True),
        sa.Column("sortino", SCORE, nullable=True),
        sa.Column("calmar", SCORE, nullable=True),
        sa.Column("total_return", SCORE, nullable=True),
        sa.Column("cagr", SCORE, nullable=True),
        sa.Column("max_drawdown", SCORE, nullable=True),
        sa.Column("profit_factor", SCORE, nullable=True),
        sa.Column("win_rate", PCT, nullable=True),
        sa.Column("total_trades", sa.Integer(), nullable=True),
        sa.Column(
            "series_status",
            sa.String(12),
            nullable=False,
            server_default=sa.text("'write_failed'"),
        ),
        sa.Column("data_source", sa.String(30), nullable=True),
        sa.Column("bars_count", sa.Integer(), nullable=True),
        sa.Column("fetched_first_ts", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fetched_last_ts", sa.DateTime(timezone=True), nullable=True),
        sa.Column("data_fetch_date", sa.Date(), nullable=True),
        sa.Column("provenance", JSONB, nullable=True),
        sa.Column("code_fingerprint", JSONB, nullable=False),
        sa.Column("created_by", sa.String(100), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "kind IN ('single','compare_slot','optimizer_baseline')",
            name="ck_bt_runs_kind",
        ),
        sa.CheckConstraint(
            "series_status IN ('present','evicted','write_failed')",
            name="ck_bt_runs_series_status",
        ),
        sa.CheckConstraint(
            "win_rate IS NULL OR (win_rate >= 0 AND win_rate <= 100)",
            name="ck_bt_runs_win_rate",
        ),
        sa.ForeignKeyConstraint(
            ["parent_compare_id"],
            ["backtest_compare_runs.compare_id"],
            name="fk_bt_runs_compare",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["optimization_run_id"],
            ["optimization_runs.run_id"],
            name="fk_bt_runs_opt_run",
            ondelete="SET NULL",
        ),
    )
    op.create_index(
        "ix_backtest_runs_list",
        "backtest_runs",
        ["strategy_id", "symbol", "timeframe", sa.literal_column("created_at DESC")],
    )
    op.create_index(
        "ix_backtest_runs_config",
        "backtest_runs",
        ["config_hash", sa.literal_column("created_at DESC")],
    )
    op.create_index(
        "ix_backtest_runs_kind",
        "backtest_runs",
        ["kind", sa.literal_column("created_at DESC")],
    )
    op.create_index(
        "ix_backtest_runs_opt",
        "backtest_runs",
        ["optimization_run_id"],
        postgresql_where=sa.text("optimization_run_id IS NOT NULL"),
        sqlite_where=sa.text("optimization_run_id IS NOT NULL"),
    )
    op.create_index(
        "ix_backtest_runs_compare",
        "backtest_runs",
        ["parent_compare_id"],
        postgresql_where=sa.text("parent_compare_id IS NOT NULL"),
        sqlite_where=sa.text("parent_compare_id IS NOT NULL"),
    )

    # -- R2: heavy series, written in its own transaction -----------------------
    op.create_table(
        "backtest_run_series",
        sa.Column("run_id", UUID, primary_key=True, nullable=False),
        sa.Column("trades", JSONB, nullable=True),
        sa.Column("equity", JSONB, nullable=True),
        sa.Column("drawdown", JSONB, nullable=True),
        sa.Column("signals", JSONB, nullable=True),
        sa.Column("extras", JSONB, nullable=True),
        sa.Column(
            "bytes_written", BIGPK, nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "stored_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["backtest_runs.run_id"],
            name="fk_bt_series_run",
            ondelete="CASCADE",
        ),
    )

    # -- R3 lineage: preset ← backtest run --------------------------------------
    op.add_column(
        "parameter_presets", sa.Column("backtest_run_id", UUID, nullable=True)
    )
    if _is_postgres():
        # SQLite cannot attach a named FK to an existing table via ALTER;
        # create_all (dev/test path) carries it inline on the fresh table.
        op.create_foreign_key(
            "fk_presets_backtest_run",
            "parameter_presets",
            "backtest_runs",
            ["backtest_run_id"],
            ["run_id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    op.drop_column("parameter_presets", "backtest_run_id")
    op.drop_table("backtest_run_series")
    for name in (
        "ix_backtest_runs_compare",
        "ix_backtest_runs_opt",
        "ix_backtest_runs_kind",
        "ix_backtest_runs_config",
        "ix_backtest_runs_list",
    ):
        op.drop_index(name, table_name="backtest_runs")
    op.drop_table("backtest_runs")
    op.drop_index("ix_backtest_compares_created", table_name="backtest_compare_runs")
    op.drop_table("backtest_compare_runs")
