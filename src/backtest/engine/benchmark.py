"""Buy-and-hold reference for a single result (PRD backTest-enhance §3.1).

The question this answers is the cheapest one in trading: *did the strategy do
anything at all?* An equity curve that doubles tells you nothing until you can
see what simply holding the same stock over the same bars would have produced.

Kept separate from :mod:`backtest.adapters.backtest_adapter` because the maths
is worth testing on its own — alpha and beta are exactly the kind of numbers
that look plausible while being computed on mismatched indices.

Two definitional choices, both stated here because they are the ones a reader
would otherwise have to guess at:

* **Alpha is simple excess return** — strategy total return minus benchmark
  total return over the same period. The PRD asks for exactly that. It is NOT
  the regression intercept, which is a different number and answers a different
  question; computing both under one name is how "alpha" becomes meaningless.
* **Beta is the OLS slope** of strategy returns on benchmark returns, i.e.
  ``cov(s, b) / var(b)``. It needs a *variance* in the denominator, so a
  benchmark that never moved (a flat synthetic series) makes it undefined, and
  it is reported as 0.0 rather than as an enormous number that would read as
  enormous sensitivity.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

__all__ = [
    "benchmark_equity",
    "benchmark_metrics",
    "alpha_beta",
    "build_benchmark",
]


def benchmark_equity(candles: pd.DataFrame, capital: float) -> pd.Series:
    """Equity of buying ``capital`` worth at the first close and holding.

    Deliberately costless. A benchmark that pays the same fees as the strategy
    would answer "did the strategy beat a buy-and-hold *account*", which is a
    question about fees as much as about the signal; the cost-shock table
    (§3.2) is where execution costs get their own line.
    """
    if candles is None or len(candles) == 0 or capital <= 0:
        return pd.Series(dtype="float64")
    close = candles["close"]
    first = float(close.iloc[0])
    if not first or not math.isfinite(first) or first <= 0:
        # A bad first print makes every later price a multiple of nonsense.
        return pd.Series(float(capital), index=close.index, dtype="float64")
    return (float(capital) * (close / first)).astype("float64")


def benchmark_metrics(equity: pd.Series, capital: float, periods_per_year: float) -> dict[str, Any]:
    """Return / CAGR / Sharpe / max drawdown for the buy-and-hold curve.

    Same definitions as ``engine/metrics.py`` so the two curves can be read
    side by side without the reader having to wonder whether Sharpe means
    something different in each column.
    """
    empty = {
        "total_return": 0.0,
        "cagr": 0.0,
        "sharpe": 0.0,
        "max_drawdown": 0.0,
        "final_equity": float(capital),
    }
    if equity is None or len(equity) < 2 or capital <= 0:
        return empty

    total_return = float(equity.iloc[-1]) / float(capital) - 1.0
    ppy = float(periods_per_year or 0)
    years = len(equity) / ppy if ppy > 0 else 0.0
    start = float(equity.iloc[0])
    cagr = ((float(equity.iloc[-1]) / start) ** (1 / years) - 1) if years > 0 and start > 0 else 0.0

    returns = equity.pct_change().dropna()
    vol = float(returns.std(ddof=0)) * math.sqrt(ppy) if len(returns) > 1 and ppy > 0 else 0.0
    sharpe = (float(returns.mean()) * ppy / vol) if vol > 0 else 0.0

    drawdown = equity / equity.cummax() - 1
    return {
        "total_return": round(total_return, 6),
        "cagr": round(cagr, 6),
        "sharpe": round(sharpe, 4),
        "max_drawdown": round(float(drawdown.min()), 6),
        "final_equity": round(float(equity.iloc[-1]), 2),
    }


def alpha_beta(
    strategy_returns: pd.Series,
    benchmark_returns: pd.Series,
    strategy_total: float,
    benchmark_total: float,
) -> dict[str, Any]:
    """Simple excess return (alpha) and OLS slope (beta).

    The two return series are intersected on index first. Strategy equity is
    recorded on the bars the engine actually traded; the benchmark is recorded
    on every bar the source produced. When those differ by even one bar, an
    un-aligned correlation compares two different days and reports a beta with
    no meaning attached to it.
    """
    empty = {"alpha": 0.0, "beta": 0.0, "aligned_bars": 0}
    if strategy_returns is None or benchmark_returns is None:
        return empty

    joined = pd.concat(
        [strategy_returns.rename("s"), benchmark_returns.rename("b")], axis=1, join="inner"
    ).dropna()
    n = len(joined)
    alpha = round(float(strategy_total) - float(benchmark_total), 6)
    if n < 3:
        return {**empty, "alpha": alpha, "aligned_bars": n}

    s = joined["s"].to_numpy(dtype="float64")
    b = joined["b"].to_numpy(dtype="float64")
    var_b = float(np.var(b))
    if var_b <= 0:
        # A benchmark that never moved has no beta — reporting a huge number
        # here would read as enormous sensitivity to a risk that does not exist.
        return {**empty, "alpha": alpha, "aligned_bars": n}
    beta = float(np.cov(s, b, ddof=0)[0][1] / var_b)
    return {"alpha": alpha, "beta": round(beta, 4), "aligned_bars": n}


def strategy_total(equity: pd.Series, capital: float) -> float:
    """Total return of the strategy curve — the other half of the alpha sum."""
    if equity is None or len(equity) == 0 or capital <= 0:
        return 0.0
    return float(equity.iloc[-1]) / float(capital) - 1.0


def build_benchmark(
    candles: pd.DataFrame,
    equity: pd.Series,
    capital: float,
    periods_per_year: float,
    strategy_total_return: float | None = None,
) -> dict[str, Any]:
    """The whole §3.1 block: curve, its own metrics, and alpha/beta.

    ``strategy_total_return`` is passed in rather than recomputed so alpha
    subtracts the SAME strategy return the cards above show. Deriving it from
    the curve here would let the two drift the moment the adapter's capital
    handling and this module's disagree.

    Returns a dict the adapter can drop straight into the payload. Every field
    is present even when the benchmark cannot be computed (no candles, zero
    capital) so the UI never has to distinguish "no benchmark" from
    "benchmark of zero" by reading around a missing key.

    The CURVE itself is deliberately not included: ``to_equity()`` already ships
    it for the chart overlay, and a second copy would double the payload's
    largest array and give the two a chance to disagree on date formatting.
    """
    curve = benchmark_equity(candles, capital)
    # Reindex onto the strategy's bars so the chart overlay is bar-for-bar and
    # so alpha/beta compare like with like. ffill is right here: the benchmark
    # simply held through any bar the engine did not trade.
    aligned = curve.reindex(equity.index).ffill() if len(curve) and len(equity) else curve
    metrics = benchmark_metrics(aligned, capital, periods_per_year)

    own = (
        float(strategy_total_return)
        if strategy_total_return is not None
        else strategy_total(equity, capital)
    )
    ab = alpha_beta(
        equity.pct_change(),
        aligned.pct_change() if len(aligned) else None,
        own,
        metrics["total_return"],
    )
    return {
        "label": "Buy & Hold",
        "available": bool(len(aligned)),
        **metrics,
        **ab,
    }
