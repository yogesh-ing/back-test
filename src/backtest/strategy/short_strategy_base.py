"""ShortStrategyBase — shared skeleton for all short-premium option strategies.

Owns:
  * Exit contract (SL / TP / trailing / DTE square-off / time stop)
  * Feed-subscription ordering (entry tokens subscribed before fill)
  * Portfolio alert hooks (gamma critical, VIX regime change)
  * instrument_type = "options"

Subclasses declare ONLY:
  * name, version, author, description
  * opening_structure (structure, strike_selection, expiry_policy)
  * entry gating (generate_market_view override — NEUTRAL = always enter)
  * param overrides (default values for stop/target/trailing/etc.)

Never hardcode exit values in execution.exit — they are params.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from backtest.alerts.types import AlertType
from backtest.strategy.intent import Direction, MarketView
from backtest.strategy.templates import StructureStrategy


class ShortStrategyBase(StructureStrategy):
    """Base class for short-premium option selling strategies.

    instrument_type = "options" — spawn form locks to Single Symbol +
    index underlyings; chain source is wired per broker.
    """

    instrument_type = "options"
    template = "structure"

    # --- Opening structure (declared by subclass) --------------------------
    # opening_structure = {
    #     "structure": "short_straddle",
    #     "strike_selection": "atm",
    #     "expiry_policy": "nearest",
    # }

    # --- Execution spec (declared by subclass via `execution` dict) --------
    # exit: { stop_loss_points, take_profit_points, signal_flip,
    #         min_days_to_expiry, ... }
    # squareoff_minutes_before: int  (declareable execution extra)

    # --- Params schema (shared, subclass can override defaults) ------------
    params: dict[str, Any] = {
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
            "tooltip": "'pause' stops new entries until clear; 'exit' also closes if this runner is ≥50% of short gamma; 'ignore' does nothing.",
        },
    }

    subscribed_alerts = (
        AlertType.PORTFOLIO_GAMMA_CRITICAL.value,
        AlertType.VIX_REGIME_CHANGE.value,
    )

    # --- build_opening_intent (shared) -------------------------------------

    def build_opening_intent(self, view: MarketView | None) -> dict[str, Any] | None:
        """Assemble the opening intent from opening_structure + execution spec."""
        if view is None or not getattr(self, "opening_structure", {}):
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

    # --- generate_market_view (shared: NEUTRAL = unconditional entry) ------

    def generate_market_view(self, candles: pd.DataFrame) -> MarketView | None:
        """Unconditional entry: NEUTRAL view on every bar with valid spot.

        Subclasses that need entry gating (VIX, DTE regime, delta filter)
        override this and return None when conditions are not met.
        """
        if candles is None or candles.empty or "close" not in candles.columns:
            return None
        spot = float(candles["close"].iloc[-1])
        if spot <= 0:
            return None
        return MarketView(
            direction=Direction.NEUTRAL,
            confidence=1.0,
            underlying=str(getattr(self, "underlying", "NIFTY")).upper(),
            spot_price=spot,
            bar_timestamp=candles.index[-1],
        )

    # --- entries (equity-path fallback) ------------------------------------

    def entries(self, candles: pd.DataFrame):
        """Equity-path fallback: always in the market."""
        return pd.Series(True, index=candles.index)

    # --- portfolio alert hooks --------------------------------------------

    def _pause_reasons(self) -> set:
        return self.__dict__.setdefault("_pause_reason_set", set())

    def _set_pause(self, reason: str, on: bool) -> None:
        reasons = self._pause_reasons()
        if on:
            reasons.add(reason)
        else:
            reasons.discard(reason)
        self.pause_new_entries = bool(reasons)

    def on_alert(self, alert_type: str, alert_data: dict) -> str | None:
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

    def on_alert_resolved(self, alert_type: str, alert_data: dict) -> None:
        if alert_type == AlertType.PORTFOLIO_GAMMA_CRITICAL:
            self._set_pause("gamma", False)
