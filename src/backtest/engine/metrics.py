"""Run-level metrics for :class:`~backtest.engine.backtester.BacktestResult`.

Everything that depends on *individual trades* (count, win rate, per-trade
P&L) comes from :func:`backtest.engine.trades.walk_trades`, which the UI's trade
table reads as well — the cards and the table are the same computation, not two
approximations of it (gaps G1/G2).
"""

from __future__ import annotations

import math

import pandas as pd

from backtest.engine.trades import trade_stats, walk_trades


def compute_metrics(result) -> dict:
    equity = result.equity
    capital = result.config.initial_capital
    ppy = result.config.periods_per_year
    years = len(equity) / ppy if ppy else 1.0

    total_return = equity.iloc[-1] / capital - 1 if capital else 0.0
    cagr = (equity.iloc[-1] / equity.iloc[0]) ** (1 / years) - 1 if len(equity) > 0 and equity.iloc[0] else 0.0

    returns = result.returns.fillna(0)
    volatility = returns.std(ddof=0) * math.sqrt(ppy) if len(returns) > 0 else 0.0
    sharpe = (returns.mean() * ppy / volatility) if volatility > 0 else 0.0

    drawdown = equity / equity.cummax() - 1
    max_drawdown = float(drawdown.min()) if len(drawdown) > 0 else 0.0
    calmar = cagr / abs(max_drawdown) if max_drawdown < 0 and abs(max_drawdown) > 0 else 0.0

    position = result.position.fillna(0)

    # Trade accounting from the equity curve, so costs land on the trade that
    # paid them and Σ trade P&L reconciles with total_return. No candle frame
    # required — prices are a display concern, not an accounting one.
    trades = walk_trades(equity, position) if len(equity) and len(position) else []
    stats = trade_stats(trades)

    exposure = float((position.abs() > 0).mean()) if len(position) else 0.0

    return {
        "total_return": total_return,
        "cagr": cagr,
        "volatility": volatility,
        "sharpe": sharpe,
        "max_drawdown": max_drawdown,
        "calmar": calmar,
        "num_trades": stats["num_trades"],          # round trips, incl. one open trade
        "closed_trades": stats["closed_trades"],
        "open_trades": stats["open_trades"],
        "winning_trades": stats["winning_trades"],
        "losing_trades": stats["losing_trades"],
        # Share of CLOSED trades that made money (an open trade is not a result
        # yet); 0.0 when nothing has closed.
        "win_rate": stats["win_rate"],
        "realised_pnl": stats["realised_pnl"],
        "avg_trade_pnl": stats["avg_trade_pnl"],
        "best_trade_pnl": stats["best_trade_pnl"],
        "worst_trade_pnl": stats["worst_trade_pnl"],
        "exposure": exposure,
        "final_equity": float(equity.iloc[-1]),
        "bars": int(len(equity)),
    }
