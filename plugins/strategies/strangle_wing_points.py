"""Short strangle with a wing stated in POINTS, not percent.

Why this exists next to ``immediate_strangle``
---------------------------------------------
``immediate_strangle`` freezes its wing in the class-level
``opening_structure`` as ``distance_pct: 2.0``. On NIFTY (~₹25,900) 2% is
~518 points — 10 strikes of the 50-point ladder — while the operator asks
for "ATM ± 300", which is 6 strikes. The percentage is also *static*: as
spot moves the wing drifts with it, so a percent never pins a point count
for the rest of the session.

Here the wing is a parameter in points and is converted against the live
spot inside the view the strategy just emitted, so the legs stay ~300
points away from ATM for the whole session instead of ~300 points away
from wherever spot was at class-definition time.

The conversion belongs in the strategy, not the spawn payload:
``allowed_overrides`` is ``["quantity", "exit"]`` for structure strategies,
``_apply_selections`` refuses a ``type`` override outside that menu, and
``build_opening_intent`` spreads ``**opening_structure`` verbatim — so
``distance_pct`` has never been a spawn-time knob.

Paper only: a short strangle is an uncapped-loss structure (see
``_UNCAPPED_SHORT_STRUCTURES`` in ``backtest.strategy.execution``);
``config/risk_limits.yaml`` grants ``strangle`` to paper and withholds it
from live.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from backtest.strategy.intent import Direction, MarketView
from backtest.strategy.templates import StructureStrategy

#: Default wing in index points (operator request, 2026-11-02).
DEFAULT_WING_POINTS = 300.0


class StrangleWingPoints(StructureStrategy):
    """Sell a strangle ``wing_points`` away from ATM on the nearest expiry."""

    name = "strangle_wing_points"
    eligible_instruments = ["NIFTY", "BANKNIFTY"]
    description = (
        "Short strangle with the wing stated in index points (default ATM "
        "±300) instead of a fixed percentage; the wing is re-measured "
        "against live spot on every intent."
    )
    description_sections = {
        "overview": (
            "Sells an out-of-the-money call and put the same number of index points away from"
            "ATM on the nearest expiry. Built to test premium-selling plumbing at a wing the "
            "operator can state in points."
        ),
        "regime": (
            "Wants a quiet market. Realised moves larger than the wing run through a short leg "
            "and losses accelerate; the declared stop is the only thing limiting the book."
        ),
        "entry": (
            "Unconditional: the opening intent fires on the first bar after deploy with no "
            "signal or confirmation."
        ),
        "construction": (
            "Two legs, one lot each: sold call above spot, sold put below spot, both "
            "wing_points from ATM, same nearest expiry. NIFTY strikes step 50 points, so 300 is "
            "exactly six strikes either side."
        ),
        "exits_risk": (
            "Declared bracket: 2000 points adverse stops the book, 1000 points favourable takes "
            "it. Both sit outside the round-trip fee floor so a target is a real net win."
            "A gap straight through the stop is worse than the stop, and the structure is "
            "paper-entitled only."
            "Carry is positional: the book is NOT squared off at 15:10 and rides overnight to "
            "the expiry it sold, where the expiry rules settle it."
        ),
        "parameters": (
            "underlying=NIFTY; wing_points=300 (index points from ATM to each short leg)."
        ),
    }
    version = "1.0"
    author = "operator request 2026-11-02"

    opening_structure = {
        "structure": "strangle",
        "strike_selection": "fixed_distance",
        "expiry_policy": "nearest",
    }
    execution = {
        "emits": [],
        "structures": {},
        "exit": {
            # UNITS (2026-11-09): points of premium, which the engine prices as
            # points × Σ(leg lot size × lots) = 150 for one NIFTY lot of this
            # two-leg structure, i.e. the ₹1,950 stop / ₹1,050 target the book
            # has always meant. See ``immediate_strangle`` for the full note.
            "stop_loss_points": 13,
            "take_profit_points": 7,
            "signal_flip": False,
            "min_days_to_expiry": None,
        },
        "quantity": 1,
        "min_quantity": 1,
        "max_quantity": 3,
        # Hold to expiry. Without this the bridge's carry rule 4 falls through
        # to the runner timeframe, and these books run on an intraday cadence —
        # which would square off the whole book at 15:10 every day. No
        # ``intraday_squareoff_minutes_before`` is declared here, so positional
        # carry does not hit the bridge's both-declared refusal.
        "carry_policy": "positional",
        "allowed_overrides": ["quantity", "exit"],
    }

    params = {
        "underlying": {
            "default": "NIFTY",
            "type": "str",
            "label": "Underlying",
            "tooltip": "Index to sell the strangle on (NIFTY / BANKNIFTY).",
        },
        "wing_points": {
            "default": DEFAULT_WING_POINTS,
            "type": "float",
            "label": "Wing (index points)",
            "min": 50.0,
            "max": 2000.0,
            "tooltip": (
                "Points away from ATM for each short leg. Converted to the "
                "percentage the chain selector wants against live spot."
            ),
        },
    }

    def _wing_pct(self, spot: float) -> float:
        try:
            wing = float(getattr(self, "wing_points", DEFAULT_WING_POINTS))
        except (TypeError, ValueError):
            wing = DEFAULT_WING_POINTS
        if wing <= 0:
            wing = DEFAULT_WING_POINTS
        return wing / float(spot) * 100.0

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

    def build_opening_intent(self, view: MarketView | None) -> dict[str, Any] | None:
        if view is None or view.spot_price is None or float(view.spot_price) <= 0:
            return None
        spec = self.execution_spec()
        return {
            "action": "OPEN",
            "direction": view.direction.name,
            **dict(self.opening_structure),
            "distance_pct": self._wing_pct(float(view.spot_price)),
            "exit": spec.exit_plan.to_dict(),
            "quantity": spec.default_lots,
            "strategy": {"name": self.name, "version": self.version},
        }
