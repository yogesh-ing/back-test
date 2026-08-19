from __future__ import annotations

import math

import numpy as np
import pandas as pd


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
    win_rate = 0.0
    num_trades = 0

    if len(position) > 0:
        changes = position.ne(position.shift(1).fillna(0))
        trade_starts = changes & (position != 0)
        trade_ends = changes & (position == 0)
        num_trades = int((trade_starts | trade_ends).sum())

        trade_pnls = []
        current = 0
        for i, value in position.items():
            if value != 0 and current == 0:
                current = value
            elif value == 0 and current != 0:
                trade_pnls.append(current)
                current = 0
        if current != 0:
            trade_pnls.append(current)
        if trade_pnls:
            win_rate = sum(1 for v in trade_pnls if v > 0) / len(trade_pnls)

    exposure = float((position.abs() > 0).mean()) if len(position) else 0.0

    return {
        "total_return": total_return,
        "cagr": cagr,
        "volatility": volatility,
        "sharpe": sharpe,
        "max_drawdown": max_drawdown,
        "calmar": calmar,
        "num_trades": num_trades,
        "win_rate": win_rate,
        "exposure": exposure,
        "final_equity": float(equity.iloc[-1]),
        "bars": int(len(equity)),
    }
