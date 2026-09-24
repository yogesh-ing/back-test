"""parameter optimization engine: indexes

All secondary indexes for the optimization tables (PRD "Migration 003"),
plus ``idx_audit_run`` for the per-run audit view. Partial indexes carry a
``sqlite_where`` twin so the SQLite dev path matches; the GIN index on
``optimization_results.params`` (JSONB containment search, e.g.
``params @> '{"fast": 10}'``) is PostgreSQL-only. The default ``jsonb_ops``
operator class needs no extension (``btree_gin`` is not required).

Hand-applied equivalent: ``db/migrations/007_optimization_indexes.sql``.

Revision ID: 007
Revises: 006
Create Date: 2026-09-24
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "007"
down_revision: Union[str, None] = "006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: (name, table, columns, kwargs) — creation order; dropped in reverse.
_INDEXES: list[tuple[str, str, list[str], dict]] = [
    ("idx_opt_runs_strategy", "optimization_runs", ["strategy_id"], {}),
    ("idx_opt_runs_status", "optimization_runs", ["status"], {}),
    ("idx_opt_runs_created", "optimization_runs", ["created_at"], {}),
    ("idx_opt_runs_strategy_status", "optimization_runs", ["strategy_id", "status"], {}),
    ("idx_opt_runs_bucket", "optimization_runs", ["bucket_id"], {}),
    ("idx_opt_results_run", "optimization_results", ["run_id"], {}),
    (
        "idx_opt_results_rank",
        "optimization_results",
        ["run_id", "rank"],
        {
            "postgresql_where": sa.text("rank IS NOT NULL"),
            "sqlite_where": sa.text("rank IS NOT NULL"),
        },
    ),
    ("idx_opt_results_score", "optimization_results", ["run_id", "objective_score"], {}),
    ("idx_opt_results_constraints", "optimization_results", ["run_id", "constraints_met"], {}),
    ("idx_presets_strategy", "parameter_presets", ["strategy_id"], {}),
    (
        "idx_presets_active",
        "parameter_presets",
        ["strategy_id", "is_active"],
        {
            "postgresql_where": sa.text("is_active = true"),
            "sqlite_where": sa.text("is_active = 1"),
        },
    ),
    ("idx_presets_source", "parameter_presets", ["source"], {}),
    ("idx_audit_strategy", "optimization_audit", ["strategy_id"], {}),
    ("idx_audit_timestamp", "optimization_audit", ["timestamp"], {}),
    ("idx_audit_action", "optimization_audit", ["action"], {}),
    ("idx_audit_user", "optimization_audit", ["user_id"], {}),
    ("idx_audit_bucket", "optimization_audit", ["applied_to_bucket"], {}),
    ("idx_audit_run", "optimization_audit", ["run_id"], {}),
]


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    for name, table, columns, kwargs in _INDEXES:
        op.create_index(name, table, columns, **kwargs)
    if _is_postgres():
        op.create_index(
            "idx_opt_results_params_gin",
            "optimization_results",
            ["params"],
            postgresql_using="gin",
        )


def downgrade() -> None:
    if _is_postgres():
        op.drop_index("idx_opt_results_params_gin", table_name="optimization_results")
    for name, table, _columns, _kwargs in reversed(_INDEXES):
        op.drop_index(name, table_name=table)
