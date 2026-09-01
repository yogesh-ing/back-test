"""Trade accounting for a backtest result — the single source of truth.

Both :func:`backtest.engine.metrics.compute_metrics` (the metric cards) and
:class:`backtest.adapters.backtest_adapter.BacktestAdapter` (the trade table) read
this, so "Trades" and "Win Rate" can no longer disagree with the rows shown
below them. That disagreement was gaps **G1** and **G2**:

* ``win_rate`` used to be derived from the *sign* of the position, so every
  long-only strategy reported 100% regardless of whether it made money;
* ``num_trades`` counted position *transitions*, so an entry and its exit each
  counted as a trade (a run with 2 round trips reported 4).

Accounting rules (deliberate, and pinned by ``tests/test_engine_trades.py``)
---------------------------------------------------------------------------
* A **trade** is a run of consecutive bars holding the same sign.
* Its P&L is measured on the **equity curve** — ``equity[exit] - ``
  ``equity[entry - 1]`` — so commission and slippage (which the engine books on
  the bars where the position changes) belong to the trade that paid them, and
  the sum of trade P&L reconciles with the total return. Reconstructing P&L
  from the entry/exit *prices* instead is what made the old table rows slightly
  wrong for shorts (gap G14).
* A position still open on the last bar is **one trade**, marked to the final
  close. It counts in ``num_trades`` but is **excluded from ``win_rate``** —
  an unrealised result is not yet a win or a loss. ``open_trades`` exposes the
  difference explicitly.
* A sign flip closes the old trade at the bar *before* the flip and opens the
  new one on the flip bar, so every bar is attributed exactly once.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Iterable, Optional

import pandas as pd

__all__ = ["Trade", "walk_trades", "trade_stats"]


@dataclass(frozen=True)
class Trade:
    """One round trip (or an open position) reconstructed from the run itself."""

    id: int
    entry_date: str
    exit_date: str
    side: str  # "LONG" | "SHORT"
    entry: Optional[float]  # close at the first held bar (None if no prices)
    exit: Optional[float]  # close at the exit bar (final close if still open)
    pnl: float  # equity-based, costs included
    result: str  # "Win" | "Loss" | "Flat"
    is_open: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _classify(pnl: float) -> str:
    """A zero-P&L trade is not a win — ties are excluded from the win count."""
    if pnl > 0:
        return "Win"
    if pnl < 0:
        return "Loss"
    return "Flat"


def walk_trades(
    equity: pd.Series,
    position: pd.Series,
    close: Optional[pd.Series] = None,
) -> list[Trade]:
    """Split a run into trades.

    ``position`` is the *held* series (already lagged by the engine), so a change
    booked at bar ``j`` means the position was flat/short/long from ``j`` on.

    ``close`` is only needed for the *display* columns (entry/exit prices). P&L
    comes from ``equity``, which already carries the costs — so metrics can call
    this without a candle frame, and a price series can never disagree with the
    equity curve about how much a trade made.
    """
    index = equity.index
    pos = position.reindex(index).fillna(0.0)
    eq = equity.reindex(index)
    px = close.reindex(index).ffill() if close is not None else None

    trades: list[Trade] = []
    entry_i: int | None = None  # index of the first held bar of the open trade
    entry_side = 0
    prev = 0.0

    def close_trade(exit_i: int, *, is_open: bool) -> None:
        entry_equity = float(eq.iloc[entry_i - 1]) if entry_i > 0 else float(eq.iloc[0])
        pnl = float(eq.iloc[exit_i]) - entry_equity
        trades.append(
            Trade(
                id=len(trades) + 1,
                entry_date=_fmt(index[entry_i]),
                exit_date=_fmt(index[exit_i]),
                side="LONG" if entry_side > 0 else "SHORT",
                entry=None if px is None else round(float(px.iloc[entry_i]), 6),
                exit=None if px is None else round(float(px.iloc[exit_i]), 6),
                pnl=round(pnl, 6),
                result=_classify(pnl),
                is_open=is_open,
            )
        )

    for i in range(len(index)):
        cur = float(pos.iloc[i])
        sign_changed = cur != 0 and prev != 0 and (cur > 0) != (prev > 0)

        if entry_i is not None and (cur == 0 or sign_changed):
            # Flip bar carries the new position's return and both costs, so the
            # closing trade stops one bar earlier; a flat bar keeps its exit cost.
            close_trade(i - 1 if sign_changed else i, is_open=False)
            entry_i = None

        if cur != 0 and entry_i is None:
            entry_i = i
            entry_side = cur
        prev = cur

    if entry_i is not None:  # still held at the last bar
        close_trade(len(index) - 1, is_open=True)

    return trades


def trade_stats(trades: Iterable[Trade]) -> dict[str, Any]:
    """Metric-card numbers derived from :func:`walk_trades` output.

    ``win_rate`` is over **closed** trades only (0.0 when nothing has closed —
    an open position tells you nothing about hit rate yet).
    """
    rows = list(trades)
    closed = [t for t in rows if not t.is_open]
    wins = sum(1 for t in closed if t.result == "Win")
    losses = sum(1 for t in closed if t.result == "Loss")
    realised = [t.pnl for t in closed]
    return {
        "num_trades": len(rows),
        "closed_trades": len(closed),
        "open_trades": len(rows) - len(closed),
        "winning_trades": wins,
        "losing_trades": losses,
        "win_rate": (wins / len(closed)) if closed else 0.0,
        "realised_pnl": round(sum(realised), 6) if realised else 0.0,
        "avg_trade_pnl": round(sum(realised) / len(realised), 6) if realised else 0.0,
        "best_trade_pnl": round(max(realised), 6) if realised else 0.0,
        "worst_trade_pnl": round(min(realised), 6) if realised else 0.0,
    }


def _fmt(ts: Any) -> str:
    """Compact, locale-free timestamp (date only for daily bars)."""
    t = pd.Timestamp(ts)
    if t.hour == 0 and t.minute == 0 and t.second == 0:
        return t.strftime("%Y-%m-%d")
    return t.strftime("%Y-%m-%d %H:%M")
