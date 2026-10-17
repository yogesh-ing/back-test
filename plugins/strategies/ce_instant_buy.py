"""CE instant buy — buy the ATM call on the first bar, hold for the day.

Phase 2 rewrite (Refactor Plan v2.1 §9.4, MVP): replaces the plugin half
of the old ``atm_instant_buy``, which emitted an unconditional NEUTRAL
view and secretly relied on a hand-edited expression to turn that into a
long call — the NIFTY/BANKNIFTY divergence incident. This strategy says
exactly what it does: an explicit BULLISH opening intent for an ATM long
call, declared once in its execution spec. NEUTRAL never opens anything
(plan §4.2); its twin is ``pe_instant_buy``.

Exit intent (author's, in the spec): the bought call's worst case is the
premium paid (``structure_capped``), no signal flip, no neutral-bar exit,
ride to settlement (``min_days_to_expiry: null``) — an execution-mechanics
/ P&L-observation instrument, not an edge. Deterministic: a pure function
of the candles, so it passes the conformance battery.
Snapshot of the predecessor: ``docs/archive/strategy-snapshot-2026-10-15``.
"""

from __future__ import annotations

import pandas as pd

from backtest.strategy.intent import Direction, MarketView
from backtest.strategy.templates import InstantLongOptionStrategy


class CeInstantBuy(InstantLongOptionStrategy):
    """Buy the ATM call instantly on deploy and hold it for the day."""

    name = "CE_instant_buy"
    side = "CE"
    eligible_instruments = ["NIFTY", "BANKNIFTY"]
    description = (
        "Instant ATM call buy: explicit long-call intent on the first bar, "
        "held for the day so MTM P&L mirrors the underlying's fluctuation. "
        "Replaces atm_instant_buy (calls half)."
    )
    version = "2.0"
    author = "Trading Bot"

    execution = {
        "emits": ["bullish"],
        "structures": {
            "bullish": {"structure": "long_call", "strike_selection": "atm"},
        },
        "exit": {
            "structure_capped": True,  # long call: worst case = premium paid
            "signal_flip": False,      # nothing flips a one-direction hold
            "neutral_bars": 0,
            "min_days_to_expiry": None,  # ride to settlement
        },
        "quantity": 1,
        "min_quantity": 1,
        "max_quantity": 5,
        "allowed_overrides": ["quantity"],
    }

    params = {
        "underlying": {
            "default": "NIFTY",
            "type": "str",
            "label": "Underlying",
            "tooltip": "Index the view applies to (NIFTY / BANKNIFTY).",
        },
    }

    def entries(self, candles: pd.DataFrame) -> pd.Series:
        """Equity-path fallback: always in the market (entry is unconditional)."""
        return pd.Series(True, index=candles.index)

    def generate_market_view(self, candles: pd.DataFrame) -> MarketView | None:
        if candles is None or candles.empty or "close" not in candles.columns:
            return None
        spot = float(candles["close"].iloc[-1])
        if spot <= 0:
            return None
        return MarketView(
            direction=Direction.BULLISH,
            confidence=1.0,
            underlying=str(self.underlying).upper(),
            spot_price=spot,
            bar_timestamp=candles.index[-1],
        )
