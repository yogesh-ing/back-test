"""Strategy Template — copy this to create a new strategy.

Save as ``src/backtest/strategies/<your_strategy>.py`` or
``plugins/strategies/<your_strategy>.py`` and fill in the sections marked
with TODO comments.

The template follows the 2026-09-29 strategy contract which requires:
1. A descriptive ``name`` (unique, lowercase, no spaces)
2. A rich ``description`` explaining what the strategy does and when to use it
3. Properly typed ``params`` with min/max bounds and tooltips
4. Either ``entries()`` for equity strategies or ``generate_market_view()`` for options
5. Optional: ``version``, ``author``, ``eligible_instruments``, ``regime_vix_range``

Example usage:
    cp templates/strategy_template.py src/backtest/strategies/my_strategy.py
    # Edit my_strategy.py, then test:
    cd src && python -m pytest ../tests/strategies/test_my_strategy.py -q
"""

from __future__ import annotations

import pandas as pd
import numpy as np

from backtest.strategy.base import Strategy
from backtest.strategy.signal import Signal


class MyNewStrategy(Strategy):
    """TODO: Replace with your strategy name (PascalCase class name)."""

    # ------------------------------------------------------------------
    # REQUIRED: Unique identifier (lowercase, no spaces)
    # ------------------------------------------------------------------
    name = "my_new_strategy"

    # ------------------------------------------------------------------
    # REQUIRED: Rich description (what it does + when to use it)
    # This is now mandatory as of 2026-09-29 for better discoverability.
    # ------------------------------------------------------------------
    description = (
        "TODO: Write a clear description here. Explain the core logic "
        "(e.g., 'EMA crossover with RSI filter'), what market regime it "
        "works best in (trending/choppy), and any key parameters traders "
        "should understand before using it."
    )

    # ------------------------------------------------------------------
    # OPTIONAL but recommended
    # ------------------------------------------------------------------
    version = "1.0"
    author = "Your Name"

    # Instrument eligibility (None = any symbol; list = restricts to these)
    # Option strategies should declare: ["NIFTY", "BANKNIFTY"]
    eligible_instruments = None

    # VIX regime range this strategy is designed for
    # Example: regime_vix_range = (10, 20) for low-vol mean-reversion
    regime_vix_range = None

    # ------------------------------------------------------------------
    # PARAMETERS: Declare with schema form for dynamic UI forms
    # ------------------------------------------------------------------
    params = {
        # Example parameter with full schema
        "fast_period": {
            "default": 12,
            "min": 5,
            "max": 50,
            "type": "int",
            "label": "Fast EMA Period",
            "tooltip": "Period for the fast EMA. Lower values react faster but generate more false signals.",
        },
        "slow_period": {
            "default": 26,
            "min": 10,
            "max": 100,
            "type": "int",
            "label": "Slow EMA Period",
            "tooltip": "Period for the slow EMA. Higher values smooth out noise but lag more.",
        },
        # Legacy flat form still works (auto-expanded to schema)
        # "threshold": 0.5,
    }

    # ------------------------------------------------------------------
    # EQUITY STRATEGIES: Implement entries() / exits()
    # ------------------------------------------------------------------
    def entries(self, candles: pd.DataFrame) -> pd.Series:
        """Return a boolean Series where True = enter long.

        Args:
            candles: OHLCV DataFrame with columns [open, high, low, close, volume]

        Returns:
            Boolean Series aligned to candles.index
        """
        close = candles["close"]

        # Calculate indicators
        fast_ema = self.ema(close, int(self.fast_period))
        slow_ema = self.ema(close, int(self.slow_period))

        # Entry condition: fast crosses above slow
        entries = pd.Series(False, index=candles.index)
        for i in range(1, len(candles)):
            if fast_ema.iloc[i - 1] <= slow_ema.iloc[i - 1] and fast_ema.iloc[i] > slow_ema.iloc[i]:
                entries.iloc[i] = True

        return entries

    def exits(self, candles: pd.DataFrame) -> pd.Series | None:
        """Return a boolean Series where True = exit position.

        Return None to use default exit logic (signal flip).
        """
        # Example: exit on opposite crossover
        close = candles["close"]
        fast_ema = self.ema(close, int(self.fast_period))
        slow_ema = self.ema(close, int(self.slow_period))

        exits = pd.Series(False, index=candles.index)
        for i in range(1, len(candles)):
            if fast_ema.iloc[i - 1] >= slow_ema.iloc[i - 1] and fast_ema.iloc[i] < slow_ema.iloc[i]:
                exits.iloc[i] = True

        return exits

    # ------------------------------------------------------------------
    # OPTION STRATEGIES: Implement generate_market_view() instead
    # Uncomment and implement this for option strategies:
    # ------------------------------------------------------------------
    # from backtest.strategy.intent import Direction, MarketView
    # from decimal import Decimal
    #
    # def generate_market_view(self, candles: pd.DataFrame) -> MarketView | None:
    #     """Emit a directional view for options expression layer.
    #
    #     Returns None for no trade, or a MarketView with direction
    #     (BULLISH/BEARISH), confidence (0-1), and metadata.
    #     """
    #     if candles.empty or "close" not in candles.columns:
    #         return None
    #
    #     close = candles["close"].iloc[-1]
    #
    #     # Your logic here...
    #     direction = Direction.BULLISH  # or BEARISH
    #     confidence = 0.7
    #
    #     return MarketView(
    #         direction=direction,
    #         confidence=confidence,
    #         underlying="NIFTY",  # or from params
    #         spot_price=Decimal(str(close)),
    #         bar_timestamp=candles.index[-1],
    #         metadata={"strategy": self.name},
    #     )

    # ------------------------------------------------------------------
    # ALTERNATIVE: Override generate_signals() directly (legacy approach)
    # Only use this if entries/exits don't fit your strategy logic.
    # ------------------------------------------------------------------
    # def generate_signals(self, candles: pd.DataFrame) -> pd.Series:
    #     """Return a Series of {-1, 0, 1} signals.
    #
    #     -1 = short, 0 = flat, 1 = long
    #     """
    #     signals = pd.Series(0, index=candles.index, dtype=int)
    #
    #     # Your logic here...
    #
    #     return signals
