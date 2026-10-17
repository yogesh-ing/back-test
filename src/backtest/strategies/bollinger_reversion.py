"""Bollinger-band mean-reversion strategy.

Signal semantics (hold-state machine like ``rsi_reversion``):

* enter long when the close closes **below** the lower band,
* exit when it recovers to the mid-band (SMA basis),
* stay flat otherwise.

Mean-reversion complements the momentum/trend built-ins for portfolio
diversification: it tends to be long exactly when trend strategies are flat.
Deterministic, pandas-only, no engine imports (plugin-safe).
"""

from __future__ import annotations

import pandas as pd

from backtest.strategy.templates import EquityStrategy


class BollingerReversion(EquityStrategy):
    """Bollinger mean-reversion — buy the lower band, exit at the mid-band."""

    name = "bollinger_reversion"
    description = (
        "Bollinger-band mean reversion — enter long when the close closes below "
        "the lower band, exit when it recovers to the mid-band."
    )
    description_sections = {
        'overview': (
            'Mean-reversion: buys when price closes below the lower Bollinger band on the'
            'assumption the stretch snaps back toward the mean.'
        ),
        'regime': (
            'Works in range-bound, oscillating markets where extremes revert. Struggles in strong'
            'breakouts and trending sell-offs, where price can ride the band down and keep'
            "falling ('catching a falling knife')."
        ),
        'entry': (
            'Enters long when the close crosses below the lower band (mean minus num_std standard'
            'deviations).'
        ),
        'construction': (
            'Single-instrument equity/index position, long only, one position at a time.'
        ),
        'exits_risk': (
            'Exits when the close recovers to the middle band (the moving average itself). There'
            'is no hard stop: worst case is a continued trend against the position, so size'
            'accordingly — the exit rule does not cap the loss.'
        ),
        'parameters': (
            'period=20 (band lookback), num_std=2.0 (band width in standard deviations). Wider'
            'bands trade less often but enter at more extreme stretches.'
        ),
    }
    version = "1.0"
    author = "Trading Bot"
    params = {
        "period": {
            "default": 20,
            "min": 5,
            "max": 100,
            "type": "int",
            "label": "Band Period",
            "tooltip": "Window for the SMA basis and standard deviation.",
        },
        "num_std": {
            "default": 2.0,
            "min": 0.5,
            "max": 4.0,
            "type": "float",
            "label": "Band Width (std dev)",
            "tooltip": "Number of standard deviations for the bands.",
        },
    }

    def generate_signals(self, candles: pd.DataFrame) -> pd.Series:
        period = int(self.period)
        num_std = float(self.num_std)

        mid = candles["close"].rolling(period).mean()
        std = candles["close"].rolling(period).std(ddof=0)
        lower = mid - num_std * std

        signals = pd.Series(0, index=candles.index, dtype=int)
        held = False
        for i in candles.index:
            close = candles["close"].loc[i]
            lo = lower.loc[i]
            basis = mid.loc[i]
            if pd.isna(lo) or pd.isna(basis):
                continue
            if not held and close < lo:
                held = True
                signals.loc[i] = 1
            elif held:
                if close >= basis:
                    held = False
                else:
                    signals.loc[i] = 1
        return signals
