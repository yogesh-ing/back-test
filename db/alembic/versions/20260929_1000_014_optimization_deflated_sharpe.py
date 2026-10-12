"""optimization: deflated sharpe column (PRD Part 2 §3)

Adds ``optimization_runs.deflated_sharpe`` beside ``robustness_score`` and
recreates the three reporting views so they carry the new column.

The value stored is SR₀ — the Sharpe the best-of-N had to beat — on the same
scale as ``best_metrics.sharpe``, which is what makes it sortable and
comparable in a list. The full statistic (including the DSR probability and
the trial count) lives in ``analysis->'deflated_sharpe'``.

Numeric(6, 3) rather than Numeric(4, 2) like ``robustness_score``: the bar
rises with the size of the search, so a wide search on a narrow distribution
produces values well past 99.99. Clamping into four digits would be wrong
rather than merely lossy.

Revision ID: 014
Revises: 013
Create Date: 2026-09-29
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "014"
down_revision: Union[str, None] = "013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_COLUMN = sa.Column("deflated_sharpe", sa.Numeric(precision=6, scale=3), nullable=True)


def upgrade() -> None:
    op.add_column("optimization_runs", _COLUMN)

    # The views are recreated, not altered: CREATE OR REPLACE VIEW cannot
    # change a column list, and these SELECT *-ish projections name columns
    # explicitly. Mirrors 012's definitions plus the new column.
    op.execute(
        """
        CREATE OR REPLACE VIEW v_latest_optimization AS
        SELECT DISTINCT ON (strategy_id)
            run_id, strategy_id, bucket_id, objective_function, method, status,
            best_score, best_params, baseline_score, overfitted, robustness_score,
            deflated_sharpe, completed_at, created_by
        FROM optimization_runs
        WHERE status = 'completed'
        ORDER BY strategy_id, completed_at DESC
        """
    )
    op.execute(
        """
        CREATE OR REPLACE VIEW v_optimization_summary AS
        SELECT
            o.run_id, o.strategy_id, o.objective_function, o.method, o.status,
            o.total_combinations, o.valid_combinations, o.best_score, o.baseline_score,
            o.overfitted, o.robustness_score, o.deflated_sharpe,
            COUNT(r.result_id)          AS results_count,
            AVG(r.objective_score)      AS avg_score,
            STDDEV(r.objective_score)   AS score_stddev,
            MIN(r.objective_score)      AS min_score,
            MAX(r.objective_score)      AS max_score,
            EXTRACT(EPOCH FROM (o.completed_at - o.started_at)) / 60 AS runtime_minutes
        FROM optimization_runs o
        LEFT JOIN optimization_results r ON o.run_id = r.run_id
        WHERE o.status = 'completed'
        GROUP BY o.run_id
        """
    )


def downgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE VIEW v_optimization_summary AS
        SELECT
            o.run_id, o.strategy_id, o.objective_function, o.method, o.status,
            o.total_combinations, o.valid_combinations, o.best_score, o.baseline_score,
            o.overfitted, o.robustness_score,
            COUNT(r.result_id)          AS results_count,
            AVG(r.objective_score)      AS avg_score,
            STDDEV(r.objective_score)   AS score_stddev,
            MIN(r.objective_score)      AS min_score,
            MAX(r.objective_score)      AS max_score,
            EXTRACT(EPOCH FROM (o.completed_at - o.started_at)) / 60 AS runtime_minutes
        FROM optimization_runs o
        LEFT JOIN optimization_results r ON o.run_id = r.run_id
        WHERE o.status = 'completed'
        GROUP BY o.run_id
        """
    )
    op.execute(
        """
        CREATE OR REPLACE VIEW v_latest_optimization AS
        SELECT DISTINCT ON (strategy_id)
            run_id, strategy_id, bucket_id, objective_function, method, status,
            best_score, best_params, baseline_score, overfitted, robustness_score,
            completed_at, created_by
        FROM optimization_runs
        WHERE status = 'completed'
        ORDER BY strategy_id, completed_at DESC
        """
    )
    op.drop_column("optimization_runs", "deflated_sharpe")
