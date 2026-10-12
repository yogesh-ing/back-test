"""Regime breakdown of a strategy's own history — PRD backTest-enhance §6.1.

"A strategy that only works in bull markets should not be certified for
all-weather paper trading." This splits one result's equity curve into named
calendar periods and reports what the parameters did in each.

Deliberately **not** the volatility-regime detector in
:mod:`backtest.intelligence.regime` — that one classifies live VIX into
low/moderate/high bands for alerting. This is a fixed calendar partition of a
backtest, because the question is not "what was the volatility doing" but
"which of these named stretches did this strategy actually survive". A VIX
regime label can change with the detector's parameters; these dates cannot.

**Not the VIX regime detector, and not a prediction.** The bands below are the
PRD's, fixed. They name what happened; nothing here forecasts, and a strategy
that scores well in one band has not been shown it will do so in the next one
like it.

Metrics are computed from the **full-resolution** equity series, never from
the downsampled curve the results page draws. Sampling every third bar changes
a Sharpe ratio enough to reorder two candidates, and a regime table built on
it would be a table of sampling artefacts.
"""

from __future__ import annotations

import math
from typing import Any, Iterable, Sequence

__all__ = ["REGIME_BANDS", "UNKNOWN_BAND_LABEL", "regime_breakdown"]

#: The PRD's fixed calendar bands. Closed on both ends.
REGIME_BANDS: tuple[tuple[str, str, str], ...] = (
    ("2020-01-01", "2020-03-31", "COVID crash"),
    ("2020-04-01", "2021-12-31", "Recovery bull"),
    ("2022-01-01", "2022-06-30", "Rate-hike correction"),
    ("2022-07-01", "2023-12-31", "Volatile recovery"),
    ("2024-01-01", "2024-12-31", "Low-volatility grind"),
)

#: Bars outside every named band. A run from 2015 to 2019 is mostly this, and
#: calling the whole thing "Recovery bull" would be a lie by omission.
UNKNOWN_BAND_LABEL = "Other / uncovered"

#: Below this many bars a per-period Sharpe is not worth printing. 20 daily
#: bars is three months; the standard error on a Sharpe over that is larger
#: than the number itself.
MIN_BARS = 20

#: Bars per year, for annualising the per-period Sharpe. Daily bars.
_SESSIONS = 252.0


def _returns(equity: Sequence[float]) -> list[float]:
    out: list[float] = []
    for prev, cur in zip(equity, equity[1:]):
        if prev and prev > 0:
            out.append((cur - prev) / prev)
    return out


def _max_drawdown(equity: Sequence[float]) -> float:
    """Peak-to-trough as a positive fraction, measured from the first bar.

    The first bar is the peak candidate: a period that never exceeds its own
    opening value has a drawdown measured from that opening, not from a peak
    that never existed.
    """
    if not equity:
        return 0.0
    peak = equity[0]
    worst = 0.0
    for value in equity:
        peak = max(peak, value)
        if peak > 0:
            worst = max(worst, (peak - value) / peak)
    return worst * 100.0


def _sharpe(returns: Sequence[float]) -> float | None:
    if len(returns) < 2:
        return None
    mean = sum(returns) / len(returns)
    var = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    if var <= 0:
        return None  # a perfectly flat stretch is not a Sharpe of infinity
    return (mean / math.sqrt(var)) * math.sqrt(_SESSIONS)


def _period_stats(equity: Sequence[float], bars: int) -> dict[str, Any]:
    rets = _returns(equity)
    total = ((equity[-1] / equity[0]) - 1.0) * 100.0 if equity and equity[0] > 0 else 0.0
    return {
        "bars": bars,
        "return_pct": round(total, 2),
        "sharpe": round(_sharpe(rets), 2) if _sharpe(rets) is not None else None,
        "max_drawdown_pct": round(_max_drawdown(equity), 2),
        "sufficient": bars >= MIN_BARS,
    }


def _band_for(day: str) -> tuple[str, str, str] | None:
    for start, end, label in REGIME_BANDS:
        if start <= day <= end:
            return (start, end, label)
    return None


def regime_breakdown(
    dates: Iterable[str],
    equity: Sequence[float],
    trades_in_period: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Split one result into the PRD's named periods.

    :param dates: bar dates, ISO ``YYYY-MM-DD``, aligned with ``equity``.
    :param equity: the equity value at each bar, full resolution.
    :param trades_in_period: optional ``{"YYYY-MM-DD": n}`` map of trades that
        CLOSED on a date. The PRD asks for a trade count per period; counting
        them from the curve would mean inferring a trade from a kink in equity,
        which is a guess with a number attached.
    """
    buckets: dict[str, list[float]] = {}
    order: list[str] = []
    for day, value in zip(dates, equity):
        band = _band_for(day)
        label = band[2] if band else UNKNOWN_BAND_LABEL
        if label not in buckets:
            buckets[label] = []
            order.append(label)
        buckets[label].append(float(value))

    covered = sum(len(v) for k, v in buckets.items() if k != UNKNOWN_BAND_LABEL)
    total = sum(len(v) for v in buckets.values())
    rows: list[dict[str, Any]] = []
    for label in order:
        values = buckets[label]
        stats = _period_stats(values, len(values))
        if trades_in_period:
            stats["trades"] = sum(
                n for day, n in trades_in_period.items()
                if (_band_for(day) or (None, None, UNKNOWN_BAND_LABEL))[2] == label
            )
        else:
            stats["trades"] = None
        band = next((b for b in REGIME_BANDS if b[2] == label), None)
        rows.append({
            "label": label,
            "from": band[0] if band else None,
            "to": band[1] if band else None,
            "named": band is not None,
            **stats,
        })

    return {
        "available": bool(rows),
        "periods": rows,
        "total_bars": total,
        "named_bars": covered,
        # How much of this run the PRD's bands actually describe. A 2015-2019
        # run scores 0% here, and the UI says so rather than implying the
        # table is complete.
        "named_coverage_pct": round((covered / total) * 100, 1) if total else 0.0,
        "min_bars": MIN_BARS,
    }
