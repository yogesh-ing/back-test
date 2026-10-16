"""ATM Instant Buy - Immediately buys at-market on first bar, no conditions.

This strategy is designed for testing: it bypasses all technical indicators
and immediately generates a BUY signal (1) on every bar. Use it to verify
that the trading pipeline (signals -> orders -> execution) is working.

Usage:
    - Test live/paper trading setup
    - Verify broker connectivity
    - Check order execution flow
    - Debug why trades aren't being placed
"""

import pandas as pd
from backtest.strategy.base import Strategy


class AtmInstantBuy(Strategy):
    """🎯 atm_instant_buy - Immediate market entry for testing."""

    name = "atm_instant_buy"
    description = (
        "🎯 Instantly buys at market price on first bar. "
        "No conditions - always signals BUY (1). Used for testing the trading pipeline."
    )
    version = "1.0"
    author = "Trading Bot"

    # Can trade on any instrument
    eligible_instruments = None  # Trade any symbol

    params = {}

    def generate_signals(self, candles: pd.DataFrame) -> pd.Series:
        """Always return BUY signal (1) for every bar.

        This ensures immediate entry with no conditions.
        """
        # Return 1 (BUY) for all bars - instant entry
        return pd.Series(1, index=candles.index, dtype=int)
