"""Swing channel-breakout strategy (daily or weekly bars, Turtle-style).

Enter when the close breaks above the highest high of the previous N bars
(shifted by one so today's own high can't trigger itself); exit when the
close drops below the lowest low of the previous M bars (M < N, so the
profit exit is tighter than the entry channel — the classic Donchian
long-stop). A volatility floor (ATR% of price) skips dead money in
sub-1%-range names where breakout follow-through is noise.

Why it should work: index-adjacent large caps make multi-week directional
moves on breakout from compression; a long-only channel exit gives back
some profit but never cuts a trend early.

Gotchas: worst enemy is whipsaw in range-bound months — the ATR floor and
a full-window test (2020-2026 includes two big chop periods) are the
hedge. Weekly use needs N≈40 weeks, daily N≈55 is the Turtle default.
"""

from __future__ import annotations

import pandas as pd

from backtest.strategy.templates import EquityStrategy


class SwingDonchianBreakout(EquityStrategy):
    name = "swing_donchian_breakout"
    description = (
        "Daily/weekly swing: long entry on an N-bar high breakout with an "
        "ATR%% volatility floor; exit on the slower M-bar low break. "
        "Classic Turtle trend-following, long-only."
    )
    description_sections = {
        'overview': (
            'Daily/weekly Turtle-style breakout: buys a new entry_n-bar high (with a volatility'
            'floor) and exits on a slower channel break. Long-only swing trend-following.'
        ),
        'regime': (
            'Works in multi-week trends on daily bars — the classic Turtle habitat. Struggles'
            'through long consolidations: repeated 55-bar-high probes fail, and the wide 20-bar'
            'exit gives back open profit before triggering.'
        ),
        'entry': (
            'Close at a new entry_n-bar high AND the ATR% volatility floor met (no entries in'
            'dead, compressed ranges).'
        ),
        'construction': (
            'Single-instrument equity position, long only, held for days to weeks on daily/weekly'
            'bars.'
        ),
        'exits_risk': (
            'Exits when price breaks the exit_n-bar low — a trailing channel stop. No fixed stop'
            'or target: worst case is a breakout that reverses the full channel width; the ATR%'
            'floor keeps it out of the choppiest entries but does not cap loss.'
        ),
        'parameters': (
            'entry_n=55, exit_n=20, atr_pct_min=1.0 (% ATR floor). The 55/20 Turtle pairing is'
            'conservative; shorter entry windows trade more, earlier, and falser.'
        ),
    }
    version = "1.0"
    author = "quant-workshop"

    params = {
        "entry_n": {"default": 55, "type": "int", "min": 10, "max": 120,
                    "label": "Entry Channel", "tooltip": "Breakout lookback in bars"},
        "exit_n": {"default": 20, "type": "int", "min": 5, "max": 60,
                   "label": "Exit Channel", "tooltip": "Trailing breakdown lookback"},
        "atr_pct_min": {"default": 1.0, "type": "float", "min": 0.0, "max": 4.0,
                        "label": "Min ATR %", "tooltip": "Skip bars quieter than this"},
    }

    def entries(self, candles: pd.DataFrame) -> pd.Series:
        close = candles["close"]
        high = candles["high"]
        low = candles["low"]

        prior_high = high.rolling(self.entry_n).max().shift(1)
        breakout = close > prior_high

        prev_close = close.shift(1)
        tr = pd.concat(
            [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
        ).max(axis=1)
        atr = tr.ewm(alpha=1.0 / 14, adjust=False).mean()
        volatile = (atr / close) * 100.0 >= self.atr_pct_min

        return pd.Series(breakout & volatile, index=candles.index).fillna(False)

    def exits(self, candles: pd.DataFrame) -> pd.Series:
        close = candles["close"]
        low = candles["low"]
        prior_low = low.rolling(self.exit_n).min().shift(1)
        return pd.Series(close < prior_low, index=candles.index).fillna(False)
