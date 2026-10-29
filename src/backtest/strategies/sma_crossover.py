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
            'Classic trend-following baseline: stay long while the fast simple moving average'
            'sits above the slow one, flat otherwise.'
        ),
        'regime': (
            'Works in sustained directional trends where moves persist for many bars. Struggles'
            'badly in sideways, choppy markets — repeated crossovers whipsaw in and out around'
            'the averages.'
        ),
        'entry': (
            'Goes long on the bar where the fast SMA crosses (and stays) above the slow SMA;'
            'there is no additional filter or confirmation.'
        ),
        'construction': (
            'Single-instrument equity/index position, long only, fully invested while the signal'
            'is on. No leverage, no shorting.'
        ),
        'exits_risk': (
            'Exits when the fast SMA crosses back below the slow SMA — the exit is the mirror of'
            'the entry, so there is no fixed stop or target; worst case is the adverse move'
            'between crossovers, which can be large in a sharp reversal.'
        ),
        'parameters': (
            'fast=20 (fast SMA window), slow=50 (slow SMA window). Shorter windows trade more and'
            'whipsaw more; longer windows lag entries and exits.'
        ),
    }
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
