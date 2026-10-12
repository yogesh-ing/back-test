"""Post-search analysis: heatmaps, sensitivity, robustness, warning signs.

All functions are pure and work on *result rows* — dicts shaped like
``{"params": {...}, "metrics": {...}, "score": float, "constraints_met": bool}``
(what the service keeps in memory and what ``optimization_results`` stores).
"""

from __future__ import annotations

import math
from collections import defaultdict
from statistics import mean, pstdev
from typing import Any, Iterable, Sequence

from backtest.optimization.config import OptimizationConfig, ParameterSpec
from backtest.optimization.scoring import FAILED_SCORE, RATIO_OBJECTIVES

HEATMAP_AGGS = ("max", "mean", "slice")

#: A value is on the "plateau" when its score is within this fraction of the
#: best score (PRD: prefer a stable plateau over a sharp peak).
PLATEAU_TOLERANCE = 0.10


def _valid(rows: Iterable[dict]) -> list[dict]:
    return [r for r in rows if r.get("score") is not None and r["score"] > FAILED_SCORE / 2]


def _close(a: Any, b: Any) -> bool:
    try:
        return math.isclose(float(a), float(b), rel_tol=1e-9, abs_tol=1e-9)
    except (TypeError, ValueError):
        return a == b


# ---------------------------------------------------------------------------
# Heatmap
# ---------------------------------------------------------------------------


def heatmap(
    rows: Sequence[dict],
    x_param: str,
    y_param: str,
    *,
    metric: str = "score",
    agg: str = "max",
    anchor: dict[str, Any] | None = None,
    compliant_only: bool = False,
) -> dict[str, Any]:
    """2-D surface of ``metric`` over ``(x_param, y_param)``.

    ``agg``: ``max`` — best value over the other params (the optimizer's
    view); ``mean`` — average over the other params (robustness view);
    ``slice`` — other params pinned to ``anchor`` (default: the best row).
    Missing cells are ``None`` (random/Bayesian runs are sparse).
    """
    if agg not in HEATMAP_AGGS:
        raise ValueError(f"agg must be one of {HEATMAP_AGGS}")
    data = _valid(rows)
    if compliant_only:
        data = [r for r in data if r.get("constraints_met")]
    data = [r for r in data if x_param in r["params"] and y_param in r["params"]]
    if agg == "slice" and data:
        anchor = anchor or max(data, key=lambda r: r["score"])["params"]
        others = [k for k in anchor if k not in (x_param, y_param)]
        data = [r for r in data if all(_close(r["params"].get(k), anchor[k]) for k in others)]

    def value_of(r: dict) -> float | None:
        if metric == "score":
            return float(r["score"])
        v = r.get("metrics", {}).get(metric)
        return None if v is None else float(v)

    cells: dict[tuple, list[float]] = defaultdict(list)
    for r in data:
        v = value_of(r)
        if v is None or not math.isfinite(v):
            continue
        cells[(r["params"][x_param], r["params"][y_param])].append(v)
    xs = sorted({k[0] for k in cells})
    ys = sorted({k[1] for k in cells})
    z: list[list[float | None]] = []
    best = None
    for yv in ys:
        row: list[float | None] = []
        for xv in xs:
            vals = cells.get((xv, yv))
            if not vals:
                row.append(None)
                continue
            cell = max(vals) if agg in ("max", "slice") else mean(vals)
            cell = round(cell, 4)
            row.append(cell)
            if best is None or cell > best["value"]:
                best = {"x": xv, "y": yv, "value": cell}
        z.append(row)
    filled = sum(1 for row in z for c in row if c is not None)
    return {
        "x_param": x_param,
        "y_param": y_param,
        "metric": metric,
        "agg": agg,
        "x_values": xs,
        "y_values": ys,
        "z": z,
        "best": best,
        "coverage": round(filled / (len(xs) * len(ys)), 3) if xs and ys else 0.0,
        "anchor": anchor if agg == "slice" else None,
    }


def heatmap_pairs(specs: Sequence[ParameterSpec], limit: int = 6) -> list[tuple[str, str]]:
    """Parameter pairs worth plotting (all pairs of optimized params, capped)."""
    names = [s.name for s in specs if s.optimize and s.size() > 1]
    pairs = [(names[i], names[j]) for i in range(len(names)) for j in range(i + 1, len(names))]
    return pairs[:limit]


# ---------------------------------------------------------------------------
# Sensitivity (1-D)
# ---------------------------------------------------------------------------


def sensitivity_curve(values: Sequence[Any], scores: Sequence[float | None],
                      best_value: Any, spec_range: float | None = None) -> dict[str, Any]:
    """Classify one parameter's 1-D score curve around the chosen value.

    * ``plateau`` — contiguous values around ``best_value`` scoring within
      ``PLATEAU_TOLERANCE`` of it (relative to |score|, with a small absolute
      floor so near-zero scores don't make every wiggle "sensitive").
    * ``stability`` — plateau width / swept range (1.0 = flat curve).
    * ``neighbor_drop`` — worst relative drop one step either side.
    * ``label`` — stable / moderate / sensitive.
    """
    pts = [(v, s) for v, s in zip(values, scores) if s is not None and s > FAILED_SCORE / 2]
    if not pts:
        return {"values": list(values), "scores": list(scores), "label": "unknown",
                "stability": None, "plateau": None, "neighbor_drop": None,
                "best_value": best_value}
    vals = [p[0] for p in pts]
    scs = [p[1] for p in pts]
    try:
        bi = next(i for i, v in enumerate(vals) if _close(v, best_value))
    except StopIteration:
        bi = max(range(len(scs)), key=lambda i: scs[i])
    ref = scs[bi]
    spread = max(scs) - min(scs)
    tol = max(abs(ref) * PLATEAU_TOLERANCE, spread * 0.05, 1e-9)
    lo = hi = bi
    while lo - 1 >= 0 and scs[lo - 1] >= ref - tol:
        lo -= 1
    while hi + 1 < len(scs) and scs[hi + 1] >= ref - tol:
        hi += 1
    swept = (float(vals[-1]) - float(vals[0])) if len(vals) > 1 else 0.0
    width = float(vals[hi]) - float(vals[lo])
    stability = (width / swept) if swept > 0 else 1.0
    denom = max(abs(ref), spread, 1e-9)
    drops = []
    for j in (bi - 1, bi + 1):
        if 0 <= j < len(scs):
            drops.append(max(0.0, (ref - scs[j]) / denom))
    neighbor_drop = max(drops) if drops else 0.0
    if stability >= 0.3 and neighbor_drop <= 0.15:
        label = "stable"
    elif stability < 0.12 or neighbor_drop > 0.4:
        label = "sensitive"
    else:
        label = "moderate"
    return {
        "values": vals,
        "scores": [round(s, 4) for s in scs],
        "best_value": vals[bi],
        "plateau": [vals[lo], vals[hi]],
        "stability": round(stability, 3),
        "neighbor_drop": round(neighbor_drop, 3),
        "label": label,
    }


def sweep_values(spec: ParameterSpec, max_points: int = 25) -> list[Any]:
    """Evenly thinned value list for a sensitivity sweep (keeps both ends)."""
    vals = spec.values()
    if len(vals) <= max_points:
        return vals
    step = (len(vals) - 1) / (max_points - 1)
    picked = sorted({vals[int(round(i * step))] for i in range(max_points)})
    return picked


# ---------------------------------------------------------------------------
# Robustness + warning signs
# ---------------------------------------------------------------------------


def top_cluster(rows: Sequence[dict], specs: Sequence[ParameterSpec],
                top_n: int = 10) -> dict[str, Any]:
    """How tightly the top-N compliant results cluster in parameter space.

    Dispersion per param = std of the top-N values / the swept range. A tight
    cluster (low dispersion) means "the good region is a region", not a
    lucky isolated point.
    """
    ranked = sorted((r for r in _valid(rows) if r.get("constraints_met")),
                    key=lambda r: r["score"], reverse=True)[:top_n]
    if len(ranked) < 3:
        return {"n": len(ranked), "dispersion": None, "score_spread": None}
    disp = {}
    for s in specs:
        if not s.optimize or s.max <= s.min:
            continue
        vals = [float(r["params"][s.name]) for r in ranked if s.name in r["params"]]
        disp[s.name] = round(pstdev(vals) / (s.max - s.min), 3) if len(vals) > 1 else 0.0
    best = ranked[0]["score"]
    spread = (best - ranked[-1]["score"]) / max(abs(best), 1e-9)
    return {"n": len(ranked), "dispersion": disp, "score_spread": round(spread, 3)}


def robustness_score(
    sensitivity: dict[str, dict] | None,
    walk_forward: dict | None,
    cluster: dict | None,
    best_metrics: dict | None,
) -> dict[str, Any]:
    """0–10 robustness score from the evidence that is available.

    Components (weights): parameter plateaus 4, walk-forward efficiency 3,
    top-result clustering 2, trade-count sufficiency 1. Missing evidence is
    dropped and the rest re-normalized, so a run without walk-forward can
    still score — but ``components`` shows exactly what was used.
    """
    parts: dict[str, tuple[float, float]] = {}
    if sensitivity:
        stab = [v["stability"] for v in sensitivity.values() if v.get("stability") is not None]
        if stab:
            parts["plateaus"] = (min(1.0, mean(stab) / 0.5), 4.0)
    if walk_forward and walk_forward.get("efficiency") is not None:
        eff = float(walk_forward["efficiency"])
        parts["walk_forward"] = (min(1.0, max(0.0, eff) / 0.8), 3.0)
    if cluster and cluster.get("dispersion"):
        avg = mean(cluster["dispersion"].values()) if cluster["dispersion"] else 1.0
        parts["clustering"] = (max(0.0, 1.0 - avg / 0.3), 2.0)
    if best_metrics:
        trades = float(best_metrics.get("total_trades") or 0)
        parts["sample_size"] = (min(1.0, trades / 30.0), 1.0)
    if not parts:
        return {"score": None, "components": {}}
    total_w = sum(w for _, w in parts.values())
    score = sum(v * w for v, w in parts.values()) / total_w * 10.0
    return {
        "score": round(score, 2),
        "components": {k: {"value": round(v, 3), "weight": w} for k, (v, w) in parts.items()},
    }


def warning_signs(
    cfg: OptimizationConfig,
    best: dict | None,
    sensitivity: dict[str, dict] | None,
    walk_forward: dict | None,
    baseline: dict | None = None,
) -> list[dict[str, str]]:
    """The PRD's "Overfitting Warning Signs", evaluated for this run."""
    out: list[dict[str, str]] = []
    if not best:
        return [{"level": "danger", "code": "no_valid",
                 "message": "No parameter set met all constraints."}]
    m = best.get("metrics", {})
    trades = int(m.get("total_trades") or 0)
    if float(m.get("sharpe") or 0) > 3.0:
        out.append({"level": "warning", "code": "high_sharpe",
                    "message": f"Sharpe {m.get('sharpe'):.2f} > 3 — unusually high; "
                               "verify it is not fitted noise."})
    if trades < 30:
        out.append({"level": "warning", "code": "few_trades",
                    "message": f"Only {trades} trades — results are statistically weak "
                               "(aim for 30+)."})
    n_opt = len(cfg.optimized)
    if trades and trades / max(n_opt, 1) < 10:
        out.append({"level": "warning", "code": "trades_per_param",
                    "message": f"{trades} trades for {n_opt} optimized parameters — "
                               "fewer than 10 trades per parameter."})
    for spec in cfg.optimized:
        v = best["params"].get(spec.name)
        if v is None or spec.size() < 3:
            continue
        if _close(v, spec.min) or _close(v, spec.max):
            edge = "minimum" if _close(v, spec.min) else "maximum"
            out.append({"level": "info", "code": f"edge_{spec.name}",
                        "message": f"Best {spec.name}={v} is at the {edge} of the searched "
                                   "range — consider extending the range."})
    for name, s in (sensitivity or {}).items():
        if s.get("label") == "sensitive":
            out.append({"level": "warning", "code": f"sharp_peak_{name}",
                        "message": f"{name} is a sharp peak (stability "
                                   f"{s.get('stability')}) — small changes move the "
                                   "score a lot; prefer a value inside a plateau."})
    if walk_forward:
        if walk_forward.get("overfitted"):
            out.append({"level": "danger", "code": "wf_overfit",
                        "message": walk_forward.get("verdict")
                        or "Walk-forward shows significant out-of-sample degradation."})
        for name, st in (walk_forward.get("param_stability") or {}).items():
            if st.get("cv") is not None and st["cv"] > 0.5:
                out.append({"level": "info", "code": f"wf_unstable_{name}",
                            "message": f"{name} changes a lot between walk-forward splits "
                                       f"(CV {st['cv']:.2f})."})
    elif not cfg.walk_forward.enabled:
        out.append({"level": "info", "code": "no_wf",
                    "message": "Walk-forward validation was off — out-of-sample "
                               "behaviour is unknown."})
    if baseline and baseline.get("score") is not None and best.get("score") is not None:
        base, top = float(baseline["score"]), float(best["score"])
        if cfg.objective in RATIO_OBJECTIVES and base > 0 and top > base * 3:
            out.append({"level": "info", "code": "big_jump",
                        "message": f"Best score is {top / base:.1f}× the current parameters' "
                                   "— large in-sample jumps often shrink out-of-sample."})
    return out
