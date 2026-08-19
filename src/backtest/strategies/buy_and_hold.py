import pandas as pd

from backtest.strategy.base import Strategy


class BuyAndHold(Strategy):
    name = "buy_and_hold"
    params = {}

    def generate_signals(self, candles: pd.DataFrame) -> pd.Series:
        return pd.Series(1, index=candles.index, dtype=int)
