"""Price-move breakout/breakdown strategy (equity).

Long when the close jumps more than ``threshold`` points over the close
``lookback`` bars ago; short when it drops more than ``threshold`` points;
flat otherwise and during the warmup.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from backtest.strategy.templates import EquityStrategy


class PriceMove(EquityStrategy):
    """Price-move breakout: directional on a thresholded lookback change."""

    name = "price_move"
    description = (
        "Breakout/breakdown on the change between the current close and the "
        "close `lookback` bars ago: long when the move exceeds `threshold` "
        "points, short when it falls below `-threshold`, flat otherwise."
    )
    description_sections = {
        "overview": (
            "Directional equity signal: long on a thresholded upward move over "
            "the lookback window, short on a thresholded downward move, flat "
            "during warmup and when the move stays inside the band."
        ),
        "regime": (
            "Trending, directional markets where a sustained move of more than "
            "`threshold` points over `lookback` bars signals a breakout. "
            "Choppy, sideways markets keep the signal flat."
        ),
        "entry": (
            "Long when close[i] - close[i-lookback] > threshold; short when "
            "close[i] - close[i-lookback] < -threshold; flat otherwise and for "
            "the first `lookback` bars (warmup)."
        ),
        "construction": (
            "Single-instrument equity/index position, long or short, fully "
            "invested while the signal is on. No leverage beyond the signal "
            "direction."
        ),
        "exits_risk": (
            "Flattens to neutral when the move re-enters the band (|move| <= "
            "threshold) or on a fresh opposite breakout. Hard stop/target are "
            "not part of this signal; manage risk at the runner/bracket level."
        ),
        "parameters": (
            "threshold=100.0 (breakout band in price points), lookback=5 "
            "(bars over which the move is measured)."
        ),
    }
    version = "1.0"
    author = "Trading Bot"
    instrument_type = "equity"
    params = {
        "threshold": {
            "default": 100.0,
            "min": 0.0,
            "max": 10000.0,
            "type": "float",
            "label": "Threshold (points)",
            "tooltip": "Minimum absolute price move over the lookback window "
            "required to emit a non-zero signal.",
        },
        "lookback": {
            "default": 5,
            "min": 1,
            "max": 500,
            "type": "int",
            "label": "Lookback (bars)",
            "tooltip": "Number of bars back to compare the current close against.",
        },
    }

    def generate_signals(self, candles: pd.DataFrame) -> pd.Series:
        close = candles["close"].to_numpy(dtype=float)
        n = len(close)
        out = np.zeros(n, dtype=np.int64)
        lookback = int(self.lookback)
        threshold = float(self.threshold)
        for i in range(lookback, n):
            move = close[i] - close[i - lookback]
            if move > threshold:
                out[i] = 1
            elif move < -threshold:
                out[i] = -1
        return pd.Series(out, index=candles.index, dtype=int)
