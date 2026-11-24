"""Immediate short-strangle strategy — deployed-at-open test instrument.

Spec (user request, 2026-09-18)
-------------------------------
Enter IMMEDIATELY on deployment/market open — no signal needed:
sell OTM call (+2%..3% above spot) + sell OTM put (2%..3% below spot),
same weekly expiry, 1 lot each. Exits: a fee-aware bracket (₹1,000 take-profit /
₹2,000 stop-loss of structure P&L, configured in the runner expression / ExitPlan,
not here) — widened off the old ±₹100 because ±₹100 sat inside the ₹193
round-trip fee the engine itself charges, so every "win" was a net loss (BUG-02).

Rectification (2026-11-21)
--------------------------
* All exit values are now *parameters* — nothing hardcoded in ``execution.exit``.
* ``exit_unit`` selects ``"points"`` or ``"rupee"`` so option-selling strategies
  book in rupee terms (calculable upfront at entry) while option-buying
  strategies can keep percent-based exits. Declared as a *param* (not in the
  strict ``execution.exit`` schema) so the spawn form exposes it.
* ``trailing_stop_points`` / ``trailing_activation_points`` tighten the stop as
  premium decays. Declared as *params*.
* ``squareoff_minutes_before_close`` maps to ``squareoff_minutes_before`` at
  the top level of ``execution`` (a declareable extra) — cuts the trade before
  market close on expiry day (avoids last-hour liquidity crunch and assignment
  risk).
* ``min_iv_percentile`` / ``max_iv_percentile`` gate entry on the IV
  environment (don't sell into crushed IV). Declared as *params*.
* Portfolio alert hooks (gamma critical, VIX regime change) replicated here
  and to the sibling strangle plugins.

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

Portfolio alerts (2026-09-26)
-----------------------------
Reference implementation of a strategy that *listens* to the platform's
information layer (``docs/STRATEGY-ALERTS.md``). The platform never acts on
an alert; this strategy decides:

* ``portfolio_gamma_critical`` — short gamma is this structure's core risk.
  ``alert_response="pause"`` (default) stops opening new strangles until the
  alert resolves; ``"exit"`` additionally requests a full close **only if this
  runner contributes ≥50% of the portfolio's short gamma**; ``"ignore"``
  logs and carries on.
* ``vix_regime_change`` — a short strangle is designed for calm markets
  (``regime_vix_range = (10, 15)``). Entering a HIGH-volatility regime pauses
  new entries; leaving it lifts the pause.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from backtest.alerts.types import AlertType
from backtest.strategy.intent import Direction, MarketView
from backtest.strategy.templates import StructureStrategy

#: Contribution share above which ``alert_response="exit"`` requests a close.
EXIT_SHARE_MIN = 0.5


class ImmediateStrangle(StructureStrategy):
    """Sell the 2%-OTM strangle the moment the runner is deployed."""

    name = "old_immediate_strangle"
    # 2026-09-22: instrument eligibility (spawn-form dropdown + create-API
    # enforcement). Index strategies trade the FNO index set.
    eligible_instruments = ["NIFTY", "BANKNIFTY"]
    template = "structure"
    version = "1.2"
    author = "rectified 2026-11"

    description = (
        "Immediate short strangle: unconditional NEUTRAL view on deploy; "
        "expression maps every direction to short_strangle (~2% OTM legs). "
        "Fee-aware bracket in points or rupee, with trailing stop, EOD "
        "squareoff, and IV entry filter."
    )
    description_sections = {
        'overview': (
            'Immediate short strangle: on deploy it sells a strangle (~2% OTM call and put) to '
            'harvest premium from a range-bound underlying. It is a volatility-selling structure,'
            'not a directional view.'
        ),
        'regime': (
            'Works in calm, range-bound markets where realised volatility stays below what the '
            'sold premium priced in. Struggles violently in trending or event-driven markets — a '
            'fast one-sided move runs through a short leg and losses accelerate.'
        ),
        'entry': (
            'Unconditional: the entry intent fires on the first bar after deploy, with no signal '
            "condition. Optional IV filter (``min_iv_percentile`` / ``max_iv_percentile``) can "
            'suppress entries in unfavourable IV regimes. Timing the market regime is entirely '
            "the operator's job."
        ),
        'construction': (
            'Short strangle: simultaneously sold OTM call and OTM put (~2% away) on the same '
            'expiry, 1 lot by default (max 3). Short options carry margin requirements and '
            'theoretically uncapped risk on the call side.'
        ),
        'exits_risk': (
            'Fee-aware declared plan: combined stop and target in points of premium or rupees '
            '(''exit_unit'' selects). Trailing stop tightens as premium decays. EOD squareoff at '
            '``squareoff_minutes_before_close`` before market close. A short strangle\'s true '
            'worst case is far larger than the stop if price gaps through it — the pre-trade '
            'risk gate requires a declared worst case and the strangle entitlement before it '
            'will fill, and this strategy should only run where that entitlement is granted.'
        ),
        'parameters': (
            "underlying=NIFTY; wing_distance_pct=2.0; exit_unit='points'; "
            "stop_loss_points=13; take_profit_points=7; trailing_stop_points=5; "
            "trailing_activation_points=3; squareoff_minutes_before_close=30; "
            "min_iv_percentile=0; max_iv_percentile=100; "
            "alert_response='pause' (how the runner reacts to risk alerts)."
        ),
    }
    version = "1.2"
    author = "rectified 2026-11"

    # Phase 2 rewrite (plan §9.2): the opening trade is stated explicitly
    # here — a short strangle ~2% OTM each side — instead of being smuggled
    # through an unconditional NEUTRAL view + expression mapping. NEUTRAL
    # remains the strategy's (non-)opinion; this intent, by name, is what
    # opens. The stop/target live in the strategy's ExitPlan below.
    opening_structure = {
        "structure": "strangle",
        "strike_selection": "fixed_distance",
        "distance_pct": 2.0,
        "expiry_policy": "nearest",
    }
    execution = {
        "emits": [],
        "structures": {},
        "exit": {
            # Units: ``exit_unit`` param selects "points" or "rupee" at spawn.
            #   points: engine prices as points × Σ(leg lot size × lots) — one NIFTY lot of this
            #           two-leg structure = 150 units, so 1 pt ≈ ₹150.
            #   rupee:  absolute ₹ amount per structure (calculable upfront at entry).
            # BUG-02 (QA 2026-10-28): the old ±₹100 bracket was *inside* the
            # ₹193 round-trip fee the engine charges this structure, so every
            # "target" hit was a net loss and every stop fired on quote bounce.
            # A short strangle is a net-CREDIT structure: its net premium is
            # negative, and ``ExitPolicy`` deliberately skips percentage
            # stops/targets when the premium base is not positive (a percent of
            # a negative credit has no meaning). So the bracket has to be an
            # absolute one, expressed in stop_loss_points (or stop_loss_rupee
            # param for rupee-based selling strategies).
            "stop_loss_points": 13.0,
            "take_profit_points": 7.0,
            "signal_flip": False,
            "min_days_to_expiry": None,
        },
        # squareoff_minutes_before is a declareable execution extra (plan §7):
        # the bridge reads it and squares off open structures N minutes before
        # market close. Declared here so the strategy owns its time-stop.
        "squareoff_minutes_before": 30,
        "quantity": 1,
        "min_quantity": 1,
        "max_quantity": 3,
        # Hold to expiry unless squareoff fires. An intraday cadence alone
        # would otherwise flatten this book at 15:10 every day.
        "carry_policy": "positional",
        "allowed_overrides": ["quantity", "exit"],
    }
    # Portfolio Intelligence: designed for a calm (LOW-VIX) market.
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
            "tooltip": "Index the view applies to (NIFTY / BANKNIFTY).",
        },
        "wing_distance_pct": {
            "default": 2.0,
            "type": "float",
            "min": 0.5,
            "max": 10.0,
            "label": "Wing Distance (%)",
            "tooltip": "OTM distance for each short leg, as percent of spot.",
        },
        "exit_unit": {
            "default": "points",
            "type": "str",
            "label": "Exit Unit",
            "tooltip": "'points' = points of premium (engine prices as points × Σ lot size × lots); 'rupee' = absolute ₹ per structure.",
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

    # -- portfolio alerts ----------------------------------------------------

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
            if response == "exit" and share >= EXIT_SHARE_MIN:
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

    # -- strategy contract ---------------------------------------------------

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
            "distance_pct": float(getattr(self, "wing_distance_pct", 2.0)),
            "exit": spec.exit_plan.to_dict(),
            "quantity": spec.default_lots,
            "strategy": {"name": self.name, "version": self.version},
        }
