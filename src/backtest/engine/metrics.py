"""Run-level metrics for :class:`~backtest.engine.backtester.BacktestResult`.

Everything that depends on *individual trades* (count, win rate, per-trade
P&L) comes from :func:`backtest.engine.trades.walk_trades`, which the UI's trade
table reads as well — the cards and the table are the same computation, not two
approximations of it (gaps G1/G2).

Quant-grade extensions (2026-09-21): Sortino, profit factor, expectancy,
VaR/ES, max consecutive losses, exposure — needed for self-sufficient
trading system evaluation.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from backtest.engine.trades import trade_stats, walk_trades


def _var_es(returns: pd.Series, alpha: float = 0.05) -> tuple[float, float]:
    """Historical VaR / Expected Shortfall at alpha (e.g. 5%)."""
    if len(returns) < 10:
        return 0.0, 0.0
    try:
        # VaR is the alpha-quantile of returns (negative = loss)
        var = float(np.quantile(returns.values, alpha))
        # ES is mean of returns <= VaR
        tail = returns[returns <= var]
        es = float(tail.mean()) if len(tail) else var
        return var, es
    except Exception:
        return 0.0, 0.0


def compute_metrics(result) -> dict:
    equity = result.equity
    capital = result.config.initial_capital
    ppy = result.config.periods_per_year
    years = len(equity) / ppy if ppy else 1.0

    total_return = equity.iloc[-1] / capital - 1 if capital else 0.0
    cagr = (
        (equity.iloc[-1] / equity.iloc[0]) ** (1 / years) - 1
        if len(equity) > 0 and equity.iloc[0]
        else 0.0
    )

    returns = result.returns.fillna(0)
    volatility = returns.std(ddof=0) * math.sqrt(ppy) if len(returns) > 0 else 0.0
    sharpe = (returns.mean() * ppy / volatility) if volatility > 0 else 0.0

    # Sortino — downside deviation only
    downside = returns[returns < 0]
    downside_vol = downside.std(ddof=0) * math.sqrt(ppy) if len(downside) > 0 else 0.0
    sortino = (returns.mean() * ppy / downside_vol) if downside_vol > 0 else 0.0

    drawdown = equity / equity.cummax() - 1
    max_drawdown = float(drawdown.min()) if len(drawdown) > 0 else 0.0
    calmar = cagr / abs(max_drawdown) if max_drawdown < 0 and abs(max_drawdown) > 0 else 0.0

    # VaR / ES
    var_95, es_95 = _var_es(returns, 0.05)
    var_99, es_99 = _var_es(returns, 0.01)

    position = result.position.fillna(0)

    # Trade accounting from the equity curve, so costs land on the trade that
    # paid them and Σ trade P&L reconciles with total_return. No candle frame
    # required — prices are a display concern, not an accounting one.
    trades = walk_trades(equity, position) if len(equity) and len(position) else []
    stats = trade_stats(trades)

    exposure = float((position.abs() > 0).mean()) if len(position) else 0.0

    # Extended trade stats — profit factor, expectancy, consecutive losses
    realised_pnls = [t.pnl for t in trades if not t.is_open]
    wins = [p for p in realised_pnls if p > 0]
    losses = [p for p in realised_pnls if p < 0]
    gross_profit = sum(wins) if wins else 0.0
    gross_loss = abs(sum(losses)) if losses else 0.0
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else 0.0
    expectancy = (sum(realised_pnls) / len(realised_pnls)) if realised_pnls else 0.0

    # Max consecutive losses
    max_consec_losses = 0
    cur_consec = 0
    for t in trades:
        if t.is_open:
            continue
        if t.result == "Loss":
            cur_consec += 1
            max_consec_losses = max(max_consec_losses, cur_consec)
        else:
            cur_consec = 0

    # Avg holding period (in bars) — estimate from trade count vs exposure
    if stats["num_trades"]:
        avg_holding_bars = exposure * len(equity) / stats["num_trades"]
    else:
        avg_holding_bars = 0.0

    return {
        "total_return": total_return,
        "cagr": cagr,
        "volatility": volatility,
        "sharpe": sharpe,
        "sortino": sortino,
        "max_drawdown": max_drawdown,
        "calmar": calmar,
        "var_95": var_95,
        "es_95": es_95,
        "var_99": var_99,
        "es_99": es_99,
        "num_trades": stats["num_trades"],  # round trips, incl. one open trade
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
        "profit_factor": profit_factor,
        "expectancy": expectancy,
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "max_consecutive_losses": max_consec_losses,
        "exposure": exposure,
        "avg_holding_bars": avg_holding_bars,
        "final_equity": float(equity.iloc[-1]),
        "bars": int(len(equity)),
    }
