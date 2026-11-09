"""Short straddle at ATM — the two-sided sibling of the strangle books.

The ``long_straddle`` / ``short_straddle`` structure classes shipped in
Phase 4 (``backtest.options.structures``, wired into the bridge's
``_STRUCTURE_BY_NAME``) and are paper-entitled in
``config/risk_limits.yaml``, but no strategy ever named them: the live
catalogue only maps directions to ``long_call`` / ``long_put`` / the
strangle. So the structures were reachable in code and unreachable from
the spawn UI. This plugin is that missing name.

ATM by definition — no wing to state, so nothing here needs a percentage.
Short straddles are in ``_UNCAPPED_SHORT_STRUCTURES``, which is why the
exit plan carries an explicit points bracket instead of ``structure_capped``
(a capped exit would understate the loss the risk gate has to see).
"""

from __future__ import annotations

import pandas as pd

from backtest.strategy.intent import Direction, MarketView
from backtest.strategy.templates import StructureStrategy


class ShortStraddleAtm(StructureStrategy):
    """Sell the ATM call and ATM put on the nearest expiry at deploy."""

    name = "straddle_short_atm"
    eligible_instruments = ["NIFTY", "BANKNIFTY"]
    description = (
        "Immediate short straddle: sells the ATM call and ATM put on the "
        "nearest expiry, one lot each, on the first bar after deploy."
    )
    description_sections = {
        "overview": (
            "Sells both ATM legs on the same nearest expiry. It is a volatility-selling "
            "structure with no directional view, and the first end-to-end strategy name for "
            "the short-straddle class the engine already supports."
        ),
        "regime": (
            "Wants a range-bound session. Any move big enough to put one ATM leg deep in the "
            "money loses on that leg faster than the other leg and the net credit recover."
        ),
        "entry": (
            "Unconditional: the opening intent fires on the first bar after deploy with no "
            "signal, filter or confirmation."
        ),
        "construction": (
            "Two legs, one lot each: sold ATM call and sold ATM put, same expiry. Strike comes "
            "from the live chain's nearest ATM, never from a hardcoded ladder."
        ),
        "exits_risk": (
            "Declared bracket: 2000 points adverse stops the book, 1000 points favourable takes "
            "it — both outside the round-trip fee floor so a target is a real net win. The true "
            "worst case is unlimited; the stop is the only limit, and this structure is "
            "paper-entitled only. Carry is positional: the book is not flattened at 15:10 and "
            "holds overnight until its expiry."
        ),
        "parameters": "underlying=NIFTY.",
    }
    version = "1.0"
    author = "operator request 2026-11-02"

    opening_structure = {
        "structure": "short_straddle",
        "strike_selection": "atm",
        "expiry_policy": "nearest",
    }
    execution = {
        "emits": [],
        "structures": {},
        "exit": {
            # UNITS (2026-11-09): points of premium, which the engine prices as
            # points × Σ(leg lot size × lots) = 150 for one NIFTY lot of this
            # two-leg structure, i.e. the ₹1,950 stop / ₹1,050 target the book
            # has always meant. See ``immediate_strangle`` for why these used to
            # read 2000/1000 and what correct units did to that trade.
            "stop_loss_points": 13,
            "take_profit_points": 7,
            "signal_flip": False,
            "min_days_to_expiry": None,
        },
        "quantity": 1,
        "min_quantity": 1,
        "max_quantity": 3,
        # Hold to expiry — see the same declaration in ``strangle_wing_points``.
        # An intraday cadence alone would otherwise flatten this book at 15:10.
        "carry_policy": "positional",
        "allowed_overrides": ["quantity", "exit"],
    }

    params = {
        "underlying": {
            "default": "NIFTY",
            "type": "str",
            "label": "Underlying",
            "tooltip": "Index to sell the straddle on (NIFTY / BANKNIFTY).",
        },
    }

    def entries(self, candles: pd.DataFrame) -> pd.Series:
        """Equity-path fallback: always in the market (the view is unconditional)."""
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
