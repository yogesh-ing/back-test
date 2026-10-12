"""Swing trend-pullback strategy (daily bars).

Buy dips in a confirmed uptrend: the long-term trend is up (close above the
200-day SMA and the fast EMA above the slow EMA), and price has pulled back
to touch the 20-EMA and closed back above it (a rejection of the dip).
Exit when the close breaks below the trend-EMA (the pullback became a
distribution) — no shorting, the platform equity model is long-only.

Why it should work: NSE large-caps trend for weeks once institutions start
accumulating; entering on the first EMA-touch after a fresh high keeps the
stop wide-but-defined and the reward anchored to trend continuation.

Gotchas: dead in choppy regimes (the SMA200 filter cuts most of that, the
EMA20-reclaim exit limits the rest); needs ≥220 bars of warmup, so short
windows report zero trades, not losses.
"""

from __future__ import annotations

import pandas as pd

from backtest.strategy.base import Strategy


class SwingEmaPullback(Strategy):
    name = "swing_ema_pullback"
    description = (
        "Daily swing: buy a pullback to the 20-EMA in a confirmed uptrend "
        "(close > 200-SMA, EMA20 > EMA50) when price reclaims the EMA; "
        "exit when the close loses the slow trend EMA."
    )
    version = "1.0"
    author = "quant-workshop"

    params = {
        "ema_fast": {"default": 20, "type": "int", "min": 5, "max": 60,
                     "label": "Pullback EMA", "tooltip": "EMA the dip is bought at"},
        "ema_slow": {"default": 50, "type": "int", "min": 20, "max": 120,
                     "label": "Trend EMA", "tooltip": "Exit line / trend structure"},
        "sma_trend": {"default": 200, "type": "int", "min": 50, "max": 300,
                      "label": "Regime SMA", "tooltip": "Long-term uptrend filter"},
    }

    def entries(self, candles: pd.DataFrame) -> pd.Series:
        close = candles["close"]
        low = candles["low"]
        ema_f = close.ewm(span=self.ema_fast, adjust=False).mean()
        ema_s = close.ewm(span=self.ema_slow, adjust=False).mean()
        sma_t = close.rolling(self.sma_trend).mean()

        uptrend = (close > sma_t) & (ema_f > ema_s)
        touched_ema = low <= ema_f * 1.002          # dip tagged the 20-EMA
        reclaimed = close > ema_f                   # closed back above it
        return pd.Series(uptrend & touched_ema & reclaimed, index=candles.index).fillna(False)

    def exits(self, candles: pd.DataFrame) -> pd.Series:
        close = candles["close"]
        ema_s = close.ewm(span=self.ema_slow, adjust=False).mean()
        return pd.Series(close < ema_s, index=candles.index).fillna(False)
