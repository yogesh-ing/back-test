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

from backtest.strategy.templates import EquityStrategy


class MacdTrend(EquityStrategy):
    """MACD trend — long while the MACD line leads its signal line."""

    name = "macd_trend"
    description = (
        "MACD trend-following — long while the MACD line is above its signal "
        "line, flat once it crosses back under."
    )
    description_sections = {
        'overview': (
            'MACD trend state machine: long while the MACD line is above its signal line, flat'
            'when it crosses back under.'
        ),
        'regime': (
            'Works in trending conditions where momentum persists after the crossover. Struggles'
            'in flat markets — MACD hovers around its signal line and generates whipsaw entries'
            'and exits.'
        ),
        'entry': (
            'Goes long when the MACD line crosses above the signal line and stays long for as'
            'long as it remains above.'
        ),
        'construction': (
            'Single-instrument equity/index position, long only, fully invested while the signal'
            'is on.'
        ),
        'exits_risk': (
            'Exits on the opposite MACD crossover. No fixed stop or target; worst case is the'
            'adverse drift between crossovers, largest at sharp V-shaped reversals.'
        ),
        'parameters': (
            'fast=12, slow=26, signal=9 — the classic MACD settings. Shorter windows react faster'
            'with more whipsaw; longer ones lag.'
        ),
    }
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
