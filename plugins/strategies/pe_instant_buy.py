"""PE instant buy — buy the ATM put on the first bar, hold for the day.

Phase 2 rewrite (Refactor Plan v2.1 §9.4, MVP): the puts twin of
``ce_instant_buy``, replacing the NEUTRAL-smuggling half of the old
``atm_instant_buy`` family. Explicit BEARISH opening intent for an ATM
long put, declared once in the execution spec. See ``ce_instant_buy``
for the full rationale; snapshot of the predecessors lives in
``docs/archive/strategy-snapshot-2026-10-15``.
"""

from __future__ import annotations

import pandas as pd

from backtest.strategy.intent import Direction, MarketView
from backtest.strategy.templates import InstantLongOptionStrategy


class PeInstantBuy(InstantLongOptionStrategy):
    """Buy the ATM put instantly on deploy and hold it for the day."""

    name = "PE_instant_buy"
    side = "PE"
    eligible_instruments = ["NIFTY", "BANKNIFTY"]
    description = (
        "Instant ATM put buy: explicit long-put intent on the first bar, "
        "held for the day so MTM P&L mirrors the underlying's fluctuation. "
        "Replaces atm_instant_buy (puts half)."
    )
    description_sections = {
        'overview': (
            'Mechanics validator, not an edge: buys an ATM NIFTY put on the first bar and holds'
            'it for the day so live/paper plumbing (fills, MTM, charges) can be verified'
            'end-to-end.'
        ),
        'regime': (
            'There is no regime logic — it trades on every deploy, up days and down days alike.'
            'Expect it to make or lose roughly whatever an ATM put does that day; do not read'
            'performance into it.'
        ),
        'entry': (
            'One explicit long-put intent on the first bar after deploy. No indicator, filter, or'
            'confirmation of any kind.'
        ),
        'construction': (
            'Single ATM long put on the nearest eligible NIFTY expiry, 1 lot (override up to 5).'
            'Defined risk: the premium paid is the most the position itself can lose.'
        ),
        'exits_risk': (
            'Held for the day — no stop or target in the declared plan; the position rides to the'
            'session/day close unless the operator exits. Worst case is the full premium if the'
            'put expires worthless intraday-value-wise; the risk gate still prices and caps every'
            'entry.'
        ),
        'parameters': (
            'underlying=NIFTY. Quantity 1-5 lots via override. Use it to smoke-test execution,'
            'never as a return strategy.'
        ),
    }
    version = "2.0"
    author = "Trading Bot"

    execution = {
        "emits": ["bearish"],
        "structures": {
            "bearish": {"structure": "long_put", "strike_selection": "atm"},
        },
        "exit": {
            "structure_capped": True,  # long put: worst case = premium paid
            "signal_flip": False,
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
            direction=Direction.BEARISH,
            confidence=1.0,
            underlying=str(self.underlying).upper(),
            spot_price=spot,
            bar_timestamp=candles.index[-1],
        )
