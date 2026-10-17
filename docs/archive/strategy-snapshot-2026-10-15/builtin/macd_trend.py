"""MACD trend strategy.

Signal semantics (classic equity series):

* ``1`` while the MACD line is above its signal line (bullish momentum
  regime),
* ``0`` otherwise.

MACD (fast EMA − slow EMA, vs. signal EMA of the difference) is a
medium-speed trend filter — slower than ``price_move``/``momentum_roc``,
faster than a 20/50 SMA cross — useful for stress-testing portfolio risk
across overlapping-but-not-identical entry timing. Deterministic,
pandas-only, no engine imports (plugin-safe).
"""

from __future__ import annotations

import pandas as pd

from backtest.strategy.base import Strategy


class MacdTrend(Strategy):
    """MACD trend — long while the MACD line leads its signal line."""

    name = "macd_trend"
    description = (
        "MACD trend-following — long while the MACD line is above its signal "
        "line, flat once it crosses back under."
    )
    version = "1.0"
    author = "Trading Bot"
    params = {
        "fast": {
            "default": 12,
            "min": 2,
            "max": 50,
            "type": "int",
            "label": "Fast EMA",
            "tooltip": "Fast EMA period of the MACD line.",
        },
        "slow": {
            "default": 26,
            "min": 5,
            "max": 200,
            "type": "int",
            "label": "Slow EMA",
            "tooltip": "Slow EMA period of the MACD line (should exceed Fast).",
        },
        "signal": {
            "default": 9,
            "min": 2,
            "max": 50,
            "type": "int",
            "label": "Signal EMA",
            "tooltip": "EMA period of the signal line.",
        },
    }

    def generate_signals(self, candles: pd.DataFrame) -> pd.Series:
        fast = int(self.fast)
        slow = int(self.slow)
        signal_period = int(self.signal)

        macd_line = (
            candles["close"].ewm(span=fast, adjust=False).mean()
            - candles["close"].ewm(span=slow, adjust=False).mean()
        )
        signal_line = macd_line.ewm(span=signal_period, adjust=False).mean()
        return (macd_line > signal_line).astype(int)
