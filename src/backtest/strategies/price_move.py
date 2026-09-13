"""Price-move threshold strategy — signal when price moves more than a
fixed number of points within a lookback window.

The earliest built-in directional signal (referenced by the options Gap PRD,
which borrowed its ``threshold`` / ``lookback`` vocabulary for
``directional_options``). Emits the classic equity signal series:

* ``+1`` when ``close - close[lookback] >  threshold``  (bullish breakout)
* ``-1`` when ``close - close[lookback] < -threshold``  (bearish breakdown)
* ``0``  otherwise (no view)
"""

from __future__ import annotations

import pandas as pd

from backtest.strategy.base import Strategy


class PriceMove(Strategy):
    """Threshold-based price movement signal."""

    name = "price_move"
    description = (
        "Price-move threshold — long when the close rises more than `threshold` "
        "points over `lookback` bars, flat (or short, if allowed) when it falls "
        "by the same amount."
    )
    version = "1.0"
    author = "Trading Bot"
    params = {
        "threshold": {
            "default": 100.0,
            "min": 1.0,
            "max": 100000.0,
            "type": "float",
            "label": "Move Threshold (pts)",
            "tooltip": "Price change in points that triggers a signal.",
        },
        "lookback": {
            "default": 5,
            "min": 1,
            "max": 50,
            "type": "int",
            "label": "Lookback (bars)",
            "tooltip": "Bars to look back for the price change.",
        },
    }

    def generate_signals(self, candles: pd.DataFrame) -> pd.Series:
        threshold = float(self.threshold)
        lookback = int(self.lookback)

        price_change = candles["close"] - candles["close"].shift(lookback)

        signals = pd.Series(0, index=candles.index)
        signals[price_change > threshold] = 1    # Bullish breakout
        signals[price_change < -threshold] = -1  # Bearish breakdown
        return signals
