import pandas as pd

from backtest.strategy.templates import EquityStrategy


class DonchianBreakout(EquityStrategy):
    """Donchian channel breakout: enter on a new high, exit on a new low."""

    name = "donchian_breakout"
    description = (
        "Donchian channel breakout — enter long on a new lookback high, "
        "exit on a new lookback low. Risk-managed with a stop and target."
    )
    description_sections = {
        'overview': (
            'Channel breakout trend-follower: buys strength at a new lookback high and rides the'
            'move until the channel breaks the other way.'
        ),
        'regime': (
            'Works in trending markets with expanding ranges — breakouts that keep going.'
            'Struggles in tight ranges, where repeated false breakouts generate small consecutive'
            'losses.'
        ),
        'entry': (
            'Enters long when the close makes a new highest high over the lookback window (a'
            'Donchian channel breakout).'
        ),
        'construction': (
            'Single-instrument equity/index position, long only, one position at a time.'
        ),
        'exits_risk': (
            'Exits on a new lookback low (the trailing channel exit), which acts as a trailing'
            'stop once the trade moves in favour. Worst case is a failed breakout reversing'
            'through the entry before the exit triggers — no fixed stop or target caps it.'
        ),
        'parameters': (
            'lookback=20 (channel window for both entry highs and exit lows). Longer windows take'
            'fewer, later trades with wider effective stops.'
        ),
    }
    version = "1.0"
    author = "Trading Bot"
    params = {
        "lookback": {
            "default": 20,
            "min": 2,
            "max": 100,
            "type": "int",
            "label": "Lookback",
            "tooltip": "Channel window for the highest high / lowest low.",
        },
    }
    stop_loss = 0.05
    take_profit = 0.10

    def entries(self, candles: pd.DataFrame) -> pd.Series:
        rolling_max = candles["close"].rolling(self.lookback).max().shift(1)
        return candles["close"] >= rolling_max

    def exits(self, candles: pd.DataFrame) -> pd.Series:
        rolling_min = candles["close"].rolling(self.lookback).min().shift(1)
        return candles["close"] <= rolling_min
