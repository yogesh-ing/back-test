from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd

from backtest.engine.metrics import compute_metrics
from backtest.logging_config import get_logger

log = get_logger(__name__)


@dataclass
class BacktestConfig:
    initial_capital: float = 100_000.0
    commission_pct: float = 0.0003
    slippage_pct: float = 0.0005
    periods_per_year: int = 252
    stop_loss: float | None = None
    take_profit: float | None = None


@dataclass
class BacktestResult:
    equity: pd.Series
    returns: pd.Series
    position: pd.Series
    candles: pd.DataFrame
    config: BacktestConfig
    metrics: dict[str, Any]


class Backtester:
    def __init__(self, config: BacktestConfig | None = None) -> None:
        self.config = config or BacktestConfig()

    def run(self, candles: pd.DataFrame, signals: pd.Series) -> BacktestResult:
        if candles is None or len(candles) == 0:
            log.error("[engine] run() called with an empty candle frame")
            raise ValueError("candles frame is empty")
        if candles.index.duplicated().any():
            log.warning(
                "[engine] %d duplicate index entries in the candle frame",
                int(candles.index.duplicated().sum()),
            )

        target = pd.Series(signals, index=candles.index, copy=True)
        target = target.reindex(candles.index).fillna(0)
        dropped = len(signals) - len(target)
        if dropped > 0:
            log.warning("[engine] %d signal bars had no matching candle and were dropped", dropped)
        target = target.clip(-1, 1)

        risk_managed = self.config.stop_loss is not None or self.config.take_profit is not None
        log.debug(
            "[engine] %s path over %d bars (commission=%.5f slippage=%.5f stop=%s tp=%s)",
            "risk-managed" if risk_managed else "vectorized",
            len(candles),
            self.config.commission_pct,
            self.config.slippage_pct,
            self.config.stop_loss,
            self.config.take_profit,
        )
        if risk_managed:
            equity, returns, position = self._run_with_risk(candles, target)
        else:
            equity, returns, position = self._run_vectorized(candles, target)

        result = BacktestResult(
            equity=equity,
            returns=returns,
            position=position,
            candles=candles,
            config=self.config,
            metrics={},
        )
        result.metrics = compute_metrics(result)
        flat = abs(float(equity.iloc[-1] / self.config.initial_capital - 1)) < 1e-12
        log.debug(
            "[engine] done: equity %s → %s, in-market %.1f%% of bars%s",
            f"{self.config.initial_capital:,.2f}",
            f"{equity.iloc[-1]:,.2f}",
            100.0 * float((position.abs() > 0).mean()) if len(position) else 0.0,
            " — FLAT: equity never moved (no signals, or every entry was blocked)" if flat else "",
        )
        if flat:
            log.warning(
                "[engine] result is flat (equity == initial capital) — see the "
                "no-signal warning from backtest.runner for the cause"
            )
        return result

    def _run_vectorized(
        self, candles: pd.DataFrame, target: pd.Series
    ) -> tuple[pd.Series, pd.Series, pd.Series]:
        close = candles["close"]
        held = target.shift(1).fillna(0)
        gross = held * close.pct_change().fillna(0)
        turnover = held.diff().abs().fillna(0)
        costs = turnover * (self.config.commission_pct + self.config.slippage_pct)
        net = gross - costs
        equity = self.config.initial_capital * (1 + net).cumprod()
        position = held.copy()
        return equity, net, position

    def _run_with_risk(
        self, candles: pd.DataFrame, target: pd.Series
    ) -> tuple[pd.Series, pd.Series, pd.Series]:
        # Values unused below (rows are read in the loop); keep the lookups so a
        # malformed frame still fails fast on missing columns (F841, ticket #11).
        _ = (candles["close"], candles["high"], candles["low"])

        # Lag target signals to implement no-lookahead rule (Invariant 1)
        lagged_target = target.shift(1).fillna(0).clip(-1, 1)

        prev_held = 0.0
        entry_price = None
        prev_close = None
        blocked = False
        equity = self.config.initial_capital
        equity_curve = [equity]
        net_series = []
        position_series = []
        forced_exits = 0

        for idx, row in candles.iterrows():
            prev_close_value = prev_close if prev_close is not None else row["close"]

            # Get desired position from lagged signal (previous bar's signal)
            desired = float(lagged_target.loc[idx]) if idx in lagged_target.index else 0.0

            # Unblock when desired returns to 0
            if desired == 0:
                blocked = False
                want = 0
            else:
                want = 0 if blocked else desired

            # Check if position change is needed
            if want != prev_held:
                if want != 0:
                    entry_price = prev_close_value
                else:
                    entry_price = None

            held = want
            turnover = abs(held - prev_held)
            bar_cost = turnover * (self.config.commission_pct + self.config.slippage_pct)

            # Calculate bar return (using held before any forced exit changes it)
            r = 0.0
            if held != 0:
                end = row["close"]
                if held > 0:
                    stop = (
                        entry_price * (1 - self.config.stop_loss)
                        if self.config.stop_loss is not None
                        else None
                    )
                    target_price = (
                        entry_price * (1 + self.config.take_profit)
                        if self.config.take_profit is not None
                        else None
                    )
                    if stop is not None and row["low"] <= stop:
                        end = stop
                        exit_triggered_cost = True
                    elif target_price is not None and row["high"] >= target_price:
                        end = target_price
                        exit_triggered_cost = True
                    else:
                        exit_triggered_cost = False
                else:
                    stop = (
                        entry_price * (1 + self.config.stop_loss)
                        if self.config.stop_loss is not None
                        else None
                    )
                    target_price = (
                        entry_price * (1 - self.config.take_profit)
                        if self.config.take_profit is not None
                        else None
                    )
                    if stop is not None and row["high"] >= stop:
                        end = stop
                        exit_triggered_cost = True
                    elif target_price is not None and row["low"] <= target_price:
                        end = target_price
                        exit_triggered_cost = True
                    else:
                        exit_triggered_cost = False

                # Calculate return with held position (before forced exit changes it)
                r = held * (end / prev_close_value - 1)

                # Apply exit cost and reset position if stop/target was hit
                if exit_triggered_cost:
                    bar_cost += abs(held) * (self.config.commission_pct + self.config.slippage_pct)
                    held = 0
                    blocked = True
                    entry_price = None
                    forced_exits += 1

            net = r - bar_cost
            equity *= 1 + net
            equity_curve.append(equity)
            net_series.append(net)
            position_series.append(held)

            prev_close = row["close"]
            prev_held = held

        equity_series = pd.Series(equity_curve[1:], index=candles.index)
        position_series = pd.Series(position_series, index=candles.index)
        log.debug(
            "[engine] risk-managed path: %d forced stop/target exits over %d bars",
            forced_exits,
            len(candles),
        )
        return equity_series, pd.Series(net_series, index=candles.index), position_series
