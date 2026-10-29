"""Immediate-entry option strategy — single-leg test instrument.

Spec (user request, 2026-09-18)
-------------------------------
Enter IMMEDIATELY on deployment/market open — no signal needed. Buy one
ATM option (side comes from the runner expression, e.g. ``"long_call"``),
exit at +₹5 profit or −₹5 loss (configured in the expression's exit block:
``take_profit_points`` / ``stop_loss_points``).

Contract adaptation
-------------------
* The strategy contract speaks *directional views*, so "immediate entry"
  is an unconditional NEUTRAL view at full confidence. A single-string
  ``type`` in the expression (``"long_call"``) maps EVERY direction to
  that one structure — so NEUTRAL buys the ATM call on the first bar.
* Deterministic: pure function of the candles. Passes the conformance
  battery. This is an execution-mechanics test instrument, not an edge.
"""

from __future__ import annotations

import pandas as pd

from backtest.strategy.base import Strategy
from backtest.strategy.intent import Direction, MarketView


class ImmediateEntry(Strategy):
    """Open the expression's structure on the first bar after deploy."""

    name = "immediate_entry"
    # 2026-09-22: instrument eligibility (spawn-form dropdown + create-API
    # enforcement). Index strategies trade the FNO index set.
    eligible_instruments = ["NIFTY", "BANKNIFTY"]
    description = (
        "Immediate single-leg entry: unconditional view on deploy — the "
        "expression's fixed structure (e.g. long_call) books on the first "
        "bar with the configured ₹ TP/SL."
    )
    version = "1.0"
    author = "strategy-plan spec 2026-09-18"

    params = {
        "underlying": {
            "default": "NIFTY",
            "type": "str",
            "label": "Underlying",
            "tooltip": "Index the view applies to (NIFTY / BANKNIFTY).",
        },
    }

    def entries(self, candles: pd.DataFrame) -> pd.Series:
        """Equity-path fallback: always in the market (view is unconditional)."""
        return pd.Series(True, index=candles.index)

    def generate_market_view(self, candles: pd.DataFrame) -> MarketView | None:
        if candles is None or candles.empty or "close" not in candles.columns:
            return None

        spot = float(candles["close"].iloc[-1])
        if spot <= 0:
            return None

        return MarketView(
            direction=Direction.NEUTRAL,
            confidence=1.0,
            underlying=str(self.underlying).upper(),
            spot_price=spot,
            bar_timestamp=candles.index[-1],
        )
