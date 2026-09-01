import pandas as pd

from backtest.strategy.base import Strategy


class RsiReversion(Strategy):
    """RSI mean-reversion: go long when RSI is oversold, exit on recovery."""

    name = "rsi_reversion"
    description = "RSI mean-reversion — enter long when RSI dips below the oversold level, exit when it recovers past the exit level."
    version = "1.0"
    author = "Trading Bot"
    params = {
        "period": {
            "default": 14,
            "min": 2,
            "max": 50,
            "type": "int",
            "label": "RSI Period",
            "tooltip": "Lookback window for the RSI calculation.",
        },
        "lower": {
            "default": 30,
            "min": 1,
            "max": 49,
            "type": "int",
            "label": "Oversold Level",
            "tooltip": "Enter long when RSI falls below this level.",
        },
        "exit_level": {
            "default": 55,
            "min": 50,
            "max": 90,
            "type": "int",
            "label": "Exit Level",
            "tooltip": "Exit the position when RSI rises above this level.",
        },
    }

    def generate_signals(self, candles: pd.DataFrame) -> pd.Series:
        delta = candles["close"].diff()
        gains = delta.clip(lower=0)
        losses = -delta.clip(upper=0)

        avg_gain = gains.ewm(alpha=1 / self.period, adjust=False).mean()
        avg_loss = losses.ewm(alpha=1 / self.period, adjust=False).mean()

        rs = avg_gain / avg_loss.replace(0, pd.NA)
        rsi = 100 - (100 / (1 + rs))
        rsi = rsi.fillna(50)

        signals = pd.Series(0, index=candles.index, dtype=int)
        held = False
        for i, value in rsi.items():
            if value < self.lower:
                held = True
                signals.loc[i] = 1
            elif value > self.exit_level:
                held = False
                signals.loc[i] = 0
            else:
                signals.loc[i] = 1 if held else 0
        return signals
