"""Immediate short-strangle strategy — deployed-at-open test instrument.

Spec (user request, 2026-09-18)
-------------------------------
Enter IMMEDIATELY on deployment/market open — no signal needed:
sell OTM call (+2%..3% above spot) + sell OTM put (2%..3% below spot),
same weekly expiry, 1 lot each. Exits: ₹100 take-profit / ₹100 stop-loss
(configured in the runner expression, not here).

Contract adaptation
-------------------
* The strategy contract only speaks *directional views*, so "immediate
  entry" is expressed as an unconditional NEUTRAL view at full confidence —
  the bridge opens the structure whatever the direction is, and the
  expression maps EVERY direction to ``short_strangle``.
* Deterministic: a pure function of the candles (needs ≥1 bar for spot).
  No clock, no randomness — passes the conformance battery.
* This is a mechanics test instrument (does the app book a short 2-leg
  credit structure end-to-end on live data), not an edge. Short strangles
  carry unlimited-loss risk — paper bucket only until risk-reviewed.
"""

from __future__ import annotations

import pandas as pd

from backtest.strategy.base import Strategy
from backtest.strategy.intent import Direction, MarketView


class ImmediateStrangle(Strategy):
    """Sell the 2%-OTM strangle the moment the runner is deployed."""

    name = "immediate_strangle"
    # 2026-09-22: instrument eligibility (spawn-form dropdown + create-API
    # enforcement). Index strategies trade the FNO index set.
    eligible_instruments = ["NIFTY", "BANKNIFTY"]
    description = (
        "Immediate short strangle: unconditional NEUTRAL view on deploy; "
        "expression maps every direction to short_strangle (~2% OTM legs)."
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
