"""Run-level metrics for :class:`~backtest.engine.backtester.BacktestResult`.

Everything that depends on *individual trades* (count, win rate, per-trade
P&L) comes from :func:`backtest.engine.trades.walk_trades`, which the UI's trade
table reads as well — the cards and the table are the same computation, not two
approximations of it (gaps G1/G2).

Quant-grade extensions (2026-09-21): Sortino, profit factor, expectancy,
VaR/ES, max consecutive losses, exposure — needed for self-sufficient
trading system evaluation.

PRD ``docs/backTest-enhance.md`` §2 adds the risk/tail, drawdown-detail,
trade-quality and statistical-confidence families. Their maths lives in
:mod:`backtest.engine.metrics_risk`; this module stays the orchestrator so
every tab that runs the engine — Backtest, Compare and Optimize — picks them up
from one place.
"""

from __future__ import annotations

import math

from backtest.engine.metrics_risk import (
    consecutive_streaks,
    drawdown_detail,
    omega_ratio,
    payoff_ratio,
    return_skew_kurtosis,
    sharpe_std_error,
    trade_count_flag,
    trade_durations,
    var_es,
)
from backtest.engine.trades import trade_stats, walk_trades


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

    # VaR / ES. One implementation, shared with the §2 tail block below: the
    # local copy that used to live here had drifted into a second definition of
    # the same quantile, which is exactly how two cards start disagreeing.
    var_95, es_95 = var_es(returns, 0.05)
    var_99, es_99 = var_es(returns, 0.01)

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

    # Streaks. Max consecutive losses is the capital-tolerance number, and it
    # is computed from the SAME closed-trade walk the trade table shows — one
    # definition of a streak, not two that can disagree.
    closed_results = [t.result for t in trades if not t.is_open]
    max_wins, max_consec_losses = consecutive_streaks(closed_results)

    # Avg holding period (in bars) — the older exposure-derived estimate, kept
    # because Compare/Optimize surfaces already read this key. PRD §2 adds the
    # measured `avg_trade_duration_bars` / `median_trade_duration_bars` below;
    # note the estimate is algebraically the same as the measured mean whenever
    # the trade spans tile the curve, so the real gain is the MEDIAN, not a
    # corrected average.
    if stats["num_trades"]:
        avg_holding_bars = exposure * len(equity) / stats["num_trades"]
    else:
        avg_holding_bars = 0.0

    last_equity = float(equity.iloc[-1]) if len(equity) else 0.0

    # ---------------------------------------------------------------- §2
    # Risk & tail. `returns` is a fraction series, so these are fractions per
    # period, matching volatility/sharpe. The Rupee forms the PRD asks for are
    # added explicitly below rather than left for a call site to guess at.
    dd = drawdown_detail(equity)
    skew, excess_kurt = return_skew_kurtosis(returns)

    # Trade quality. `wins`/`losses` are already split above, so the payoff
    # ratio reuses them rather than re-filtering the trade list. Durations use
    # CLOSED trades only: an open trade is right-censored (it has been held at
    # least N bars, not exactly N), and averaging a censored sample in drags the
    # mean down toward a number nobody actually held.
    payoff = payoff_ratio(wins, losses)
    avg_bars, median_bars = trade_durations([t.bars_held for t in trades if not t.is_open])

    # Statistical confidence: is this sample big enough to believe, and how
    # wide is the error bar on the Sharpe we just printed? Flagged on CLOSED
    # trades — an open trade is not a result yet.
    se = sharpe_std_error(sharpe, stats["closed_trades"])
    flag = trade_count_flag(stats["closed_trades"])

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
        "final_equity": last_equity,
        "bars": int(len(equity)),
        # --- §2.1 risk & tail -----------------------------------------
        # These are the PRD's own key names. `kurtosis` is EXCESS (Fisher)
        # kurtosis, so a normal distribution reads 0.0, not 3.0.
        "omega": omega_ratio(returns),
        "skewness": skew,
        "kurtosis": excess_kurt,
        "ulcer_index": dd["ulcer_index"],
        # CVaR 95% is Expected Shortfall at 95% — the same number under the name
        # the PRD uses, so it is aliased rather than recomputed. It is worse
        # (more negative) than VaR 95% by construction, and that ordering is
        # what makes it the number worth showing.
        "cvar_95": es_95,
        "var_95_inr": var_95 * last_equity,
        "cvar_95_inr": es_95 * last_equity,
        # --- §2.1 drawdown detail -------------------------------------
        "max_drawdown_duration_days": dd["max_drawdown_duration_days"],
        "max_drawdown_recovery_days": dd["max_drawdown_recovery_days"],
        "max_drawdown_recovered": dd["max_drawdown_recovered"],
        "time_in_drawdown_pct": dd["time_in_drawdown_pct"],
        "drawdowns_over_10pct": dd["drawdowns_over_10pct"],
        "drawdown_episodes": dd["drawdown_episodes"],
        # --- §2.1 trade quality ---------------------------------------
        # `expectancy_inr` is the PRD's name for the Rupee form of `expectancy`.
        "expectancy_inr": expectancy,
        "payoff_ratio": payoff,
        "max_consecutive_wins": max_wins,
        "avg_trade_duration_bars": avg_bars,
        "median_trade_duration_bars": median_bars,
        # --- §2.1 statistical confidence ------------------------------
        "sharpe_std_error": se,
        "sharpe_ci_low": sharpe - se,
        "sharpe_ci_high": sharpe + se,
        "trade_count_flag": flag,
        "trade_count_sufficient": flag != "insufficient",
    }
