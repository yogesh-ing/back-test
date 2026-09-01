import pandas as pd

from backtest.strategy.base import Strategy


class DonchianBreakout(Strategy):
    """Donchian channel breakout: enter on a new high, exit on a new low."""

    name = "donchian_breakout"
    description = "Donchian channel breakout — enter long on a new lookback high, exit on a new lookback low. Risk-managed with a stop and target."
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
