"""Swing short-term mean-reversion (daily bars, Connors RSI(2) pattern).

Buy a 2-day washout inside a long-term uptrend: RSI(2) under the buy
threshold while close is still above the 200-SMA — statistically the
highest-probability retail-scale edge in liquid large caps. Take profit on
the snap-back: close crosses above the fast SMA (5-day by default), which
is the mean reversion completing, not a trend signal.

Why it should work: NSE large-caps overreact to 2-3 day flows (FII selling,
expiry noise) then revert within days; the SMA200 filter confines entries
to regimes where reversion has buying pressure behind it.

Gotchas: the filter that kills live versions — do NOT drop the trend
condition (RSI2 dips in a bear market keep dipping); holding periods are
days so delivery STT + brokerage matter, model them; and in 2020/2024-style
crash weeks it catches a falling knife once per regime — size accordingly.
"""

from __future__ import annotations

import pandas as pd

from backtest.strategy.base import Strategy


def _rsi(close: pd.Series, period: int) -> pd.Series:
    """Wilder RSI (ewm alpha=1/period), matching the canonical definition."""
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0.0, pd.NA)
    return 100.0 - 100.0 / (1.0 + rs)


class SwingRsi2MeanRev(Strategy):
    name = "swing_rsi2_meanrev"
    description = (
        "Daily swing mean-reversion: buy an RSI(2) washout below the "
        "threshold while price holds above the 200-SMA; exit when the close "
        "reclaims the fast SMA (the reversion is done)."
    )
    version = "1.0"
    author = "quant-workshop"

    params = {
        "rsi_period": {"default": 2, "type": "int", "min": 2, "max": 14,
                       "label": "RSI Period", "tooltip": "Short lookback is the point"},
        "rsi_buy": {"default": 10.0, "type": "float", "min": 0.0, "max": 30.0,
                    "label": "RSI Buy Below", "tooltip": "Washout threshold"},
        "sma_trend": {"default": 200, "type": "int", "min": 50, "max": 300,
                      "label": "Regime SMA", "tooltip": "Only fade dips in an uptrend"},
        "sma_exit": {"default": 5, "type": "int", "min": 2, "max": 30,
                     "label": "Exit SMA", "tooltip": "Reclaim of this mean = profit take"},
    }

    def entries(self, candles: pd.DataFrame) -> pd.Series:
        close = candles["close"]
        rsi = _rsi(close, int(self.rsi_period))
        sma_t = close.rolling(self.sma_trend).mean()
        washout = rsi < self.rsi_buy
        uptrend = close > sma_t
        return pd.Series(washout & uptrend, index=candles.index).fillna(False)

    def exits(self, candles: pd.DataFrame) -> pd.Series:
        close = candles["close"]
        sma_x = close.rolling(self.sma_exit).mean()
        return pd.Series(close > sma_x, index=candles.index).fillna(False)
