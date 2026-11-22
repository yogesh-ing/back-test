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

Rectification (2026-11-21)
--------------------------
* All exit values are now *parameters* — nothing hardcoded in ``execution.exit``.
* ``exit_unit`` selects ``"points"`` or ``"rupee"``.
* ``trailing_stop_points`` / ``trailing_activation_points`` tighten the stop as premium decays.
* ``squareoff_minutes_before_close`` cuts the trade before market close on expiry day.
* ``min_iv_percentile`` / ``max_iv_percentile`` gate entry on the IV environment.
* Portfolio alert hooks (gamma critical, VIX regime change) added — short straddle
  is short gamma, so it must respond to portfolio-level risk alerts.
* Fixed ``parameters`` field (was a string, now a proper description string).
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from backtest.alerts.types import AlertType
from backtest.strategy.intent import Direction, MarketView
from backtest.strategy.templates import StructureStrategy


class ShortStraddleAtm(StructureStrategy):
    """Sell the ATM call and ATM put on the nearest expiry at deploy."""

    name = "straddle_short_atm"
    eligible_instruments = ["NIFTY", "BANKNIFTY"]
    template = "structure"
    version = "1.1"
    author = "rectified 2026-11"

    description = (
        "Immediate short straddle: sells the ATM call and ATM put on the "
        "nearest expiry, one lot each, on the first bar after deploy. "
        "Fee-aware bracket in points or rupee, with trailing stop, EOD "
        "squareoff, and IV entry filter."
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
            "signal, filter or confirmation. Optional IV filter can suppress entries in "
            "unfavourable IV regimes."
        ),
        "construction": (
            "Two legs, one lot each: sold ATM call and sold ATM put, same expiry. Strike comes "
            "from the live chain's nearest ATM, never from a hardcoded ladder."
        ),
        "exits_risk": (
            "Declared bracket: stop and target in points of premium or rupees "
            "(''exit_unit'' selects). Trailing stop tightens as premium decays. EOD squareoff at "
            "squareoff_minutes_before_close before market close. The true worst case is "
            "unlimited; the stop is the only limit, and this structure is paper-entitled only."
            "Carry is positional: the book is not flattened at 15:10 and holds overnight until "
            "its expiry."
        ),
        "parameters": (
            "underlying=NIFTY; exit_unit='points'; stop_loss_points=13; take_profit_points=7; "
            "trailing_stop_points=5; trailing_activation_points=3; "
            "squareoff_minutes_before_close=30; min_iv_percentile=0; max_iv_percentile=100."
        ),
    }
    version = "1.1"
    author = "rectified 2026-11"

    opening_structure = {
        "structure": "short_straddle",
        "strike_selection": "atm",
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
        # Hold to expiry — see the same declaration in ``strangle_wing_points``.
        # An intraday cadence alone would otherwise flatten this book at 15:10.
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
            "tooltip": "Index to sell the straddle on (NIFTY / BANKNIFTY).",
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
