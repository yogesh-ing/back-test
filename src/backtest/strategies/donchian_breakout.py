import pandas as pd

from backtest.strategy.base import Strategy


class DonchianBreakout(Strategy):
    name = "donchian_breakout"
    params = {"lookback": 20}
    stop_loss = 0.05
    take_profit = 0.10

    def entries(self, candles: pd.DataFrame) -> pd.Series:
        rolling_max = candles["close"].rolling(self.lookback).max().shift(1)
        return candles["close"] >= rolling_max

    def exits(self, candles: pd.DataFrame) -> pd.Series:
        rolling_min = candles["close"].rolling(self.lookback).min().shift(1)
        return candles["close"] <= rolling_min
