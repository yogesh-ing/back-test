"""Multi-day swing on hourly bars: Supertrend regime + EMA200 filter.

For symbols with 1-minute history resampled to 1-hour. Long entry when a
Supertrend(10, 3) flips bullish while the hourly close is above the 200-EMA
(a regime change inside a longer uptrend — this OR filter also folds in the
bear->bull flip itself, so no separate crossover series is needed). Exit
when Supertrend flips bearish, i.e. the trailing stop line is crossed;
typical hold is 2-10 sessions.

Why it should work: hourly candles cut the daily strategy's reaction lag
roughly in half on index-adjacent large caps, and Supertrend's ATR channel
lets the winner run further than a fixed-percentage stop would.

Gotchas: intraday gaps around expiry days can jump several ATRs — the
signal model has no intrabar stop, so treat the flip-exit as a *next bar*
exit; overnight positions pay delivery STT, not the 0.025% intraday rate,
which the costed mstock executor applies by holding time. Runs only on
symbols where hourly data exists (DB 1-minute coverage).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from backtest.strategy.base import Strategy


def _supertrend_dir(high: np.ndarray, low: np.ndarray, close: np.ndarray,
                    period: int, factor: float) -> np.ndarray:
    """Pine-consistent ta.supertrend direction: +1 bull, -1 bear."""
    n = len(close)
    tr = np.empty(n)
    tr[0] = high[0] - low[0]
    for i in range(1, n):
        tr[i] = max(high[i] - low[i], abs(high[i] - close[i - 1]),
                    abs(low[i] - close[i - 1]))
    atr = np.empty(n)
    atr[0] = tr[0]
    for i in range(1, n):
        atr[i] = (atr[i - 1] * (period - 1) + tr[i]) / period

    hl2 = (high + low) / 2.0
    upper = hl2 + factor * atr
    lower = hl2 - factor * atr
    direction = np.ones(n, dtype=int)
    for i in range(1, n):
        if not (upper[i] < upper[i - 1] or close[i - 1] > upper[i - 1]):
            upper[i] = upper[i - 1]
        if not (lower[i] > lower[i - 1] or close[i - 1] < lower[i - 1]):
            lower[i] = lower[i - 1]
        if direction[i - 1] == 1:
            direction[i] = -1 if close[i] < lower[i] else 1
        else:
            direction[i] = 1 if close[i] > upper[i] else -1
    return direction


class SwingHourlySupertrend(Strategy):
    name = "swing_hourly_supertrend"
    description = (
        "Hourly swing: long on a Supertrend(10,3) bull flip above the "
        "200-EMA regime filter; exit on the bear flip. Multi-day holds, "
        "needs 1-hour history (resampled from 1-minute bars)."
    )
    version = "1.0"
    author = "quant-workshop"

    params = {
        "st_period": {"default": 10, "type": "int", "min": 5, "max": 30,
                      "label": "Supertrend ATR", "tooltip": "ATR length of the channel"},
        "st_factor": {"default": 3.0, "type": "float", "min": 1.5, "max": 6.0,
                      "label": "Supertrend Factor", "tooltip": "Channel width in ATRs"},
        "ema_regime": {"default": 200, "type": "int", "min": 50, "max": 400,
                       "label": "Regime EMA", "tooltip": "Only flip-buy in this uptrend"},
    }

    def _direction(self, candles: pd.DataFrame) -> np.ndarray:
        return _supertrend_dir(
            candles["high"].values, candles["low"].values, candles["close"].values,
            int(self.st_period), float(self.st_factor),
        )

    def entries(self, candles: pd.DataFrame) -> pd.Series:
        close = candles["close"]
        direction = self._direction(candles)
        bull = np.asarray(direction == 1)
        bull_flip = np.zeros(len(candles), dtype=bool)
        bull_flip[1:] = bull[1:] & ~bull[:-1]
        regime = close > close.ewm(span=self.ema_regime, adjust=False).mean()
        return pd.Series(bull_flip & regime.values, index=candles.index).fillna(False)

    def exits(self, candles: pd.DataFrame) -> pd.Series:
        direction = self._direction(candles)
        return pd.Series(np.asarray(direction == -1), index=candles.index)
