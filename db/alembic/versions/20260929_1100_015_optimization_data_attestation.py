"""optimization: data attestation columns (PRD Part 2 §2)

Adds the data-provenance block to ``optimization_runs`` so a run carries a
record of *what it actually ran on* — which source, how many bars, over what
range, fetched when — instead of leaving that to be inferred from whatever the
app happened to be started with.

Strictly additive: nullable columns, no defaults, no backfill. A row written
before this migration has no attestation, and that is the honest state of it —
a guessed value would be a claim nobody checked. The UI reads the missing
record as "not recorded" rather than inventing one.

``data_attestation`` holds the whole JSON record (including staleness and the
synthetic acknowledgement); the flat columns beside it are there so runs can
be listed and filtered by source without unpacking JSON in every query. Both
are written together by ``attestation_columns()``, which is the only thing
allowed to know the split.

``date_from``/``date_to`` are what the candles *covered*, not what was
requested. A symbol with gaps reports a narrower range than was asked for, and
that gap is the thing an operator needs to see.

Revision ID: 015
Revises: 014
Create Date: 2026-09-29
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "015"
down_revision: Union[str, None] = "014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_FLAT = (
    sa.Column("data_source", sa.String(30), nullable=True),
    sa.Column("data_fetch_date", sa.Date, nullable=True),
    sa.Column("bars_count", sa.Integer, nullable=True),
    sa.Column("symbol", sa.String(30), nullable=True),
    sa.Column("timeframe", sa.String(10), nullable=True),
    sa.Column("date_from", sa.Date, nullable=True),
    sa.Column("date_to", sa.Date, nullable=True),
)

#: JSON column, dialect-portable via the project's JSONVariant equivalent.
_ATTESTATION = sa.Column("data_attestation", sa.JSON, nullable=True)


def upgrade() -> None:
    for column in _FLAT:
        op.add_column("optimization_runs", column)
    op.add_column("optimization_runs", _ATTESTATION)

    # Partial index: only completed runs are ever listed by source, and the
    # runs that matter are overwhelmingly real-data. Kept partial so the index
    # stays small on a table that accumulates one row per search.
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_optimization_runs_data_source
        ON optimization_runs (data_source)
        WHERE status = 'completed'
        """
    )

    # The three reporting views are recreated rather than altered: CREATE OR
    # REPLACE VIEW cannot change a column list.
    op.execute(
        """
        CREATE OR REPLACE VIEW v_latest_optimization AS
        SELECT DISTINCT ON (strategy_id)
            run_id, strategy_id, bucket_id, objective_function, method, status,
            best_score, best_params, baseline_score, overfitted, robustness_score,
            deflated_sharpe, data_source, symbol, timeframe, bars_count,
            completed_at, created_by
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
            o.data_source, o.symbol, o.timeframe, o.bars_count,
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
    op.execute("DROP INDEX IF EXISTS ix_optimization_runs_data_source")
    op.drop_column("optimization_runs", "data_attestation")
    for column in reversed(_FLAT):
        op.drop_column("optimization_runs", column.name)
