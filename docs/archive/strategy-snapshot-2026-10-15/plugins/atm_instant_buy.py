"""ATM instant-buy option strategy — day-long price-fluctuation test instrument.

Spec (user request, 2026-09-24)
-------------------------------
Buy the ATM strike of the underlying (NIFTY / BANKNIFTY) INSTANTLY on the
first bar after deployment — no signal, no warmup — and then hold it for the
rest of the day so the book's mark-to-market P&L tracks the underlying's
price fluctuation bar by bar.

Contract adaptation
-------------------
* The strategy contract speaks *directional views*, so "instant entry" is an
  unconditional NEUTRAL view at full confidence (same pattern as
  ``immediate_entry``). With a fixed single-string expression ``type``
  (``"long_call"``) every direction — including NEUTRAL — maps to that one
  structure, so the first bar books the ATM call.
* Exit config comes from the runner expression (see ``exit_policy.py``). For
  an all-day hold spawn with ``exit.signal_flip=false``,
  ``exit.neutral_bars=0`` and ``exit.min_days_to_expiry=null`` so nothing
  closes the position before the session ends; expiry-day spawns ride to
  cash settlement.
* Deterministic: pure function of the candles. Passes the conformance
  battery. This is an execution-mechanics / P&L-observation instrument,
  not an edge.
"""

from __future__ import annotations

import pandas as pd

from backtest.strategy.base import Strategy
from backtest.strategy.intent import Direction, MarketView


class AtmInstantBuy(Strategy):
    """Buy the ATM option on the first bar and hold it for the day."""

    name = "atm_instant_buy"
    # 2026-09-24: instrument eligibility (spawn-form dropdown + create-API
    # enforcement). Index strategies trade the FNO index set.
    eligible_instruments = ["NIFTY", "BANKNIFTY"]
    description = (
        "Instant ATM buy: unconditional view on deploy — books the "
        "expression's fixed structure (e.g. long_call) at the ATM strike on "
        "the first bar and holds it, so MTM P&L mirrors the underlying's "
        "intraday fluctuation."
    )
    version = "1.0"
    author = "Trading Bot"

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
