import pandas as pd

from backtest.strategy.base import Strategy


class BuyAndHold(Strategy):
    """Passive benchmark: buy on the first bar and hold to the end."""

    name = "buy_and_hold"
    description = (
        "Buy-and-hold benchmark — long from the first bar to the last. "
        "Useful as a baseline to compare active strategies against."
    )
    version = "1.0"
    author = "Trading Bot"
    params: dict = {}

    def generate_signals(self, candles: pd.DataFrame) -> pd.Series:
        return pd.Series(1, index=candles.index, dtype=int)
