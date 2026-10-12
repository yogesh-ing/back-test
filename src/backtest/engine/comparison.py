"""Cross-strategy comparison maths (PRD backTest-enhance §4.3 / §4.4 / §4.5).

Three questions you can only answer by looking at the results *together*:

* §4.3 — are these four strategies actually four different bets, or the same
  bet four times?
* §4.4 — is the winner's higher Sharpe real, or the luckiest of a set?
* §4.5 — do the curves start from the same point, so they can be compared at
  all?

Pure functions over return series, split out from the API so each is testable
on its own. No strategies, no I/O.

**The significance test is PAIRED, and that choice is load-bearing.** Two
strategies on the same tab traded the *same market days*, so their returns are
strongly coupled through that shared market. Resampling each series
independently injects noise from a correlation structure that is not in the
data, which makes the test conservative — it will under-declare real
differences. Resampling the same day-indices for every strategy preserves the
coupling, which is the correct null. Both strategies then see the identical
draw of market days, and any difference in their Sharpe distributions comes
from how each one *traded* those days, which is the question being asked.

The cost of pairing is that it is less sensitive; the benefit is that it is
right, and a comparison panel that cries "significant difference" at people is
worse than one that stays quiet.
"""

from __future__ import annotations

import logging
import math
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd

logger = logging.getLogger("backtest.engine.comparison")

__all__ = [
    "HIGH_CORRELATION",
    "SIGNIFICANCE_LEVELS",
    "correlation_matrix",
    "sharpe_significance",
    "rebase_to_100",
]

#: §4.3: "flag any pair with correlation > 0.8". Above this, holding both
#: alongside each other is taking roughly the same risk twice.
HIGH_CORRELATION = 0.8

#: §4.4 verdict bands, as percentages of simulations where A beat B.
SIGNIFICANCE_LEVELS = {
    "a_better": 95.0,  # >= 95%: A is better with real confidence
    "b_better": 5.0,  # <= 5%:  B is better with real confidence
}


# ---------------------------------------------------------------------------
# §4.3 Correlation
# ---------------------------------------------------------------------------


def _aligned(series: Mapping[str, pd.Series]) -> pd.DataFrame:
    """Outer-join every label's returns, then drop any bar not shared by all.

    Pairwise-intersecting each pair separately would let a pair correlate over
    500 bars while another pair uses 300, and the two cells of the same heatmap
    would be measured on different evidence. One shared index for the whole
    matrix, or the matrix is not a matrix.
    """
    if not series:
        return pd.DataFrame()
    frame = pd.concat(
        [pd.Series(s, name=label) for label, s in series.items()],
        axis=1,
    )
    return frame.dropna()


def correlation_matrix(series: Mapping[str, pd.Series]) -> dict[str, Any]:
    """Pairwise Pearson correlation of per-bar returns, plus the §4.3 flags.

    A standard deviation of zero (a strategy that never moved) has no
    correlation with anything, so those cells are ``None`` rather than 0.0 —
    a 0.0 would read as "uncorrelated, great for diversification" when the
    truth is "there is nothing here to diversify".
    """
    labels = list(series.keys())
    if len(labels) < 2:
        return {
            "available": False,
            "reason": "need at least two results to correlate",
            "labels": labels,
            "matrix": [],
            "pairs": [],
            "high_correlation_pairs": [],
        }

    frame = _aligned(series)
    n = len(frame)
    if n < 3:
        return {
            "available": False,
            "reason": f"only {n} bars shared by every result — too few to correlate",
            "labels": labels,
            "matrix": [],
            "pairs": [],
            "high_correlation_pairs": [],
        }

    values = frame.to_numpy(dtype="float64")
    stds = values.std(axis=0, ddof=0)
    usable = stds > 0

    matrix: list[list[float | None]] = []
    for i in range(len(labels)):
        row: list[float | None] = []
        for j in range(len(labels)):
            if i == j:
                row.append(1.0)
            elif not usable[i] or not usable[j]:
                row.append(None)
            else:
                a = values[:, i] - values[:, i].mean()
                b = values[:, j] - values[:, j].mean()
                denom = math.sqrt(float((a * a).sum()) * float((b * b).sum()))
                row.append(round(float((a * b).sum() / denom), 4) if denom > 0 else None)
        matrix.append(row)

    pairs = []
    for i in range(len(labels)):
        for j in range(i + 1, len(labels)):
            corr = matrix[i][j]
            pairs.append(
                {
                    "a": labels[i],
                    "b": labels[j],
                    "correlation": corr,
                    "high": corr is not None and corr > HIGH_CORRELATION,
                }
            )
    high = [p for p in pairs if p["high"]]
    # A "n/a" cell with no explanation reads as a rendering bug. Name the
    # curves that never moved, so the gap is visibly the data's and not ours.
    flat = sorted({p[a] for p in pairs if p["correlation"] is None for a in ("a", "b")})

    return {
        "available": True,
        "labels": labels,
        "matrix": matrix,
        "pairs": pairs,
        "aligned_bars": n,
        "high_correlation_threshold": HIGH_CORRELATION,
        "high_correlation_pairs": high,
        "max_correlation": max(
            (p["correlation"] for p in pairs if p["correlation"] is not None), default=None
        ),
        "min_correlation": min(
            (p["correlation"] for p in pairs if p["correlation"] is not None), default=None
        ),
        "undefined_pairs": [n for n in flat],
        "warnings": (
            [
                {
                    "level": "warning",
                    "code": "high_correlation",
                    "message": (
                        f"{p['a']} and {p['b']} are {abs(p['correlation']):.2f} correlated. "
                        "Running both offers little diversification benefit."
                    ),
                }
                for p in high
            ]
            + (
                [
                    {
                        "level": "info",
                        "code": "undefined_correlation",
                        "message": (
                            f"{', '.join(flat)} never moved, so correlation against it is "
                            "undefined rather than low. A flat curve is not a diversifier."
                        ),
                    }
                ]
                if flat
                else []
            )
        ),
    }


# ---------------------------------------------------------------------------
# §4.4 Statistical significance
# ---------------------------------------------------------------------------


def _sharpe(returns: np.ndarray, ppy: float) -> np.ndarray:
    """Annualised Sharpe along the LAST axis of an ``(..., n_bars)`` array.

    Leading axes pass through untouched, so the same function serves the
    ``(n_sims, n_bars, n_labels)`` bootstrap below and the single
    ``(1, n_bars, 1)`` slice used for the observed value.
    """
    if returns.size == 0:
        return np.zeros(returns.shape[:-1])
    mean = returns.mean(axis=-1)
    std = returns.std(axis=-1, ddof=0)
    scale = math.sqrt(ppy) if ppy > 0 else 1.0
    out = np.zeros_like(mean)
    ok = std > 0
    out[ok] = mean[ok] * ppy / (std[ok] * scale)
    return out


def sharpe_significance(
    series: Mapping[str, pd.Series],
    periods_per_year: float,
    *,
    simulations: int = 1000,
    seed: int = 42,
) -> dict[str, Any]:
    """Bootstrap the Sharpe of each result, then compare them pairwise.

    Returns, for each unordered pair, the share of simulations in which A's
    Sharpe exceeded B's. §4.4 renders that as "A is likely better than B
    (85% confidence)" or "no significant difference" — informational, never a
    gate.
    """
    labels = list(series.keys())
    if len(labels) < 2:
        return {
            "available": False,
            "reason": "need at least two results to compare",
            "comparisons": [],
            "labels": labels,
        }

    frame = _aligned(series)
    n = len(frame)
    if n < 10:
        return {
            "available": False,
            "reason": f"only {n} bars shared by every result — too few to test",
            "comparisons": [],
            "labels": labels,
        }

    values = frame.to_numpy(dtype="float64")
    rng = np.random.default_rng(seed)
    n_sims = max(100, int(simulations))
    # One draw of day-indices, shared by every strategy: see the module
    # docstring. Resampling each series independently would manufacture
    # disagreement that the shared market never produced.
    idx = rng.integers(0, n, size=(n_sims, n))
    # (n_sims, n_bars, n_labels) -> (n_sims, n_labels, n_bars) so Sharpe is
    # taken along bars, giving one Sharpe per simulation per strategy.
    boot = values[idx].transpose(0, 2, 1)
    sharpes = _sharpe(boot, float(periods_per_year or 0))  # (n_sims, n_labels)

    ppy = float(periods_per_year or 0)
    observed_series = _sharpe(values.T[np.newaxis, :, :], ppy)[0]
    observed = {label: round(float(observed_series[i]), 4) for i, label in enumerate(labels)}

    comparisons = []
    for i in range(len(labels)):
        for j in range(i + 1, len(labels)):
            diff = sharpes[:, i] - sharpes[:, j]
            a_better = float((diff > 0).mean()) * 100
            verdict = "no_significant_difference"
            winner = None
            if a_better >= SIGNIFICANCE_LEVELS["a_better"]:
                verdict, winner = "a_better", labels[i]
            elif a_better <= SIGNIFICANCE_LEVELS["b_better"]:
                verdict, winner = "b_better", labels[j]
            gap = observed[labels[i]] - observed[labels[j]]
            comparisons.append(
                {
                    "a": labels[i],
                    "b": labels[j],
                    "a_better_pct": round(a_better, 1),
                    "b_better_pct": round(100 - a_better, 1),
                    "observed_sharpe_gap": round(gap, 4),
                    "verdict": verdict,
                    "winner": winner,
                }
            )

    any_different = any(c["verdict"] != "no_significant_difference" for c in comparisons)
    return {
        "available": True,
        "labels": labels,
        "simulations": n_sims,
        "seed": int(seed),
        "aligned_bars": n,
        "paired": True,
        "observed_sharpe": observed,
        "comparisons": comparisons,
        "warnings": (
            []
            if any_different
            else [
                {
                    "level": "warning",
                    "code": "no_significant_winner",
                    "message": (
                        "No pair differs significantly. Promoting the top row out of "
                        "these results is promoting the luckiest, not the best."
                    ),
                }
            ]
        ),
    }


# ---------------------------------------------------------------------------
# §4.5 Rebased equity
# ---------------------------------------------------------------------------


def rebase_to_100(values: Sequence[float]) -> list[float]:
    """Index a curve to 100 at its first point.

    §4.5: two strategies entered with the same capital but different first-bar
    behaviour produce curves whose *levels* are not comparable, so the chart
    compares entry mechanics rather than performance. Indexing to 100 makes
    every curve start at the same place and turns the y-axis into a percentage
    return, which is the only thing the reader was looking at.
    """
    series = [float(v) for v in values]
    if not series:
        return []
    base = series[0]
    # A non-positive or non-finite base cannot be divided into an index. The
    # curve is returned untouched rather than filled with infinities, which
    # would blank the whole chart — the same fallback the JS helper uses.
    if base <= 0 or not math.isfinite(base):
        return series
    return [round(v / base * 100.0, 4) for v in series]
