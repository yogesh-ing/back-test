"""Objective functions and constraint checks over standardized metrics.

Every function here reads the *standardized* metrics dict produced by
:func:`backtest.optimization.evaluator.standardize_metrics` (same keys and
units as ``optimization_results``): ``total_return``/``max_drawdown`` are
decimal fractions (drawdown negative), ``win_rate`` is a percentage and
``total_trades`` counts round trips.
"""

from __future__ import annotations

import math
from typing import Any, Iterable

from backtest.optimization.config import Constraint

#: Objectives measured in "Sharpe-like" units, where the PRD's absolute
#: degradation threshold (0.3) is meaningful for walk-forward.
RATIO_OBJECTIVES = frozenset({"sharpe", "sortino", "calmar", "profit_factor"})

OBJECTIVE_LABELS = {
    "sharpe": "Sharpe Ratio",
    "sortino": "Sortino Ratio",
    "calmar": "Calmar Ratio",
    "total_return": "Total Return",
    "profit_factor": "Profit Factor",
    "expectancy": "Expectancy",
}

#: Score assigned to a failed backtest so it always ranks last.
FAILED_SCORE = -1e6


def _finite(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def objective_score(metrics: dict[str, Any], objective: str) -> float:
    """Value of ``objective`` for one backtest (higher is better)."""
    if not metrics or metrics.get("error"):
        return FAILED_SCORE
    return _finite(metrics.get(objective), 0.0)


def _metric_for(constraint: Constraint, metrics: dict[str, Any]) -> float:
    if constraint.metric == "max_drawdown":
        # percent magnitude of the (negative) drawdown: -0.083 -> 8.3
        return abs(_finite(metrics.get("max_drawdown"))) * 100.0
    if constraint.metric == "min_trades":
        return _finite(metrics.get("total_trades"))
    if constraint.metric == "win_rate":
        return _finite(metrics.get("win_rate"))
    if constraint.metric == "total_return":
        # constraint values are in percent for returns (e.g. > 5 = +5 %)
        return _finite(metrics.get("total_return")) * 100.0
    return _finite(metrics.get(constraint.metric))


_OPS = {
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
}


def check_constraints(
    metrics: dict[str, Any], constraints: Iterable[Constraint]
) -> list[dict[str, Any]]:
    """Return the violated constraints (empty list = compliant).

    A failed backtest violates everything (it has no trustworthy metrics).
    """
    violations: list[dict[str, Any]] = []
    for c in constraints:
        if not metrics or metrics.get("error"):
            actual = None
            ok = False
        else:
            actual = _metric_for(c, metrics)
            ok = _OPS[c.operator](actual, c.value)
        if not ok:
            violations.append(
                {
                    "metric": c.metric,
                    "operator": c.operator,
                    "limit": c.value,
                    "actual": None if actual is None else round(actual, 4),
                }
            )
    return violations


def compliance_report(
    metrics: dict[str, Any], constraints: Iterable[Constraint]
) -> list[dict[str, Any]]:
    """Every constraint with its actual value and a pass/fail verdict."""
    out = []
    for c in constraints:
        actual = None if (not metrics or metrics.get("error")) else _metric_for(c, metrics)
        ok = actual is not None and _OPS[c.operator](actual, c.value)
        out.append(
            {
                "metric": c.metric,
                "operator": c.operator,
                "limit": c.value,
                "actual": None if actual is None else round(actual, 4),
                "passed": bool(ok),
            }
        )
    return out


def violation_label(violations: list[dict[str, Any]]) -> str:
    """Short UI tag such as ``DD`` / ``Trades`` for the progress table."""
    names = {"max_drawdown": "DD", "min_trades": "Trades", "win_rate": "WinRate",
             "sharpe": "Sharpe", "profit_factor": "PF", "total_return": "Return"}
    return ", ".join(names.get(v["metric"], v["metric"]) for v in violations)
