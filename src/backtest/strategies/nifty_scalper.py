"""Nifty Scalper strategy (Card 00 invariants compliant).

Fast scalp momentum and pullback mean-reversion designed specifically for
liquid index instruments (e.g. NIFTY, BANKNIFTY) with strict stop-loss and profit target.
"""

from __future__ import annotations

import pandas as pd

from backtest.strategy.templates import EquityStrategy


class NiftyScalper(EquityStrategy):
    """Nifty Scalper — Fast EMA momentum + RSI filter with engine-enforced stops."""

    name = "nifty_scalper"
    eligible_instruments = ["NIFTY", "BANKNIFTY", "DEMO", "INFY"]
    # issues.txt S2: intraday equity segment link (spawn-form preselect).
    default_segment = "equity_intraday"
    description = (
        "Nifty Scalper — fast intraday index momentum/scalp using dual EMAs and RSI confirmation, "
        "enforcing strict stop-loss and take-profit targets."
    )
    description_sections = {
        'overview': (
            'Intraday NIFTY index scalper: dual-EMA momentum with RSI confirmation, built for'
            'short holds with strict per-trade exits.'
        ),
        'regime': (
            'Works in intraday sessions with a clear directional bias and follow-through after'
            'EMA crosses. Struggles in narrow, low-volatility ranges where crosses are frequent'
            'and RSI confirmation whipsaws.'
        ),
        'entry': (
            'Long when the fast EMA is above the slow EMA and RSI confirms strength (RSI above'
            'the long threshold), i.e. momentum and trend agree.'
        ),
        'construction': (
            'Trades the NIFTY index instrument directly (equity path), long only, intraday — flat'
            'by session end.'
        ),
        'exits_risk': (
            'Strict declared stop-loss and take-profit on every trade plus exit when the EMA/RSI'
            'signal turns off. Worst case per trade is the stop distance (plus slippage on fast'
            'bars).'
        ),
        'parameters': (
            'fast_ema=9, slow_ema=21, rsi_period=14, rsi_long_threshold=52. Designed for intraday'
            'timeframes (1-15 min).'
        ),
    }
    version = "1.0"
    author = "Trading Bot"

    params = {
        "fast_ema": {
            "default": 9,
            "min": 2,
            "max": 50,
            "type": "int",
            "label": "Fast EMA Period",
            "tooltip": "Fast trend EMA length.",
        },
        "slow_ema": {
            "default": 21,
            "min": 5,
            "max": 100,
            "type": "int",
            "label": "Slow EMA Period",
            "tooltip": "Slow trend EMA length.",
        },
        "rsi_period": {
            "default": 14,
            "min": 2,
            "max": 50,
            "type": "int",
            "label": "RSI Filter Period",
            "tooltip": "Momentum filter lookback.",
        },
        "rsi_long_threshold": {
            "default": 52,
            "min": 40,
            "max": 70,
            "type": "int",
            "label": "RSI Long Threshold",
            "tooltip": "RSI level required to enter long scalp.",
        },
    }

    # Strict risk management stops
    stop_loss = 0.015   # 1.5% stop loss
    take_profit = 0.030  # 3.0% take profit

    def entries(self, candles: pd.DataFrame) -> pd.Series:
        close = candles["close"]
        fast = close.ewm(span=int(self.fast_ema), adjust=False).mean()
        slow = close.ewm(span=int(self.slow_ema), adjust=False).mean()

        delta = close.diff()
        gains = delta.clip(lower=0)
        losses = -delta.clip(upper=0)
        avg_gain = gains.ewm(alpha=1 / int(self.rsi_period), adjust=False).mean()
        avg_loss = losses.ewm(alpha=1 / int(self.rsi_period), adjust=False).mean()
        rs = avg_gain / avg_loss.replace(0, pd.NA)
        rsi = (100 - (100 / (1 + rs))).fillna(50)

        # Bullish momentum cross with RSI confirmation
        # Shifted by 1 bar to respect no-lookahead
        cross_up = (fast > slow) & (fast.shift(1) <= slow.shift(1))
        rsi_valid = rsi >= float(self.rsi_long_threshold)

        return (cross_up & rsi_valid).fillna(False)

    def exits(self, candles: pd.DataFrame) -> pd.Series:
        close = candles["close"]
        fast = close.ewm(span=int(self.fast_ema), adjust=False).mean()
        slow = close.ewm(span=int(self.slow_ema), adjust=False).mean()
        cross_down = (fast < slow) & (fast.shift(1) >= slow.shift(1))
        return cross_down.fillna(False)
