"""Price-move strategy — enter on upward momentum, exit on fixed price target/stop.

Logic
-----
* **Entry**: when the bar's close is at least ``move_amount`` above the
  previous bar's close (upward price movement).
* **Take-profit**: when the close is at least ``take_profit`` above the
  entry price.
* **Stop-loss**: when the close is at least ``stop_loss`` below the
  entry price.

Entry and exit prices are absolute (₹/$), not percentages, so the
strategy works across instruments with very different price levels.
"""

import pandas as pd

from backtest.strategy.base import Strategy


class PriceMove(Strategy):
    """Enter on upward price movement; exit on fixed price target or stop."""

    name = "price_move"
    description = (
        "Price-move momentum — enter long when the close rises by at least "
        "'Move Amount' from the previous bar. Exit on a fixed take-profit or "
        "stop-loss (absolute price, not percentage)."
    )
    version = "1.0"
    author = "Trading Bot"
    params = {
        "move_amount": {
            "default": 5.0,
            "min": 0.1,
            "max": 10000.0,
            "type": "float",
            "label": "Move Amount",
            "tooltip": (
                "Minimum upward price move (close vs previous close) to trigger an entry."
            ),
        },
        "take_profit": {
            "default": 5.0,
            "min": 0.1,
            "max": 10000.0,
            "type": "float",
            "label": "Take Profit",
            "tooltip": (
                "Exit when the price is at least this much above the entry price."
            ),
        },
        "stop_loss": {
            "default": 5.0,
            "min": 0.1,
            "max": 10000.0,
            "type": "float",
            "label": "Stop Loss",
            "tooltip": (
                "Exit when the price is at least this much below the entry price."
            ),
        },
    }

    def generate_signals(self, candles: pd.DataFrame) -> pd.Series:
        """Return 1 while in position, 0 otherwise."""
        closes = candles["close"]
        signals = pd.Series(0, index=candles.index, dtype=int)

        entry_price: float | None = None

        for i in range(1, len(closes)):
            current = float(closes.iloc[i])
            prev = float(closes.iloc[i - 1])

            if entry_price is None:
                # --- not in a position: look for entry -------------------
                if current - prev >= self.move_amount:
                    entry_price = current
                    signals.iloc[i] = 1
            else:
                # --- in a position: check take-profit / stop-loss --------
                if current - entry_price >= self.take_profit:
                    # take-profit hit — exit (signal stays 0)
                    entry_price = None
                elif entry_price - current >= self.stop_loss:
                    # stop-loss hit — exit (signal stays 0)
                    entry_price = None
                else:
                    signals.iloc[i] = 1

        return signals
