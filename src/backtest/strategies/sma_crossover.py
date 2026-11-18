import numpy as np
import pandas as pd

from backtest.strategy.templates import EquityStrategy


class SmaCrossover(EquityStrategy):
    """Simple moving-average crossover: long when fast SMA is above slow SMA."""

    name = "sma_crossover"
    description = (
        "Moving-average crossover — long while the fast SMA is above the slow SMA, flat otherwise."
    )
    description_sections = {
        'overview': (
            'Classic trend-following baseline: stay long while the fast simple moving average '
            'sits above the slow one, flat otherwise.'
        ),
        'regime': (
            'Works in sustained directional trends where moves persist for many bars. Struggles '
            'badly in sideways, choppy markets — repeated crossovers whipsaw in and out around '
            'the averages.'
        ),
        'entry': (
            'Goes long on the bar where the fast SMA crosses (and stays) above the slow SMA;'
            'there is no additional filter or confirmation.'
        ),
        'construction': (
            'Single-instrument equity/index position, long only, fully invested while the signal '
            'is on. No leverage, no shorting.'
        ),
        'exits_risk': (
            'Exits when the fast SMA crosses back below the slow SMA, or when the price bracket '
            'fires: a hard stop at -1.5% of the entry price and a target at +4%, both editable '
            'at spawn. After a stop/target exit the strategy waits for a fresh crossover before '
            're-entering, so a stop-out is not instantly re-bought.'
        ),
        'parameters': (
            'fast=20 (fast SMA window), slow=50 (slow SMA window). Shorter windows trade more and '
            'whipsaw more; longer windows lag entries and exits. stop_loss_pct=1.5 and '
            'take_profit_pct=4.0 (% of entry price; set either to 0 to disable that side of the '
            'bracket).'
        ),
    }
    version = "1.1"
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
        "stop_loss_pct": {
            "default": 1.5,
            "min": 0.0,
            "max": 50.0,
            "type": "float",
            "label": "Stop Loss %",
            "tooltip": "Exit when close falls this percent below the entry price (0 = off).",
        },
        "take_profit_pct": {
            "default": 4.0,
            "min": 0.0,
            "max": 100.0,
            "type": "float",
            "label": "Take Profit %",
            "tooltip": "Exit when close rises this percent above the entry price (0 = off).",
        },
    }

    def generate_signals(self, candles: pd.DataFrame) -> pd.Series:
        fast = candles["close"].rolling(int(self.fast)).mean()
        slow = candles["close"].rolling(int(self.slow)).mean()
        above = (fast > slow).to_numpy()
        closes = candles["close"].to_numpy()
        sl = float(self.stop_loss_pct) / 100.0
        tp = float(self.take_profit_pct) / 100.0

        held = False
        # a bracket exit while the trend is still up must not re-enter on the
        # very next bar — re-arm only after a fresh cross
        wait_cross = False
        entry_price = 0.0
        out = np.zeros(len(candles), dtype=np.int64)
        for i in range(len(out)):
            if not held:
                if not above[i]:
                    wait_cross = False
                elif not wait_cross:
                    held = True
                    entry_price = closes[i]
                    out[i] = 1
            else:
                bracket_hit = (sl > 0 and closes[i] <= entry_price * (1.0 - sl)) or (
                    tp > 0 and closes[i] >= entry_price * (1.0 + tp)
                )
                if not above[i] or bracket_hit:
                    held = False
                    wait_cross = bracket_hit and above[i]
                else:
                    out[i] = 1
        return pd.Series(out, index=candles.index, dtype=int)
