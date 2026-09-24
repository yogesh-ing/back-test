"""parameter optimization engine: seed default presets

Three strategy-agnostic templates (``strategy_id = 'default'``):
Conservative / Moderate / Aggressive, from the PRD "Migration 005".

Fixes vs the PRD draft: the draft passed ``json.dumps(...)`` strings into a
JSONB-typed ``bulk_insert`` column, which serialises a second time and
stores a JSON *string* (``"{\\"stop_loss_pct\\": 10}"``) instead of an
object. Dicts are passed here. Primary keys are supplied explicitly so the
revision also runs on SQLite (no ``gen_random_uuid()`` there).

Hand-applied equivalent: ``db/migrations/009_optimization_seed_presets.sql``.

Revision ID: 009
Revises: 008
Create Date: 2026-09-24
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql import column, table

revision: str = "009"
down_revision: Union[str, None] = "008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

JSONB = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
UUID = sa.String(36).with_variant(postgresql.UUID(as_uuid=False), "postgresql")

#: Fixed ids keep the seed idempotent and identical across both paths.
DEFAULT_PRESETS = [
    {
        "preset_id": "00000000-0000-4000-8000-000000000001",
        "strategy_id": "default",
        "name": "Conservative",
        "description": "Low risk preset with tight stop-loss and moderate targets",
        "params": {"stop_loss_pct": 10, "target_pct": 15, "position_size_pct": 2,
                   "max_positions": 3},
        "source": "default",
        "is_active": True,
        "created_by": "system",
    },
    {
        "preset_id": "00000000-0000-4000-8000-000000000002",
        "strategy_id": "default",
        "name": "Moderate",
        "description": "Balanced risk/reward preset",
        "params": {"stop_loss_pct": 15, "target_pct": 25, "position_size_pct": 5,
                   "max_positions": 5},
        "source": "default",
        "is_active": True,
        "created_by": "system",
    },
    {
        "preset_id": "00000000-0000-4000-8000-000000000003",
        "strategy_id": "default",
        "name": "Aggressive",
        "description": "Higher risk preset with wider stops and bigger targets",
        "params": {"stop_loss_pct": 20, "target_pct": 40, "position_size_pct": 10,
                   "max_positions": 8},
        "source": "default",
        "is_active": True,
        "created_by": "system",
    },
]


def upgrade() -> None:
    presets = table(
        "parameter_presets",
        column("preset_id", UUID),
        column("strategy_id", sa.String),
        column("name", sa.String),
        column("description", sa.Text),
        column("params", JSONB),
        column("source", sa.String),
        column("is_active", sa.Boolean),
        column("created_by", sa.String),
    )
    bind = op.get_bind()
    existing = {
        row[0]
        for row in bind.execute(
            sa.text("SELECT name FROM parameter_presets WHERE strategy_id = 'default'")
        )
    }
    rows = [p for p in DEFAULT_PRESETS if p["name"] not in existing]
    if rows:
        op.bulk_insert(presets, rows)


def downgrade() -> None:
    op.execute(
        "DELETE FROM parameter_presets WHERE source = 'default' AND created_by = 'system'"
    )
