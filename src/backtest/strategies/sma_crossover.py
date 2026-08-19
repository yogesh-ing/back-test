import pandas as pd

from backtest.strategy.base import Strategy


class SmaCrossover(Strategy):
    name = "sma_crossover"
    params = {"fast": 20, "slow": 50}

    def generate_signals(self, candles: pd.DataFrame) -> pd.Series:
        fast = candles["close"].rolling(self.fast).mean()
        slow = candles["close"].rolling(self.slow).mean()
        return (fast > slow).astype(int)
