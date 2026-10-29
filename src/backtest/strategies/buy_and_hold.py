import pandas as pd

from backtest.strategy.templates import EquityStrategy


class BuyAndHold(EquityStrategy):
    """Passive benchmark: buy on the first bar and hold to the end."""

    name = "buy_and_hold"
    description = (
        "Buy-and-hold benchmark — long from the first bar to the last. "
        "Useful as a baseline to compare active strategies against."
    )
    description_sections = {
        'overview': (
            'Passive benchmark: buys on the first bar and holds to the last bar of the run. Not'
            'an active strategy — a yardstick for everything else.'
        ),
        'regime': (
            'Works whenever the instrument drifts up over the test window. Struggles (by design)'
            'in bear markets and long sideways stretches, where it simply absorbs the full'
            'drawdown.'
        ),
        'entry': (
            'A single unconditional buy on the first available bar; no signal, filter, or timing'
            'logic of any kind.'
        ),
        'construction': (
            'Single-instrument equity/index position, long only, held for the whole run.'
        ),
        'exits_risk': (
            'No stop, no target, no exit rule: the position is closed only by the end of the'
            "backtest (or the session end in live intraday use). Worst case is the instrument's"
            'full peak-to-trough drawdown over the holding period.'
        ),
        'parameters': (
            'No parameters. Choose the instrument and date range to define the benchmark.'
        ),
    }
    version = "1.0"
    author = "Trading Bot"
    params: dict = {}

    def generate_signals(self, candles: pd.DataFrame) -> pd.Series:
        return pd.Series(1, index=candles.index, dtype=int)
