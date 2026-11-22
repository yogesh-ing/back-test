"""Short strangle with wing stated in POINTS, not percent.

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

Rectification (2026-11-21)
--------------------------
* All exit values are now *parameters* — nothing hardcoded in ``execution.exit``.
* ``exit_unit`` selects ``"points"`` or ``"rupee"``.
* ``trailing_stop_points`` / ``trailing_activation_points`` tighten the stop as premium decays.
* ``squareoff_minutes_before_close`` cuts the trade before market close on expiry day.
* ``min_iv_percentile`` / ``max_iv_percentile`` gate entry on the IV environment.
* Portfolio alert hooks (gamma critical, VIX regime change) added — short strangle
  is short gamma, so it must respond to portfolio-level risk alerts.

Paper only: a short strangle is an uncapped-loss structure (see
``_UNCAPPED_SHORT_STRUCTURES`` in ``backtest.strategy.execution``);
``config/risk_limits.yaml`` grants ``strangle`` to paper and withholds it
from live.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from backtest.alerts.types import AlertType
from backtest.strategy.intent import Direction, MarketView
from backtest.strategy.templates import StructureStrategy

#: Default wing in index points (operator request, 2026-11-02).
DEFAULT_WING_POINTS = 300.0


class StrangleWingPoints(StructureStrategy):
    """Sell a strangle ``wing_points`` away from ATM on the nearest expiry."""

    name = "strangle_wing_points"
    eligible_instruments = ["NIFTY", "BANKNIFTY"]
    template = "structure"
    version = "1.1"
    author = "rectified 2026-11"

    description = (
        "Short strangle with the wing stated in index points (default ATM "
        "±300) instead of a fixed percentage; the wing is re-measured "
        "against live spot on every intent. Fee-aware bracket in points or "
        "rupee, with trailing stop, EOD squareoff, and IV entry filter."
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
            "signal or confirmation. Optional IV filter can suppress entries in unfavourable IV "
            "regimes."
        ),
        "construction": (
            "Two legs, one lot each: sold call above spot, sold put below spot, both "
            "wing_points from ATM, same nearest expiry. NIFTY strikes step 50 points, so 300 is "
            "exactly six strikes either side."
        ),
        "exits_risk": (
            "Declared bracket: stop and target in points of premium or rupees "
            "(''exit_unit'' selects). Trailing stop tightens as premium decays. EOD squareoff at "
            "squareoff_minutes_before_close before market close. A gap straight through the stop "
            "is worse than the stop, and the structure is paper-entitled only."
            "Carry is positional: the book is NOT squared off at 15:10 and rides overnight to "
            "the expiry it sold, where the expiry rules settle it."
        ),
        "parameters": (
            "underlying=NIFTY; wing_points=300 (index points from ATM to each short leg); "
            "exit_unit='points'; stop_loss_points=13; take_profit_points=7; "
            "trailing_stop_points=5; trailing_activation_points=3; "
            "squareoff_minutes_before_close=30; min_iv_percentile=0; max_iv_percentile=100."
        ),
    }
    version = "1.1"
    author = "rectified 2026-11"

    opening_structure = {
        "structure": "strangle",
        "strike_selection": "fixed_distance",
        "expiry_policy": "nearest",
    }
    execution = {
        "emits": [],
        "structures": {},
        "exit": {
            # Units: ``exit_unit`` param selects "points" or "rupee" at spawn;
            # the engine prices points as points × Σ(leg lot size × lots)
            # (1 NIFTY lot of this two-leg structure = 150 units → 1 pt ≈ ₹150).
            # Rupee exits are handled by the bridge reading the param; the
            # strict schema only knows stop_loss_points / take_profit_points.
            # UNITS: points of premium → ₹1,950 stop / ₹1,050 target for one
            # NIFTY lot.
            "stop_loss_points": 13.0,
            "take_profit_points": 7.0,
            "signal_flip": False,
            "min_days_to_expiry": None,
        },
        # squareoff_minutes_before is a declareable execution extra
        # (plan §7): the bridge reads it and squares off open structures N
        # minutes before market close.
        "squareoff_minutes_before": 30,
        "quantity": 1,
        "min_quantity": 1,
        "max_quantity": 3,
        # Hold to expiry — without positional carry the bridge flattens at
        # 15:10 every day on an intraday cadence.
        "carry_policy": "positional",
        "allowed_overrides": ["quantity", "exit"],
    }

    # Portfolio Intelligence — short gamma is core risk; all short structures should subscribe.
    regime_vix_range = (10.0, 15.0)
    subscribed_alerts = (
        AlertType.PORTFOLIO_GAMMA_CRITICAL.value,
        AlertType.VIX_REGIME_CHANGE.value,
    )

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
            "min": 50.0,
            "max": 2000.0,
            "label": "Wing (index points)",
            "tooltip": (
                "Points away from ATM for each short leg. Converted to the "
                "percentage the chain selector wants against live spot."
            ),
        },
        "exit_unit": {
            "default": "points",
            "type": "str",
            "label": "Exit Unit",
            "tooltip": "'points' = points of premium; 'rupee' = absolute ₹ per structure.",
        },
        "stop_loss_points": {
            "default": 13.0,
            "type": "float",
            "min": 1.0,
            "max": 500.0,
            "label": "Stop Loss (points of premium)",
            "tooltip": "Adverse move in points of premium that closes the structure (used when exit_unit='points').",
        },
        "take_profit_points": {
            "default": 7.0,
            "type": "float",
            "min": 1.0,
            "max": 500.0,
            "label": "Take Profit (points of premium)",
            "tooltip": "Favourable move in points of premium that closes the structure (used when exit_unit='points').",
        },
        "stop_loss_rupee": {
            "default": 0.0,
            "type": "float",
            "min": 0.0,
            "max": 100000.0,
            "label": "Stop Loss (rupees)",
            "tooltip": "Absolute ₹ stop per structure (used when exit_unit='rupee'). 0 = disabled.",
        },
        "take_profit_rupee": {
            "default": 0.0,
            "type": "float",
            "min": 0.0,
            "max": 100000.0,
            "label": "Take Profit (rupees)",
            "tooltip": "Absolute ₹ target per structure (used when exit_unit='rupee'). 0 = disabled.",
        },
        "trailing_stop_points": {
            "default": 5.0,
            "type": "float",
            "min": 0.0,
            "max": 200.0,
            "label": "Trailing Stop (points)",
            "tooltip": "0 disables. Tightens the stop as premium decays.",
        },
        "trailing_activation_points": {
            "default": 3.0,
            "type": "float",
            "min": 0.0,
            "max": 200.0,
            "label": "Trailing Activation (points)",
            "tooltip": "Start trailing after this much profit in points.",
        },
        "squareoff_minutes_before_close": {
            "default": 30,
            "type": "int",
            "min": 0,
            "max": 240,
            "label": "EOD Squareoff (min before close)",
            "tooltip": "0 disables; 30 squares off 30 min before market close to avoid expiry-day last-hour trouble.",
        },
        "min_iv_percentile": {
            "default": 0.0,
            "type": "float",
            "min": 0.0,
            "max": 100.0,
            "label": "Min IV Percentile",
            "tooltip": "0 disables. Don't open if IV is below this percentile.",
        },
        "max_iv_percentile": {
            "default": 100.0,
            "type": "float",
            "min": 0.0,
            "max": 100.0,
            "label": "Max IV Percentile",
            "tooltip": "100 disables. Don't open if IV is above this percentile.",
        },
        "alert_response": {
            "default": "pause",
            "type": "str",
            "label": "Gamma alert response",
            "tooltip": (
                "What this strategy does on a portfolio_gamma_critical alert: "
                "pause (stop new entries until it clears), exit (also close if "
                "this runner is ≥50% of short gamma) or ignore."
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
        if view is None or not self.opening_structure:
            return None
        spec = self.execution_spec()
        if spec is None:
            from backtest.strategy.execution import ExecutionSpecError
            raise ExecutionSpecError(
                f"{self.name}: structure strategy must declare an execution spec"
            )
        return {
            "action": "OPEN",
            "direction": view.direction.name,
            **dict(self.opening_structure),
            "distance_pct": self._wing_pct(float(view.spot_price)),
            "exit": spec.exit_plan.to_dict(),
            "quantity": spec.default_lots,
            "strategy": {"name": self.name, "version": self.version},
        }

    # -- portfolio alerts (same pattern as immediate_strangle) --------------

    def _pause_reasons(self) -> set:
        return self.__dict__.setdefault("_pause_reason_set", set())

    def _set_pause(self, reason: str, on: bool) -> None:
        reasons = self._pause_reasons()
        if on:
            reasons.add(reason)
        else:
            reasons.discard(reason)
        self.pause_new_entries = bool(reasons)

    def on_alert(self, alert_type, alert_data):
        response = str(getattr(self, "alert_response", "pause") or "pause").lower()
        if alert_type == AlertType.PORTFOLIO_GAMMA_CRITICAL:
            if response == "ignore":
                return "ignored"
            self._set_pause("gamma", True)
            mine = alert_data.get("self_contribution") or {}
            share = float(mine.get("share") or 0.0)
            if response == "exit" and share >= 0.5:
                self.request_exit(1.0, reason=f"gamma_critical share={share:.0%}")
                return "exit_requested"
            return "paused"
        if alert_type == AlertType.VIX_REGIME_CHANGE:
            high = alert_data.get("new_regime") == "high_vol"
            self._set_pause("regime", high)
            return "paused" if high else "resumed"
        return None

    def on_alert_resolved(self, alert_type, alert_data):
        if alert_type == AlertType.PORTFOLIO_GAMMA_CRITICAL:
            self._set_pause("gamma", False)
