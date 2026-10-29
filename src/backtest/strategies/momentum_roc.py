"""Rate-of-change momentum strategy.

Signal semantics (classic equity series, same vocabulary as ``price_move``):

* ``1`` when the N-bar rate of change exceeds ``threshold_pct`` % —
  momentum long (trend continuation).
* ``0`` otherwise (flat).

Deterministic, pandas-only, no engine imports (plugin-safe).
"""

from __future__ import annotations

import pandas as pd

from backtest.strategy.templates import EquityStrategy


class MomentumROC(EquityStrategy):
    """ROC momentum — long while N-bar rate of change stays above a floor."""

    name = "momentum_roc"
    description = (
        "Rate-of-change momentum — go long when the close's N-bar percent "
        "change exceeds the threshold, exit when momentum fades below it."
    )
    description_sections = {
        'overview': (
            'Rate-of-change momentum: holds the instrument only while its recent percent change'
            'clears a threshold — momentum on, risk on.'
        ),
        'regime': (
            'Works when strong moves continue (momentum persistence). Struggles at turning points'
            'and in range markets, where the percent change oscillates around the threshold and'
            'churns the position.'
        ),
        'entry': (
            "Enters long when the close's percent change over the lookback window exceeds the"
            'threshold.'
        ),
        'construction': (
            'Single-instrument equity/index position, long only, one position at a time.'
        ),
        'exits_risk': (
            'Exits when momentum fades back below the threshold. No hard stop: worst case is a'
            'reversal fast enough to carry price well below entry before the lookback window'
            'registers the fade.'
        ),
        'parameters': (
            'lookback=10 (bars for the percent change), threshold_pct=1.0 (minimum % change to be'
            'long). Higher thresholds take only the strongest moves, later.'
        ),
    }
    version = "1.0"
    author = "Trading Bot"
    params = {
        "lookback": {
            "default": 10,
            "min": 2,
            "max": 60,
            "type": "int",
            "label": "Momentum Lookback",
            "tooltip": "Bars over which the percent rate of change is measured.",
        },
        "threshold_pct": {
            "default": 1.0,
            "min": 0.1,
            "max": 50.0,
            "type": "float",
            "label": "Momentum Threshold %",
            "tooltip": "Minimum N-bar percent change to stay long.",
        },
    }

    def generate_signals(self, candles: pd.DataFrame) -> pd.Series:
        lookback = int(self.lookback)
        threshold = float(self.threshold_pct)

        roc = candles["close"].pct_change(periods=lookback) * 100.0
        return (roc > threshold).astype(int)
