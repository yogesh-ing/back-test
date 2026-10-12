"""Risk, tail, drawdown and trade-quality statistics (PRD backTest-enhance §2).

Split out of :mod:`backtest.engine.metrics` so each family can be tested on its
own, and so the orchestrator stays readable. Everything here is a pure function
over an equity curve, a return series or a list of trade P&Ls — no strategy, no
portfolio, no I/O.

Three of these deserve a note, because "the obvious implementation" is wrong in
a way that flatters the strategy:

* **Max drawdown duration** is measured on the *worst* episode, from the peak
  that started it to the bar that recovered it — not from the trough. A drawdown
  that never recovers has no end, and reporting only its depth hides exactly the
  fact an operator needs to know.
* **Sharpe standard error** uses Lo's (2002) estimator
  ``sqrt((1 + S²/2) / N)``, which collapses to the familiar ``1/sqrt(N)`` only
  for a small Sharpe. The simpler form understates the error precisely when the
  Sharpe looks good, which is the case where you need the warning.
* **Omega** is capped (see :data:`OMEGA_CAP`) rather than reported as infinity:
  a strategy with no losing bar has an undefined, not a perfect, Omega.
"""

from __future__ import annotations

import math
from typing import Any, Sequence

import numpy as np
import pandas as pd

__all__ = [
    "OMEGA_CAP",
    "TRADE_COUNT_OK",
    "TRADE_COUNT_WARN",
    "FLAG_INSUFFICIENT",
    "FLAG_OK",
    "FLAG_WARN",
    "drawdown_episodes",
    "drawdown_detail",
    "ulcer_index",
    "var_es",
    "return_skew_kurtosis",
    "omega_ratio",
    "sharpe_std_error",
    "trade_count_flag",
    "payoff_ratio",
    "consecutive_streaks",
    "trade_durations",
]

#: Cap for a ratio with an unbounded denominator (no losing bars). Matches
#: ``optimization.evaluator.PROFIT_FACTOR_CAP`` so the two paths agree.
OMEGA_CAP = 100.0

#: Closed-trade thresholds for ``trade_count_flag``. Below 20 the whole metric
#: block is noise; 20-29 is usable with a caveat; 30+ is a defensible sample.
TRADE_COUNT_OK = 30
TRADE_COUNT_WARN = 20

#: The three states ``trade_count_flag`` returns. Named ``FLAG_*`` rather than
#: ``TRADE_COUNT_*`` so a reader cannot confuse the string "warn" with the
#: number 20 — which is exactly the mix-up these two used to invite.
FLAG_OK = "ok"
FLAG_WARN = "warn"
FLAG_INSUFFICIENT = "insufficient"

#: A drawdown deeper than this counts toward ``drawdowns_over_10pct``.
DRAWDOWN_THRESHOLD = 0.10


# ---------------------------------------------------------------------------
# Drawdown
# ---------------------------------------------------------------------------


def drawdown_episodes(drawdown: pd.Series) -> list[dict[str, Any]]:
    """Every underwater stretch, with its peak, trough and recovery.

    An episode runs from a new equity high to the next new high. The final
    episode has ``recovery_i is None`` when the curve ended below its peak —
    that is a real state of affairs, not missing data, so it is reported rather
    than dropped.
    """
    if len(drawdown) == 0:
        return []
    values = np.asarray(drawdown.values, dtype="float64")
    episodes: list[dict[str, Any]] = []
    in_dd = False
    peak_i = trough_i = 0

    def close(peak: int, trough: int, recovery: int | None) -> None:
        episodes.append(
            {
                "peak_i": peak,
                "trough_i": trough,
                "recovery_i": recovery,
                "depth": float(values[trough]),
                "recovered": recovery is not None,
            }
        )

    for i, value in enumerate(values):
        if value < 0:
            if not in_dd:
                in_dd = True
                peak_i = i - 1 if i > 0 else 0
                trough_i = i
            elif value < values[trough_i]:
                trough_i = i
        elif in_dd:
            close(peak_i, trough_i, i)
            in_dd = False

    if in_dd:
        close(peak_i, trough_i, None)
    return episodes


def _days_between(index: pd.Index, a: int, b: int) -> int:
    """Calendar days from bar ``a`` to bar ``b`` (never negative)."""
    return max(0, (pd.Timestamp(index[b]) - pd.Timestamp(index[a])).days)


def drawdown_detail(equity: pd.Series) -> dict[str, Any]:
    """Drawdown DEPTH is one number; duration, recovery and frequency are four.

    A strategy that recovers a 20% drawdown in a week and one that takes two
    years to recover the same 20% have the same max drawdown and are not the
    same trade.
    """
    empty = {
        "max_drawdown_duration_days": 0,
        "max_drawdown_recovery_days": 0,
        "max_drawdown_recovered": True,
        "time_in_drawdown_pct": 0.0,
        "drawdowns_over_10pct": 0,
        "drawdown_episodes": 0,
        "ulcer_index": 0.0,
    }
    if equity is None or len(equity) == 0:
        return empty

    drawdown = equity / equity.cummax() - 1
    episodes = drawdown_episodes(drawdown)
    if not episodes:
        return empty

    index = equity.index
    worst = min(episodes, key=lambda e: e["depth"])
    end_i = len(equity) - 1
    # An unrecovered episode has no recovery bar; measure to the end of the run
    # and say so, rather than reporting a duration that implies a recovery.
    end_of_worst = worst["recovery_i"] if worst["recovered"] else end_i

    deep = sum(1 for e in episodes if e["depth"] <= -DRAWDOWN_THRESHOLD)
    return {
        # PRD: "how many calendar days did the worst drawdown last start to end"
        "max_drawdown_duration_days": _days_between(index, worst["peak_i"], end_of_worst),
        "max_drawdown_recovery_days": _days_between(index, worst["trough_i"], end_of_worst),
        "max_drawdown_recovered": worst["recovered"],
        "time_in_drawdown_pct": round(float((drawdown < 0).mean()) * 100, 2),
        "drawdowns_over_10pct": deep,
        "drawdown_episodes": len(episodes),
        "ulcer_index": round(_ulcer(drawdown), 4),
    }


def _ulcer(drawdown: pd.Series) -> float:
    """Ulcer Index: RMS of the percentage drawdown — depth AND time in one number."""
    if len(drawdown) == 0:
        return 0.0
    pct = np.asarray(drawdown.values, dtype="float64") * 100.0
    return float(np.sqrt(np.mean(np.square(pct))))


def ulcer_index(drawdown: pd.Series) -> float:
    """Public single-metric form (drawdown as a fraction, not a percentage)."""
    return _ulcer(drawdown)


# ---------------------------------------------------------------------------
# Return distribution
# ---------------------------------------------------------------------------


def var_es(returns: pd.Series, alpha: float = 0.05) -> tuple[float, float]:
    """Historical VaR and Expected Shortfall at ``alpha`` (returns, not ₹).

    VaR is the alpha-quantile of realised returns; ES is the mean of the tail
    at or below it, so ES is always the worse of the pair — which is the number
    that matters, because the worst 5% of days are not interchangeable.
    """
    if returns is None or len(returns) < 10:
        return 0.0, 0.0
    try:
        values = np.asarray(returns.fillna(0.0).values, dtype="float64")
        var = float(np.quantile(values, alpha))
        tail = values[values <= var]
        return var, (float(tail.mean()) if tail.size else var)
    except (ValueError, IndexError, TypeError):
        return 0.0, 0.0


def return_skew_kurtosis(returns: pd.Series) -> tuple[float, float]:
    """Skew and EXCESS (Fisher) kurtosis of the return series.

    Skew < 0 means the distribution leans toward occasional large losses — the
    shape that breaks short-premium strategies, which sell tails they cannot
    stop collecting. Kurtosis > 0 means fatter tails than a normal, i.e. rare
    events are more likely than the volatility figure suggests.
    """
    if returns is None or len(returns) < 3:
        return 0.0, 0.0
    try:
        return round(float(returns.skew()), 4), round(float(returns.kurt()), 4)
    except (ValueError, TypeError):
        return 0.0, 0.0


def omega_ratio(returns: pd.Series, threshold: float = 0.0, cap: float = OMEGA_CAP) -> float:
    """Probability-weighted gains over probability-weighted losses.

    Unlike Sortino, Omega keeps the whole return distribution, not just the
    deviation below the threshold: a run with many small gains and one large
    loss can score better than a run with a single large gain.
    """
    if returns is None or len(returns) == 0:
        return 0.0
    try:
        values = np.asarray(returns.fillna(threshold).values, dtype="float64")
    except (ValueError, TypeError):
        return 0.0
    gains = float(values[values > threshold].sum())
    losses = float(-values[values < threshold].sum())
    if losses <= 0:
        # No losing bar: the ratio is undefined, not perfect. Capped, and the
        # trade-quality block's win/loss counts tell the real story.
        return cap if gains > 0 else 0.0
    return round(min(gains / losses, cap), 4)


def sharpe_std_error(sharpe: float, n: int) -> float:
    """Standard error of an observed Sharpe (Lo 2002).

    ``sqrt((1 + S²/2) / N)`` — the familiar ``1/sqrt(N)`` is its low-Sharpe
    limit. Using the bare ``1/sqrt(N)`` understates the error exactly when the
    Sharpe looks good, which is when the warning is worth having.
    """
    if n is None or n < 2:
        return 0.0
    try:
        s = float(sharpe)
    except (TypeError, ValueError):
        return 0.0
    return round(math.sqrt((1.0 + (s * s) / 2.0) / n), 4)


# ---------------------------------------------------------------------------
# Trade quality
# ---------------------------------------------------------------------------


def trade_count_flag(closed_trades: int) -> str:
    """``ok`` / ``warn`` / ``insufficient`` for the sample size behind a metric.

    Return, drawdown and Sharpe are all point estimates. Below 20 closed
    trades the interval around them is wider than the numbers themselves, so
    the whole result block is flagged rather than a footnote.
    """
    if not closed_trades:
        return FLAG_INSUFFICIENT
    if closed_trades >= TRADE_COUNT_OK:
        return FLAG_OK
    if closed_trades >= TRADE_COUNT_WARN:
        return FLAG_WARN
    return FLAG_INSUFFICIENT


def payoff_ratio(wins: Sequence[float], losses: Sequence[float]) -> float:
    """Average winning trade ÷ average losing trade.

    The counterpart to profit factor: a strategy can have PF 1.5 built entirely
    on rare outsized wins, and this is what shows that.
    """
    avg_win = (sum(wins) / len(wins)) if wins else 0.0
    avg_loss = abs(sum(losses) / len(losses)) if losses else 0.0
    if avg_loss <= 0:
        return 0.0
    return round(avg_win / avg_loss, 4)


def consecutive_streaks(results: Sequence[str]) -> tuple[int, int]:
    """Longest run of wins and of losses in a closed-trade sequence.

    Max consecutive losses is the capital-tolerance number: it says how much
    room a runner needs before the strategy's own worst case arrives.
    """
    best_wins = best_losses = 0
    run_wins = run_losses = 0
    for r in results:
        if r == "Win":
            run_wins += 1
            run_losses = 0
            best_wins = max(best_wins, run_wins)
        elif r == "Loss":
            run_losses += 1
            run_wins = 0
            best_losses = max(best_losses, run_losses)
        else:  # a flat trade breaks both streaks — it proved nothing either way
            run_wins = run_losses = 0
    return best_wins, best_losses


def trade_durations(bars_held: Sequence[int]) -> tuple[float, float]:
    """Mean and median holding time in BARS, from the trade walk's own counts.

    Worth being precise about why this replaces the older
    ``exposure × bars / num_trades`` estimate. That estimate is *algebraically
    the same number* as the mean, whenever the trade spans tile the curve —
    which they do today by construction, so it was never obviously wrong. What
    it cannot do is give a **median**, and what it silently assumes is that
    tiling. A median is the more useful of the two: a strategy whose average
    hold is 12 bars typically holds for 2 and occasionally 40, and only one of
    those numbers describes the trade you are actually in.

    So this is a measurement rather than an inference, not a correction of an
    arithmetic error.
    """
    values = [int(b) for b in bars_held if b is not None and int(b) > 0]
    if not values:
        return 0.0, 0.0
    return round(float(np.mean(values)), 2), round(float(np.median(values)), 2)


def summarise(values: Any) -> dict[str, Any]:  # pragma: no cover - debugging aid
    """Tiny describe() for a pandas object — used when a metric looks wrong."""
    if isinstance(values, pd.Series):
        return values.describe().to_dict()
    return {"n": len(values) if hasattr(values, "__len__") else 1}
