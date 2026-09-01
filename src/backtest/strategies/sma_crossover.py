import pandas as pd

from backtest.strategy.base import Strategy


class SmaCrossover(Strategy):
    """Simple moving-average crossover: long when fast SMA is above slow SMA."""

    name = "sma_crossover"
    description = (
        "Moving-average crossover — long while the fast SMA is above the slow SMA, flat otherwise."
    )
    version = "1.0"
    author = "Trading Bot"
    params = {
        "fast": {
            "default": 20,
            "min": 2,
            "max": 100,
            "type": "int",
            "label": "Fast SMA",
            "tooltip": "Period of the fast moving average.",
        },
        "slow": {
            "default": 50,
            "min": 5,
            "max": 250,
            "type": "int",
            "label": "Slow SMA",
            "tooltip": "Period of the slow moving average (should exceed Fast).",
        },
    }

    def generate_signals(self, candles: pd.DataFrame) -> pd.Series:
        fast = candles["close"].rolling(self.fast).mean()
        slow = candles["close"].rolling(self.slow).mean()
        return (fast > slow).astype(int)
