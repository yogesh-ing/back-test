"""Walk-forward validation: optimize on train windows, test on the next window.

Split geometry is calendar based (the PRD's ``trainPeriodDays`` /
``testPeriodDays`` / ``stepDays``)::

    |---- train 1 ----|-- test 1 --|
         step |---- train 2 ----|-- test 2 --|
                   step |---- train 3 ----|-- test 3 --|

Each split re-runs the configured search method on its train window (budget
``max_evals_per_split``), picks the best constraint-compliant set, and runs
that set on the unseen test window. Bars before a window are used only for
indicator warm-up (they are in the past, so there is no lookahead) and the
first measured bar is forced flat.

Constraints on a window: ``min_trades`` is scaled by window length /
full-period length (a 30-trades-per-year floor cannot apply unchanged to a
60-day window); every other constraint applies as configured.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from statistics import mean, pstdev
from typing import Any, Callable

import pandas as pd

from backtest.optimization.config import Constraint, OptimizationConfig
from backtest.optimization.evaluator import EvalWindow, Evaluator
from backtest.optimization.methods import SearchSpace, run_method
from backtest.optimization.scoring import (
    FAILED_SCORE,
    RATIO_OBJECTIVES,
    check_constraints,
    objective_score,
)

_FMT = "%Y-%m-%d"


@dataclass(frozen=True)
class Split:
    index: int
    train_start: str
    train_end: str
    test_start: str
    test_end: str

    def to_dict(self) -> dict[str, Any]:
        return {"split": self.index, "train_start": self.train_start,
                "train_end": self.train_end, "test_start": self.test_start,
                "test_end": self.test_end}


def make_splits(start: str, end: str, train_days: int, test_days: int,
                step_days: int) -> list[Split]:
    """Rolling calendar splits that fit entirely inside ``[start, end]``."""
    s = datetime.strptime(start, _FMT)
    e = datetime.strptime(end, _FMT)
    out: list[Split] = []
    k = 0
    while True:
        tr_s = s + timedelta(days=k * step_days)
        tr_e = tr_s + timedelta(days=train_days - 1)
        te_s = tr_e + timedelta(days=1)
        te_e = te_s + timedelta(days=test_days - 1)
        if te_e > e:
            break
        out.append(Split(k + 1, tr_s.strftime(_FMT), tr_e.strftime(_FMT),
                         te_s.strftime(_FMT), te_e.strftime(_FMT)))
        k += 1
        if k > 500:  # pragma: no cover - guarded by config validation
            break
    return out


def warmup_bars(cfg: OptimizationConfig) -> int:
    """Indicator warm-up heuristic: the largest integer param value (≤ 300).

    Integer params of the bundled strategies are look-back lengths (SMA
    periods, RSI length, breakout window); floats are thresholds.
    """
    biggest = 0
    for p in cfg.parameters:
        if p.type == "int" and not p.name.startswith("engine."):
            biggest = max(biggest, int(p.max if p.optimize else p.current))
    return int(min(300, max(20, biggest + 5)))


def warmup_start(index: pd.DatetimeIndex, start: str, bars: int) -> str | None:
    pos = int(index.searchsorted(pd.Timestamp(start)))
    if pos <= 0:
        return None
    return index[max(0, pos - bars)].strftime(_FMT)


def scaled_constraints(constraints: tuple[Constraint, ...], window_days: int,
                       total_days: int) -> tuple[Constraint, ...]:
    ratio = window_days / max(total_days, 1)
    out = []
    for c in constraints:
        if c.metric == "min_trades" and c.operator in (">", ">="):
            out.append(replace(c, value=float(max(1, math.ceil(c.value * ratio)))))
        else:
            out.append(c)
    return tuple(out)


def _chain_curves(curves: list[list[list[Any]]], capital: float) -> list[list[Any]]:
    """Stitch per-split OOS curves into one compounded out-of-sample curve."""
    out: list[list[Any]] = []
    level = capital
    for curve in curves:
        if not curve:
            continue
        base = float(curve[0][1]) or 1.0
        for d, v in curve:
            out.append([d, round(level * float(v) / base, 2)])
        level = out[-1][1]
    return out


def run_walk_forward(
    cfg: OptimizationConfig,
    evaluator: Evaluator,
    *,
    batch_size: int = 8,
    on_split: Callable[[dict[str, Any], int, int], None] | None = None,
) -> dict[str, Any]:
    """Run every split; return the aggregate report stored in ``walk_forward_results``."""
    wf = cfg.walk_forward
    bt = cfg.backtest
    splits = make_splits(bt.start_date, bt.end_date, wf.train_period_days,
                         wf.test_period_days, wf.step_days)
    space = SearchSpace.from_config(cfg)
    index = evaluator.candles.index
    bars = warmup_bars(cfg)
    total_days = (datetime.strptime(bt.end_date, _FMT)
                  - datetime.strptime(bt.start_date, _FMT)).days + 1
    train_constraints = scaled_constraints(cfg.constraints, wf.train_period_days, total_days)
    rows: list[dict[str, Any]] = []
    curves: list[list[list[Any]]] = []

    for n, split in enumerate(splits, start=1):
        train_w = EvalWindow(split.train_start, split.train_end,
                             warmup_start(index, split.train_start, bars))
        test_w = EvalWindow(split.test_start, split.test_end,
                            warmup_start(index, split.test_start, bars))
        seen: list[dict[str, Any]] = []

        def evaluate(points: list[dict[str, Any]], _w=train_w, _seen=seen):
            payloads = evaluator.evaluate_batch(points, _w)
            out = []
            for p in payloads:
                score = objective_score(p["metrics"], cfg.objective) if not p["error"] \
                    else FAILED_SCORE
                ok = not p["error"] and not check_constraints(p["metrics"], train_constraints)
                _seen.append({"params": p["params"], "score": score, "ok": ok,
                              "metrics": p["metrics"]})
                out.append((p["params"], score if ok else FAILED_SCORE))
            return out

        run_method(cfg, space, evaluate, budget=wf.max_evals_per_split, batch_size=batch_size)
        compliant = [r for r in seen if r["ok"]]
        pool = compliant or [r for r in seen if r["score"] > FAILED_SCORE / 2]
        if not pool:
            row = {**split.to_dict(), "params": None, "train_score": None,
                   "test_score": None, "degradation": None, "evaluations": len(seen),
                   "note": "no valid parameter set on the train window"}
            rows.append(row)
            if on_split:
                on_split(row, n, len(splits))
            continue
        best = max(pool, key=lambda r: r["score"])
        test = evaluator.evaluate_batch([best["params"]], test_w, keep_curve=True)[0]
        test_score = objective_score(test["metrics"], cfg.objective) if not test["error"] \
            else None
        curves.append(test.get("curve") or [])
        row = {
            **split.to_dict(),
            "params": best["params"],
            "train_score": round(best["score"], 4),
            "test_score": None if test_score is None else round(test_score, 4),
            "degradation": None if test_score is None else round(best["score"] - test_score, 4),
            "train_trades": int(best["metrics"].get("total_trades") or 0),
            "test_trades": int(test["metrics"].get("total_trades") or 0),
            "test_return": test["metrics"].get("total_return"),
            "test_max_drawdown": test["metrics"].get("max_drawdown"),
            "train_constraints_met": bool(best["ok"]),
            "evaluations": len(seen),
        }
        rows.append(row)
        if on_split:
            on_split(row, n, len(splits))

    return summarize(cfg, rows, _chain_curves(curves, bt.initial_capital), bars)


def summarize(cfg: OptimizationConfig, rows: list[dict[str, Any]],
              oos_curve: list[list[Any]] | None = None, bars: int = 0) -> dict[str, Any]:
    wf = cfg.walk_forward
    scored = [r for r in rows if r.get("test_score") is not None]
    avg_train = mean(r["train_score"] for r in scored) if scored else None
    avg_test = mean(r["test_score"] for r in scored) if scored else None
    avg_deg = mean(r["degradation"] for r in scored) if scored else None
    efficiency = None
    if avg_train is not None and avg_train > 0:
        efficiency = round(avg_test / avg_train, 3)
    positive = sum(1 for r in scored if r["test_score"] > 0)
    ratio = cfg.objective in RATIO_OBJECTIVES
    if not scored:
        overfitted = None
        verdict = "Walk-forward produced no scored splits — widen the windows or relax " \
                  "constraints."
    elif ratio:
        overfitted = avg_deg > wf.overfit_threshold
        verdict = (
            f"Average train→test degradation {avg_deg:.2f} "
            f"{'exceeds' if overfitted else 'is within'} the {wf.overfit_threshold:.2f} "
            f"threshold{' — likely overfitted' if overfitted else ''}."
        )
    else:
        overfitted = efficiency is None or efficiency < 0.5
        eff_text = "n/a" if efficiency is None else f"{efficiency:.2f}"
        verdict = (
            f"Walk-forward efficiency {eff_text} "
            f"{'is below 0.5 — likely overfitted' if overfitted else 'is healthy (≥ 0.5)'}."
        )
    # per-param stability across splits
    stability: dict[str, dict[str, Any]] = {}
    for spec in cfg.optimized:
        vals = [float(r["params"][spec.name]) for r in rows
                if r.get("params") and spec.name in r["params"]]
        if not vals:
            continue
        mu = mean(vals)
        sd = pstdev(vals) if len(vals) > 1 else 0.0
        stability[spec.name] = {
            "values": vals,
            "mean": round(mu, 4),
            "std": round(sd, 4),
            "cv": round(sd / abs(mu), 3) if mu else None,
            "range_fraction": round(sd / (spec.max - spec.min), 3) if spec.max > spec.min
            else 0.0,
        }
    return {
        "enabled": True,
        "splits": rows,
        "n_splits": len(rows),
        "n_scored": len(scored),
        "avg_train_score": None if avg_train is None else round(avg_train, 4),
        "avg_test_score": None if avg_test is None else round(avg_test, 4),
        "avg_degradation": None if avg_deg is None else round(avg_deg, 4),
        "efficiency": efficiency,
        "positive_test_splits": positive,
        "overfitted": overfitted,
        "overfit_rule": ("avg_degradation > threshold" if ratio else "efficiency < 0.5"),
        "threshold": wf.overfit_threshold,
        "verdict": verdict,
        "param_stability": stability,
        "oos_curve": oos_curve or [],
        "warmup_bars": bars,
        "settings": wf.to_dict(),
    }
