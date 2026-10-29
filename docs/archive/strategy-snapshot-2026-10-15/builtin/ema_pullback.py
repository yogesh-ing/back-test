"""Dual-EMA pullback strategy — trend filter plus entry timing.

Signal semantics (hold-state machine):

* regime: price is above a slow EMA (only trade with the trend),
* entry: the close dips to/below a fast EMA within that uptrend (a
  pullback), exit when the close closes back above a stretch multiple of
  the fast EMA or the trend breaks (close below the slow EMA).

Gives the portfolio a trend strategy with *patient* entries — combined
with ``macd_trend``/``momentum_roc`` it stresses the risk module's
max-open-positions and gross-exposure caps with staggered, overlapping
holds rather than simultaneous entries. Deterministic, pandas-only, no
engine imports (plugin-safe).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from backtest.strategy.base import Strategy


class EmaPullback(Strategy):
    """EMA pullback — buy dips to the fast EMA inside a slow-EMA uptrend."""

    name = "ema_pullback"
    description = (
        "Dual-EMA pullback — in a slow-EMA uptrend, enter long when price "
        "pulls back to the fast EMA; exit on a stretch-target or trend break."
    )
    version = "1.0"
    author = "Trading Bot"
    params = {
        "fast": {
            "default": 10,
            "min": 2,
            "max": 50,
            "type": "int",
            "label": "Fast EMA",
            "tooltip": "Pullback reference EMA (entry zone).",
        },
        "slow": {
            "default": 50,
            "min": 5,
            "max": 250,
            "type": "int",
            "label": "Slow EMA",
            "tooltip": "Regime filter — only long above this EMA.",
        },
        "stretch_pct": {
            "default": 1.0,
            "min": 0.2,
            "max": 10.0,
            "type": "float",
            "label": "Exit Stretch %",
            "tooltip": "Exit when close exceeds fast EMA by this percent.",
        },
    }

    def generate_signals(self, candles: pd.DataFrame) -> pd.Series:
        fast_n = int(self.fast)
        slow_n = int(self.slow)
        stretch = float(self.stretch_pct)

        fast_ema = candles["close"].ewm(span=fast_n, adjust=False).mean()
        slow_ema = candles["close"].ewm(span=slow_n, adjust=False).mean()

        # Vectorised hold-state machine (the naive .loc-per-bar loop costs
        # ~40 ms per 500-bar evaluation — over the 20 ms R-P1 budget):
        # entry = close in (fast, slow] band while below the slow regime EMA
        # (slow_v NaN-safe: comparisons with NaN are False, matching `continue`)
        close = candles["close"]
        entry = (close <= fast_ema) & (close > slow_ema)
        # exit while held = trend break or stretch target hit
        exit_cond = (close < slow_ema) | (close >= fast_ema * (1.0 + stretch / 100.0))

        held = False
        entries = entry.to_numpy(copy=True)
        exits = exit_cond.to_numpy(copy=True)
        out = np.zeros(len(candles), dtype=np.int64)
        for i in range(len(out)):
            if not held:
                if entries[i]:
                    held = True
                    out[i] = 1
            elif exits[i]:
                held = False
            else:
                out[i] = 1
        return pd.Series(out, index=candles.index, dtype=int)
