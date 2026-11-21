"""NIFTY trigger long PE — buy ATM put when index close meets trigger level.

Index-triggered option strategy for the model-priced backtest sandbox.
Emits a BEARISH view when NIFTY close <= trigger_level, resolved to a long
ATM put through the execution spec. Configurable TP/SL via the API exits
block or the declared execution.exit plan.

Mechanics validator, not an edge — do not read performance into it.
"""

from __future__ import annotations

import pandas as pd

from backtest.strategy.intent import Direction, MarketView
from backtest.strategy.templates import InstantLongOptionStrategy


class NiftyTriggerLongPE(InstantLongOptionStrategy):
    """Buy ATM NIFTY put when index close meets the trigger level."""

    name = "nifty_trigger_long_pe"
    side = "PE"
    eligible_instruments = ["NIFTY"]
    description = (
        "Index-triggered long put: buys an ATM NIFTY put when the index "
        "close meets or falls below the trigger level, with configurable TP/SL."
    )
    description_sections = {
        "overview": (
            "Index trigger -> long ATM put. Fires on the first bar where "
            "close <= trigger_level, holds until stop/target/signal flip/"
            "expiry. Verifies index-triggered option plumbing end-to-end."
        ),
        "regime": (
            "Directional: profits when NIFTY falls past the trigger, loses "
            "premium (theta + IV) when it does not. Not an edge."
        ),
        "entry": (
            "One explicit long-put intent on the first bar where close <= "
            "trigger_level. No additional indicator, filter, or confirmation."
        ),
        "construction": (
            "Single ATM long put on the nearest eligible NIFTY expiry, 1 lot "
            "(override up to 5). Defined risk: premium paid is the max loss."
        ),
        "exits_risk": (
            "Stop/target as premium % (default SL 30%, TP 50%) plus signal "
            "flip and DTE square-off. Worst case is the full premium."
        ),
        "parameters": (
            "trigger_level (index close threshold), underlying=NIFTY, "
            "quantity 1-5 lots via override. Stop/target via API exits block."
        ),
    }
    version = "1.0"
    author = "operator request"

    execution = {
        "emits": ["bearish"],
        "structures": {
            "bearish": {"structure": "long_put", "strike_selection": "atm"},
        },
        "exit": {
            "structure_capped": True,
            "signal_flip": True,
            "neutral_bars": 0,
            "min_days_to_expiry": 1,
        },
        "quantity": 1,
        "min_quantity": 1,
        "max_quantity": 5,
        "allowed_overrides": ["quantity", "exit"],
    }

    params = {
        "trigger_level": {
            "default": 25000.0,
            "type": "float",
            "label": "Index trigger level",
            "tooltip": "NIFTY close level at or below which the long put fires.",
        },
        "underlying": {
            "default": "NIFTY",
            "type": "str",
            "label": "Underlying",
            "tooltip": "Index the view applies to (NIFTY / BANKNIFTY).",
        },
    }

    def entries(self, candles: pd.DataFrame) -> pd.Series:
        """Equity-path fallback: in the market when close <= trigger_level."""
        if candles is None or candles.empty or "close" not in candles.columns:
            return pd.Series(False, index=candles.index if candles is not None else [])
        return pd.Series(
            candles["close"] <= float(self.trigger_level), index=candles.index
        )

    def generate_market_view(self, candles: pd.DataFrame) -> MarketView | None:
        if candles is None or candles.empty or "close" not in candles.columns:
            return None
        spot = float(candles["close"].iloc[-1])
        if spot <= 0:
            return None
        trigger = float(self.trigger_level)
        if spot <= trigger:
            return MarketView(
                direction=Direction.BEARISH,
                confidence=1.0,
                underlying=str(self.underlying).upper(),
                spot_price=spot,
                bar_timestamp=candles.index[-1],
            )
        return None
