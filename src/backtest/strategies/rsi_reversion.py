import pandas as pd

from backtest.strategy.base import Strategy


class RsiReversion(Strategy):
    name = "rsi_reversion"
    params = {"period": 14, "lower": 30, "exit_level": 55}

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
