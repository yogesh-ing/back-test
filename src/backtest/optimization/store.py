"""Persistence for optimization runs, results, presets and the audit trail.

Thin repository over the ORM models in :mod:`backtest.db.models`. All public
methods return plain dicts (JSON-ready: Decimals → float, datetimes → ISO
strings) so the service and API never hold live ORM objects across threads.

Numeric safety: every metric is sanitized before it is written — NaN/±inf
become NULL and values are clamped into the column's precision
(``NUMERIC(10,4)`` → ±999 999.9999, ``NUMERIC(10,2)`` → ±99 999 999.99,
``win_rate NUMERIC(5,2)`` → 0..100), so an extreme backtest can never fail
the whole batch insert on PostgreSQL.
"""

from __future__ import annotations

import logging
import math
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Iterable

import numpy as np
from sqlalchemy import delete, func, select, update

from backtest.db.models import (
    OPTIMIZATION_TABLES,
    OptimizationAudit,
    OptimizationResult,
    OptimizationRun,
    ParameterPreset,
)

log = logging.getLogger("backtest.optimization.store")

SCORE_MAX = 999_999.9999
MONEY_MAX = 99_999_999.99

#: optimization_results metric columns and their numeric kind.
RESULT_METRIC_COLUMNS: dict[str, str] = {
    "sharpe": "score", "sortino": "score", "calmar": "score",
    "total_return": "score", "cagr": "score", "max_drawdown": "score",
    "drawdown_duration_days": "int", "profit_factor": "score",
    "win_rate": "pct", "total_trades": "int", "winning_trades": "int",
    "losing_trades": "int", "expectancy": "money", "avg_win": "money",
    "avg_loss": "money", "largest_win": "money", "largest_loss": "money",
    "volatility": "score", "downside_deviation": "score",
    "avg_holding_time_minutes": "int", "avg_slippage": "score",
}

#: Columns the results table may be sorted by (API whitelist).
SORTABLE = ("objective_score", "rank", *RESULT_METRIC_COLUMNS)

#: Default presets seeded by migration 009 (mirrored for create_all installs).
DEFAULT_PRESETS = (
    ("00000000-0000-4000-8000-000000000001", "Conservative",
     "Low risk preset with tight stop-loss and moderate targets",
     {"stop_loss_pct": 10, "target_pct": 15, "position_size_pct": 2, "max_positions": 3}),
    ("00000000-0000-4000-8000-000000000002", "Moderate",
     "Balanced risk/reward preset",
     {"stop_loss_pct": 15, "target_pct": 25, "position_size_pct": 5, "max_positions": 5}),
    ("00000000-0000-4000-8000-000000000003", "Aggressive",
     "Higher risk preset with wider stops and bigger targets",
     {"stop_loss_pct": 20, "target_pct": 40, "position_size_pct": 10, "max_positions": 8}),
)


# ---------------------------------------------------------------------------
# Sanitizers
# ---------------------------------------------------------------------------


def clean_json(value: Any) -> Any:
    """Recursively make ``value`` strict-JSON safe (no NaN/inf, no numpy)."""
    if isinstance(value, dict):
        return {str(k): clean_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [clean_json(v) for v in value]
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    if isinstance(value, (int, np.integer)):
        return int(value)
    if isinstance(value, (float, np.floating, Decimal)):
        f = float(value)
        return f if math.isfinite(f) else None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def clamp(value: Any, kind: str = "score") -> Any:
    """Fit a metric into its column (``None`` for missing / non-finite)."""
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f):
        return None
    if kind == "int":
        return int(max(-2**31, min(2**31 - 1, round(f))))
    if kind == "pct":
        return round(max(0.0, min(100.0, f)), 2)
    if kind == "money":
        return round(max(-MONEY_MAX, min(MONEY_MAX, f)), 2)
    return round(max(-SCORE_MAX, min(SCORE_MAX, f)), 4)


def _score_value(value: Any) -> float:
    """``objective_score`` is NOT NULL: ±inf saturate, NaN/missing sink to the floor."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return -SCORE_MAX
    if math.isnan(f):
        return -SCORE_MAX
    return round(max(-SCORE_MAX, min(SCORE_MAX, f)), 4)


def _out(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    return value


def _row(obj: Any, columns: Iterable[str] | None = None) -> dict[str, Any]:
    cols = columns or [c.key for c in obj.__table__.columns]
    return {c: _out(getattr(obj, c)) for c in cols}


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class OptimizationStore:
    """Repository over a :class:`backtest.db.manager.DatabaseManager`."""

    def __init__(self, db: Any) -> None:
        self.db = db

    # -- schema -------------------------------------------------------------

    def ensure_schema(self, seed: bool = True) -> None:
        """Create missing optimization tables (idempotent) + default presets.

        Production PostgreSQL is migrated by Alembic (005–009); this keeps
        SQLite dev databases and ``:memory:`` test databases usable without a
        migration step. ``checkfirst`` makes it a no-op on a migrated DB.
        """
        from backtest.db.models import Base

        Base.metadata.create_all(self.db.engine, tables=list(OPTIMIZATION_TABLES),
                                 checkfirst=True)
        if seed:
            with self.db.session() as s:
                existing = s.scalar(
                    select(func.count()).select_from(ParameterPreset)
                    .where(ParameterPreset.source == "default")
                )
                if not existing:
                    for pid, name, desc, params in DEFAULT_PRESETS:
                        s.add(ParameterPreset(
                            preset_id=pid, strategy_id="default", name=name,
                            description=desc, params=params, source="default",
                            is_active=True, created_by="system",
                        ))

    # -- runs -----------------------------------------------------------------

    def create_run(self, *, strategy_id: str, objective: str, method: str,
                   param_space: Any, constraints: Any, backtest_config: Any,
                   walk_forward_enabled: bool, walk_forward_config: Any,
                   total_combinations: int, bucket_id: str | None = None,
                   created_by: str | None = None, status: str = "pending",
                   baseline_params: Any = None) -> str:
        run_id = str(uuid.uuid4())
        with self.db.session() as s:
            s.add(OptimizationRun(
                run_id=run_id, strategy_id=strategy_id, bucket_id=bucket_id,
                objective_function=objective, method=method,
                param_space=clean_json(param_space), constraints=clean_json(constraints),
                backtest_config=clean_json(backtest_config), status=status,
                total_combinations=int(total_combinations),
                walk_forward_enabled=bool(walk_forward_enabled),
                walk_forward_config=clean_json(walk_forward_config),
                baseline_params=clean_json(baseline_params),
                created_by=created_by,
            ))
        return run_id

    def touch_run(self, run_id: str) -> None:
        """Heartbeat: bump ``updated_at`` of a live run."""
        with self.db.session() as s:
            s.execute(update(OptimizationRun).where(OptimizationRun.run_id == run_id)
                      .values(updated_at=_now()))

    def update_run(self, run_id: str, **fields: Any) -> None:
        values: dict[str, Any] = {}
        for key, val in fields.items():
            col = OptimizationRun.__table__.columns.get(key)
            if col is None:
                raise KeyError(f"unknown optimization_runs column: {key}")
            type_name = col.type.__class__.__name__
            if key in ("best_score", "baseline_score", "avg_train_score", "avg_test_score"):
                val = clamp(val)
            elif key == "robustness_score":
                val = None if val is None else round(max(0.0, min(10.0, float(val))), 2)
            elif type_name in ("JSONVariant", "JSON", "JSONB") or key in (
                "param_space", "constraints", "backtest_config", "best_params",
                "best_metrics", "baseline_params", "baseline_metrics",
                "walk_forward_config", "walk_forward_results", "analysis",
            ):
                val = clean_json(val)
            values[key] = val
        values["updated_at"] = _now()
        with self.db.session() as s:
            s.execute(update(OptimizationRun).where(OptimizationRun.run_id == run_id)
                      .values(**values))

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self.db.session() as s:
            obj = s.get(OptimizationRun, str(run_id))
            return _row(obj) if obj else None

    _LIST_COLUMNS = (
        "run_id", "strategy_id", "bucket_id", "objective_function", "method", "status",
        "started_at", "completed_at", "error_message", "total_combinations",
        "tested_combinations", "valid_combinations", "best_params", "best_score",
        "baseline_score", "walk_forward_enabled", "overfitted", "avg_train_score",
        "avg_test_score", "robustness_score", "created_by", "created_at", "backtest_config",
    )

    def list_runs(self, *, strategy_id: str | None = None, status: str | None = None,
                  limit: int = 50, offset: int = 0) -> tuple[list[dict], int]:
        with self.db.session() as s:
            q = select(OptimizationRun)
            cq = select(func.count()).select_from(OptimizationRun)
            if strategy_id:
                q = q.where(OptimizationRun.strategy_id == strategy_id)
                cq = cq.where(OptimizationRun.strategy_id == strategy_id)
            if status:
                q = q.where(OptimizationRun.status == status)
                cq = cq.where(OptimizationRun.status == status)
            total = int(s.scalar(cq) or 0)
            rows = s.scalars(q.order_by(OptimizationRun.created_at.desc())
                             .limit(limit).offset(offset)).all()
            return [_row(r, self._LIST_COLUMNS) for r in rows], total

    def delete_run(self, run_id: str) -> bool:
        with self.db.session() as s:
            s.execute(delete(OptimizationResult).where(OptimizationResult.run_id == run_id))
            res = s.execute(delete(OptimizationRun).where(OptimizationRun.run_id == run_id))
            return bool(res.rowcount)

    def fail_stale_runs(self, stale_after_seconds: float = 900.0) -> int:
        """Mark runs abandoned by a dead process as failed.

        Live runs heartbeat ``updated_at`` (see the service), so only rows
        silent for ``stale_after_seconds`` are touched — another worker
        process sharing the database keeps its in-flight runs.
        """
        from datetime import timedelta

        cutoff = _now() - timedelta(seconds=stale_after_seconds)
        with self.db.session() as s:
            res = s.execute(
                update(OptimizationRun)
                .where(OptimizationRun.status.in_(("running", "paused", "pending")))
                .where(OptimizationRun.updated_at < cutoff)
                .values(status="failed", completed_at=_now(), updated_at=_now(),
                        error_message="interrupted: the server restarted while this "
                                      "run was in progress")
            )
            return int(res.rowcount or 0)

    # -- results ----------------------------------------------------------------

    def save_results(self, run_id: str, rows: list[dict[str, Any]],
                     replace: bool = True) -> int:
        """Bulk insert result rows (``params``/``metrics``/``score``/...)."""
        payload = []
        for r in rows:
            m = r.get("metrics") or {}
            rec = {
                "result_id": str(uuid.uuid4()),
                "run_id": run_id,
                "params": clean_json(r["params"]),
                "constraints_met": bool(r.get("constraints_met")),
                "constraint_violations": clean_json(r.get("violations") or None),
                "objective_score": _score_value(r.get("score")),
                "rank": r.get("rank"),
                "full_result": clean_json({
                    "metrics": m,
                    "error": r.get("error"),
                    "elapsed_ms": r.get("elapsed_ms"),
                    "seq": r.get("seq"),
                    "origin": r.get("origin", "search"),
                }),
            }
            for col, kind in RESULT_METRIC_COLUMNS.items():
                rec[col] = clamp(m.get(col), kind)
            payload.append(rec)
        with self.db.session() as s:
            if replace:
                s.execute(delete(OptimizationResult).where(OptimizationResult.run_id == run_id))
            for start in range(0, len(payload), 1000):
                s.execute(OptimizationResult.__table__.insert(), payload[start:start + 1000])
        return len(payload)

    def get_results(self, run_id: str, *, sort: str = "objective_score",
                    order: str = "desc", limit: int = 100, offset: int = 0,
                    compliant_only: bool = False) -> tuple[list[dict], int]:
        if sort not in SORTABLE:
            sort = "objective_score"
        col = getattr(OptimizationResult, sort)
        ordering = col.asc() if order == "asc" else col.desc()
        with self.db.session() as s:
            q = select(OptimizationResult).where(OptimizationResult.run_id == run_id)
            cq = select(func.count()).select_from(OptimizationResult) \
                .where(OptimizationResult.run_id == run_id)
            if compliant_only:
                q = q.where(OptimizationResult.constraints_met.is_(True))
                cq = cq.where(OptimizationResult.constraints_met.is_(True))
            total = int(s.scalar(cq) or 0)
            objs = s.scalars(q.order_by(ordering.nulls_last(),
                                        OptimizationResult.result_id)
                             .limit(limit).offset(offset)).all()
            out = []
            for o in objs:
                d = _row(o)
                d.pop("full_result", None)
                d["error"] = (o.full_result or {}).get("error")
                d["origin"] = (o.full_result or {}).get("origin", "search")
                out.append(d)
            return out, total

    def analysis_rows(self, run_id: str) -> list[dict[str, Any]]:
        """Every result as ``{params, metrics, score, constraints_met}``."""
        cols = ["params", "objective_score", "constraints_met", *RESULT_METRIC_COLUMNS]
        with self.db.session() as s:
            res = s.execute(
                select(*[getattr(OptimizationResult, c) for c in cols])
                .where(OptimizationResult.run_id == run_id)
            ).all()
        rows = []
        for rec in res:
            d = dict(zip(cols, rec))
            metrics = {c: _out(d[c]) for c in RESULT_METRIC_COLUMNS}
            rows.append({"params": d["params"], "score": _out(d["objective_score"]),
                         "constraints_met": bool(d["constraints_met"]), "metrics": metrics})
        return rows

    # -- presets --------------------------------------------------------------

    def list_presets(self, strategy_id: str | None = None, *, include_defaults: bool = True,
                     active_only: bool = True) -> list[dict]:
        with self.db.session() as s:
            q = select(ParameterPreset)
            if strategy_id:
                ids = [strategy_id, "default"] if include_defaults else [strategy_id]
                q = q.where(ParameterPreset.strategy_id.in_(ids))
            if active_only:
                q = q.where(ParameterPreset.is_active.is_(True))
            objs = s.scalars(q.order_by(ParameterPreset.strategy_id,
                                        ParameterPreset.created_at.desc())).all()
            return [_row(o) for o in objs]

    def get_preset(self, preset_id: str) -> dict | None:
        with self.db.session() as s:
            o = s.get(ParameterPreset, str(preset_id))
            return _row(o) if o else None

    def create_preset(self, *, strategy_id: str, name: str, params: dict,
                      source: str = "manual", description: str | None = None,
                      optimization_run_id: str | None = None,
                      backtest_metrics: dict | None = None,
                      created_by: str | None = None) -> dict:
        """Insert a preset; a clashing (strategy, name) gets a numeric suffix."""
        with self.db.session() as s:
            base, final, n = name.strip()[:90] or "Preset", None, 1
            candidate = base
            while final is None:
                clash = s.scalar(select(func.count()).select_from(ParameterPreset).where(
                    ParameterPreset.strategy_id == strategy_id,
                    ParameterPreset.name == candidate))
                if clash:
                    n += 1
                    candidate = f"{base} ({n})"
                else:
                    final = candidate
            obj = ParameterPreset(
                strategy_id=strategy_id, name=final, description=description,
                params=clean_json(params), source=source,
                optimization_run_id=optimization_run_id,
                backtest_metrics=clean_json(backtest_metrics), is_active=True,
                created_by=created_by,
            )
            s.add(obj)
            s.flush()
            return _row(obj)

    def update_preset(self, preset_id: str, **fields: Any) -> dict | None:
        allowed = {"name", "description", "is_active", "params"}
        values = {k: (clean_json(v) if k == "params" else v)
                  for k, v in fields.items() if k in allowed}
        with self.db.session() as s:
            o = s.get(ParameterPreset, str(preset_id))
            if o is None:
                return None
            for k, v in values.items():
                setattr(o, k, v)
            o.updated_at = _now()
            s.flush()
            return _row(o)

    def mark_preset_applied(self, preset_id: str) -> None:
        with self.db.session() as s:
            s.execute(update(ParameterPreset).where(ParameterPreset.preset_id == preset_id)
                      .values(applied_count=ParameterPreset.applied_count + 1,
                              last_applied_at=_now(), updated_at=_now()))

    def delete_preset(self, preset_id: str) -> bool:
        with self.db.session() as s:
            res = s.execute(delete(ParameterPreset)
                            .where(ParameterPreset.preset_id == preset_id))
            return bool(res.rowcount)

    # -- audit ----------------------------------------------------------------

    def add_audit(self, **fields: Any) -> dict:
        json_cols = ("action_details", "old_params", "new_params", "params_diff",
                     "expected_impact", "actual_impact")
        values = {k: (clean_json(v) if k in json_cols else v) for k, v in fields.items()}
        with self.db.session() as s:
            obj = OptimizationAudit(**values)
            s.add(obj)
            s.flush()
            return _row(obj)

    def get_audit(self, audit_id: str) -> dict | None:
        with self.db.session() as s:
            o = s.get(OptimizationAudit, str(audit_id))
            return _row(o) if o else None

    def list_audit(self, *, strategy_id: str | None = None, run_id: str | None = None,
                   limit: int = 100) -> list[dict]:
        with self.db.session() as s:
            q = select(OptimizationAudit)
            if strategy_id:
                q = q.where(OptimizationAudit.strategy_id == strategy_id)
            if run_id:
                q = q.where(OptimizationAudit.run_id == run_id)
            objs = s.scalars(q.order_by(OptimizationAudit.timestamp.desc()).limit(limit)).all()
            return [_row(o) for o in objs]

    def update_audit(self, audit_id: str, **fields: Any) -> None:
        with self.db.session() as s:
            o = s.get(OptimizationAudit, str(audit_id))
            if o is None:
                return
            for k, v in fields.items():
                setattr(o, k, clean_json(v) if isinstance(v, (dict, list)) else v)
