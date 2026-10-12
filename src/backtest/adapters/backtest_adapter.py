"""Bridge from ``BacktestResult`` to dashboard, compare and forward payloads.

PRD §4.5 — Task 1.4. The adapter never mutates its input and always emits
JSON-serialisable data (native ``float``/``int``/``str``, ISO date strings) so
it can be returned directly from Flask endpoints.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from backtest.engine.backtester import BacktestResult
from backtest.engine.benchmark import build_benchmark
from backtest.engine.monte_carlo import monte_carlo_trade_order
from backtest.engine.trades import walk_trades
from backtest.logging_config import get_logger

log = get_logger(__name__)


def _fmt_dt(ts: Any) -> str:
    """Format a pandas Timestamp as a compact, locale-free string."""
    t = pd.Timestamp(ts)
    if t.hour == 0 and t.minute == 0 and t.second == 0:
        return t.strftime("%Y-%m-%d")
    return t.strftime("%Y-%m-%d %H:%M")


def _f(value: Any, ndigits: int = 4) -> float:
    """Round a numpy/pandas/native number to a plain Python float."""
    try:
        if value is None:
            return 0.0
        return round(float(value), ndigits)
    except (TypeError, ValueError):
        return 0.0


class BacktestAdapter:
    """Translate a single ``BacktestResult`` into UI-ready payloads."""

    #: Schema version of ``to_all()``. The run ledger stores payloads keyed by
    #: this: a stored row may only be reconstructed when its major matches,
    #: so a breaking payload-shape change MUST bump it (PRD R1, gotcha §5.3).
    PAYLOAD_VERSION = 1

    def __init__(self, result: BacktestResult) -> None:
        self.result = result
        self._equity: pd.Series = result.equity
        self._position: pd.Series = result.position.fillna(0)
        self._candles: pd.DataFrame = result.candles
        self._metrics: dict[str, Any] = result.metrics
        self._capital: float = float(result.config.initial_capital)

    # ------------------------------------------------------------------
    # Metrics cards
    # ------------------------------------------------------------------

    def to_metrics(self) -> dict[str, Any]:
        m = self._metrics
        total_return = m.get("total_return", 0.0)
        pnl = self._capital * total_return
        return {
            "total_pnl": _f(pnl, 2),
            "total_return_pct": _f(total_return * 100, 2),
            "win_rate_pct": _f(m.get("win_rate", 0.0) * 100, 2),
            "max_drawdown_pct": _f(m.get("max_drawdown", 0.0) * 100, 2),
            "sharpe": _f(m.get("sharpe", 0.0), 2),
            "sortino": _f(m.get("sortino", 0.0), 2),
            "total_trades": int(m.get("num_trades", 0)),
            # Breakdown behind the two numbers above: win_rate only covers closed
            # trades, so a run with an open position says so explicitly.
            "closed_trades": int(m.get("closed_trades", 0)),
            "open_trades": int(m.get("open_trades", 0)),
            "winning_trades": int(m.get("winning_trades", 0)),
            "losing_trades": int(m.get("losing_trades", 0)),
            "realised_pnl": _f(m.get("realised_pnl", 0.0), 2),
            "avg_trade_pnl": _f(m.get("avg_trade_pnl", 0.0), 2),
            "best_trade_pnl": _f(m.get("best_trade_pnl", 0.0), 2),
            "worst_trade_pnl": _f(m.get("worst_trade_pnl", 0.0), 2),
            # Quant-grade risk extensions
            "profit_factor": _f(m.get("profit_factor", 0.0), 4),
            "expectancy": _f(m.get("expectancy", 0.0), 2),
            "gross_profit": _f(m.get("gross_profit", 0.0), 2),
            "gross_loss": _f(m.get("gross_loss", 0.0), 2),
            "max_consecutive_losses": int(m.get("max_consecutive_losses", 0)),
            "avg_holding_bars": _f(m.get("avg_holding_bars", 0.0), 2),
            "var_95_pct": _f(m.get("var_95", 0.0) * 100, 4),
            "es_95_pct": _f(m.get("es_95", 0.0) * 100, 4),
            "var_99_pct": _f(m.get("var_99", 0.0) * 100, 4),
            "es_99_pct": _f(m.get("es_99", 0.0) * 100, 4),
            # extras surfaced for richer cards
            "final_equity": _f(m.get("final_equity", self._equity.iloc[-1]), 2),
            "cagr_pct": _f(m.get("cagr", 0.0) * 100, 2),
            "volatility_pct": _f(m.get("volatility", 0.0) * 100, 2),
            "calmar": _f(m.get("calmar", 0.0), 2),
            "exposure_pct": _f(m.get("exposure", 0.0) * 100, 2),
            "initial_capital": _f(self._capital, 2),
            "bars": int(m.get("bars", len(self._equity))),
            # --- PRD §2.1 risk & tail ----------------------------------
            "omega": _f(m.get("omega", 0.0), 4),
            "skewness": _f(m.get("skewness", 0.0), 4),
            "kurtosis": _f(m.get("kurtosis", 0.0), 4),  # excess / Fisher
            "ulcer_index": _f(m.get("ulcer_index", 0.0), 4),
            "cvar_95_pct": _f(m.get("cvar_95", 0.0) * 100, 4),
            "var_95_inr": _f(m.get("var_95_inr", 0.0), 2),
            "cvar_95_inr": _f(m.get("cvar_95_inr", 0.0), 2),
            # --- PRD §2.1 drawdown detail ------------------------------
            "max_drawdown_duration_days": int(m.get("max_drawdown_duration_days", 0)),
            "max_drawdown_recovery_days": int(m.get("max_drawdown_recovery_days", 0)),
            "max_drawdown_recovered": bool(m.get("max_drawdown_recovered", True)),
            "time_in_drawdown_pct": _f(m.get("time_in_drawdown_pct", 0.0), 2),
            "drawdowns_over_10pct": int(m.get("drawdowns_over_10pct", 0)),
            "drawdown_episodes": int(m.get("drawdown_episodes", 0)),
            # --- PRD §2.1 trade quality --------------------------------
            "expectancy_inr": _f(m.get("expectancy_inr", m.get("expectancy", 0.0)), 2),
            "payoff_ratio": _f(m.get("payoff_ratio", 0.0), 4),
            "max_consecutive_wins": int(m.get("max_consecutive_wins", 0)),
            "avg_trade_duration_bars": _f(m.get("avg_trade_duration_bars", 0.0), 2),
            "median_trade_duration_bars": _f(m.get("median_trade_duration_bars", 0.0), 2),
            # --- PRD §2.1 statistical confidence ------------------------
            # Shipped as a STRING on purpose: the UI branches on it to raise the
            # "fewer than 20 closed trades" banner, and a truthy/falsy boolean
            # would lose the `warn` middle state the PRD defines.
            "trade_count_flag": str(m.get("trade_count_flag", "insufficient")),
            "sharpe_std_error": _f(m.get("sharpe_std_error", 0.0), 4),
            "sharpe_ci_low": _f(m.get("sharpe_ci_low", 0.0), 2),
            "sharpe_ci_high": _f(m.get("sharpe_ci_high", 0.0), 2),
        }

    # ------------------------------------------------------------------
    # Benchmark (PRD §3.1)
    # ------------------------------------------------------------------

    def to_benchmark(self) -> dict[str, Any]:
        """Buy-and-hold reference: its own metrics, plus alpha and beta.

        The CURVE is not here — ``to_equity()`` already ships it for the chart
        overlay, and a second copy would double the payload's largest array.
        """
        cached = self.__dict__.get("_benchmark_cache")
        if cached is not None:
            return cached
        block = build_benchmark(
            self._candles,
            self._equity,
            self._capital,
            float(self.result.config.periods_per_year or 0),
            # Take the strategy's return from the metrics the cards already
            # show, so alpha cannot disagree with them.
            strategy_total_return=float(self._metrics.get("total_return", 0.0)),
        )
        self.__dict__["_benchmark_cache"] = block
        return block

    def to_monte_carlo(self, simulations: int | None = None) -> dict[str, Any]:
        """Trade-sequence resampling (PRD §3.3) over the CLOSED trades.

        Same trade walk the cards and the table use, so the sample being
        resampled is the sample that was counted.
        """
        cached = self.__dict__.get("_monte_carlo_cache")
        if cached is not None:
            return cached
        trades = self.to_trades()
        pnls = [float(t.get("pnl", 0.0)) for t in trades if not t.get("is_open")]
        kwargs = {"simulations": simulations} if simulations else {}
        block = monte_carlo_trade_order(pnls, self._capital, **kwargs)
        self.__dict__["_monte_carlo_cache"] = block
        return block

    # ------------------------------------------------------------------
    # Equity curve (with buy & hold benchmark)
    # ------------------------------------------------------------------

    def _benchmark(self) -> pd.Series:
        close = self._candles["close"]
        first = close.iloc[0]
        if first <= 0:
            return pd.Series(self._capital, index=close.index)
        return self._capital * (close / first)

    def to_equity(self) -> dict[str, Any]:
        bench = self._benchmark().reindex(self._equity.index).ffill()
        return {
            "dates": [_fmt_dt(d) for d in self._equity.index],
            "values": [_f(v, 2) for v in self._equity.values],
            "benchmark": [_f(v, 2) for v in bench.values],
        }

    # ------------------------------------------------------------------
    # Drawdown
    # ------------------------------------------------------------------

    def to_drawdown(self) -> dict[str, Any]:
        dd = self._equity / self._equity.cummax() - 1
        worst = float(dd.min()) if len(dd) else 0.0
        worst_date = _fmt_dt(dd.idxmin()) if len(dd) else ""
        return {
            "dates": [_fmt_dt(d) for d in dd.index],
            "values": [_f(v, 4) for v in dd.values],
            "worst_dd_pct": _f(worst * 100, 2),
            "worst_dd_date": worst_date,
        }

    # ------------------------------------------------------------------
    # Trades (reconstructed from the position series)
    # ------------------------------------------------------------------

    def to_trades(self) -> list[dict[str, Any]]:
        """Rows for the trade table.

        These are the *same* trades :func:`~backtest.engine.metrics.compute_metrics`
        counted, via :func:`backtest.engine.trades.walk_trades` — so the "Trades"
        and "Win Rate" cards cannot drift from the table below them (gaps G1/G2),
        and each row's P&L is equity-based (costs included) rather than
        reconstructed from prices (gap G14).
        """
        cached = self.__dict__.get("_trades_cache")
        if cached is not None:
            return cached
        trades = walk_trades(self._equity, self._position, self._candles["close"])
        cached = [
            {
                "id": t.id,
                "date": t.entry_date,
                "exit_date": t.exit_date,
                "side": t.side,
                "entry": _f(t.entry, 2),
                "exit": _f(t.exit, 2),
                "pnl": _f(t.pnl, 2),
                "result": t.result,
                "is_open": t.is_open,
            }
            for t in trades
        ]
        self.__dict__["_trades_cache"] = cached
        return cached

    # ------------------------------------------------------------------
    # Price + signals markers
    # ------------------------------------------------------------------

    def to_signals(self) -> dict[str, Any]:
        pos = self._position
        close = self._candles["close"]

        candles_out = [
            {
                "date": _fmt_dt(idx),
                "open": _f(self._candles.loc[idx, "open"]),
                "high": _f(self._candles.loc[idx, "high"]),
                "low": _f(self._candles.loc[idx, "low"]),
                "close": _f(self._candles.loc[idx, "close"]),
            }
            for idx in self._candles.index
        ]
        buys: list[dict[str, Any]] = []
        sells: list[dict[str, Any]] = []
        prev = 0.0
        for idx in pos.index:
            cur = float(pos.loc[idx])
            price = float(close.loc[idx])
            d = _fmt_dt(idx)
            if cur > 0 and prev <= 0:
                buys.append({"date": d, "price": _f(price, 2)})
            elif (cur == 0 and prev > 0) or (cur < 0 and prev >= 0):
                sells.append({"date": d, "price": _f(price, 2)})
            prev = cur
        return {"candles": candles_out, "buys": buys, "sells": sells}

    # ------------------------------------------------------------------
    # Compare slot payload + full response
    # ------------------------------------------------------------------

    def to_compare(self) -> dict[str, Any]:
        m = self.to_metrics()
        return {
            "label": self._metrics.get("strategy", ""),
            "metrics": m,
            "equity": self.to_equity(),
            "drawdown": self.to_drawdown(),
            "total_return_pct": m["total_return_pct"],
            "win_rate_pct": m["win_rate_pct"],
            "max_drawdown_pct": m["max_drawdown_pct"],
            "sharpe": m["sharpe"],
            "total_trades": m["total_trades"],
        }

    def to_all(self) -> dict[str, Any]:
        m = self._metrics
        trades = self.to_trades()
        if trades:
            # Σ trade P&L must equal the equity the run actually produced: flat bars
            # move nothing, so the trade spans tile the whole curve (and each span
            # carries its own entry/exit costs). A gap here means the accounting and
            # the equity curve are being computed differently again.
            total_pnl = self._capital * float(m.get("total_return", 0.0))
            summed = sum(t["pnl"] for t in trades)
            if abs(summed - total_pnl) > max(1.0, 0.005 * abs(self._capital)):
                log.warning(
                    "[adapter] trades don't reconcile for %s/%s: Σpnl=%.2f vs total_pnl=%.2f",
                    m.get("strategy"),
                    m.get("symbol"),
                    summed,
                    total_pnl,
                )
        log.debug(
            "[adapter] %s/%s → %d trades (%d closed, %d open), win_rate=%.2f%% over closed",
            m.get("strategy"),
            m.get("symbol"),
            m.get("num_trades"),
            m.get("closed_trades", 0),
            m.get("open_trades", 0),
            100.0 * float(m.get("win_rate", 0.0)),
        )
        return {
            "config": {
                "strategy": m.get("strategy", ""),
                "symbol": m.get("symbol", ""),
                "capital": _f(self._capital, 2),
                "stop_loss": m.get("stop_loss"),
                "take_profit": m.get("take_profit"),
                "bars": int(m.get("bars", len(self._equity))),
                "strategy_params": m.get("strategy_params"),
            },
            "metrics": self.to_metrics(),
            # PRD §3: the single-run checks ride on the SAME payload as the
            # cards they qualify — a separate fetch could disagree with the
            # numbers it is commenting on.
            "benchmark": self.to_benchmark(),
            "monte_carlo": self.to_monte_carlo(),
            "equity": self.to_equity(),
            "drawdown": self.to_drawdown(),
            "trades": trades,
            "signals": self.to_signals(),
        }
