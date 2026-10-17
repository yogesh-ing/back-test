"""Momentum Burst option strategy — strategy-plan draft #1, adapted (2026-09-18).

Original draft (strategy-plan/Greeks Trigger Strategy.txt)
----------------------------------------------------------
"IF underlying price moves ±0.3% in the last 5 min THEN buy ATM call/put",
with delta/target/stop/time exits.

Adaptation to this system
-------------------------
* ENTRY only lives here: last bar's % move vs the threshold → BULLISH/BEARISH
  view; the OptionsBridge buys the ATM call/put. Exits are the Playbook's job
  (C2 layering) — set stop_loss_pct 0.20 / take_profit_pct 0.30 in the
  playbook expression, matching the draft's 20% SL / 30% target.
* The draft's 5-minute trigger assumes 5-min bars. Our real feed is 1-hour
  bars (G6), so ``lookback_bars`` defaults to 1: the last closed bar's move.
  The draft's delta-based exit needs per-position Greeks streaming — not in
  the V1 bridge; the playbook's premium-based SL/TP stands in.
* Determinism: pure function of the candle frame (no clock, no state).
"""

from __future__ import annotations

import pandas as pd

from backtest.strategy.templates import DirectionalOptionStrategy
from backtest.strategy.intent import Direction, MarketView


class MomentumBurst(DirectionalOptionStrategy):
    """Buy ATM call/put when the last bar moves ±threshold% (draft #1)."""

    name = "momentum_burst"
    # 2026-09-22: instrument eligibility (spawn-form dropdown + create-API
    # enforcement). Index strategies trade the FNO index set.
    eligible_instruments = ["NIFTY", "BANKNIFTY"]
    description = (
        "Draft #1 adapted: last-bar move beyond +-threshold% emits a "
        "BULLISH/BEARISH view (ATM long call/put via the playbook)."
    )
    description_sections = {
        'overview': (
            'Burst continuation: a single bar that moves beyond a percent threshold fires a'
            'same-direction options view (long call up, long put down).'
        ),
        'regime': (
            'Works in news/breakout tape where one decisive bar starts a multi-bar move.'
            'Struggles in spike-and-fade markets — by construction it buys after the burst,'
            'exactly where exhaustion reversals begin.'
        ),
        'entry': (
            "Last bar's move beyond ±move_threshold_pct (over lookback_bars) with sufficient"
            'confidence; anything smaller is silence.'
        ),
        'construction': (
            'Bullish view buys an at-the-money long call, bearish view an at-the-money long put'
            "on the underlying's nearest eligible expiry — a defined-risk, single-leg structure."
            'Size is 1 lot by default (override 1-5 lots where the spawn form allows).'
        ),
        'exits_risk': (
            'Declared exit plan: hard stop at -50% of premium paid, target at +100%, and exit'
            'when the directional signal flips. Entries are blocked inside 1 day of expiry. Worst'
            'case on a trade is the stop level (gap risk on illiquid strikes aside); a long'
            'option can never lose more than the premium paid.'
        ),
        'parameters': (
            'move_threshold_pct=0.3, lookback_bars=1, min_confidence=0.3, underlying=NIFTY. Size'
            'defaults to 1 lot; quantity (and, where listed, exit levels) can be overridden at'
            'spawn within the declared menu.'
        ),
    }
    version = "1.0"
    author = "strategy-plan draft 1"

    # Phase 2 rewrite (plan §9.2): the strategy's own execution declaration.
    # Signal logic below is unchanged from the snapshot; what changed is
    # that the direction->structure mapping and the exit contract now live
    # here, once, instead of in UI defaults / bridge fallbacks.
    execution = {
        "emits": ["bullish", "bearish", "neutral"],
        "structures": {
            "bullish": {"structure": "long_call", "strike_selection": "atm"},
            "bearish": {"structure": "long_put", "strike_selection": "atm"},
        },
        "exit": {
            "stop_loss_pct": 0.50,
            "take_profit_pct": 1.0,
            "signal_flip": True,
            "min_days_to_expiry": 1,
        },
        "quantity": 1,
        "min_quantity": 1,
        "max_quantity": 5,
        "allowed_overrides": ["quantity", "exit"],
    }

    params = {
        "move_threshold_pct": {
            "default": 0.3,
            "min": 0.01,
            "max": 5.0,
            "type": "float",
            "label": "Move Threshold %",
            "tooltip": "Last-bar % move that triggers entry (draft: 0.3%).",
        },
        "lookback_bars": {
            "default": 1,
            "min": 1,
            "max": 12,
            "type": "int",
            "label": "Lookback Bars",
            "tooltip": "Bars over which the % move is measured (1 = last bar).",
        },
        "min_confidence": {
            "default": 0.3,
            "min": 0.0,
            "max": 1.0,
            "type": "float",
            "label": "Min Confidence",
            "tooltip": "Views below this conviction are dropped.",
        },
        "underlying": {
            "default": "NIFTY",
            "type": "str",
            "label": "Underlying",
            "tooltip": "Index the view applies to (NIFTY / BANKNIFTY).",
        },
    }

    def entries(self, candles: pd.DataFrame) -> pd.Series:
        """Equity-path fallback: hold while momentum persists (never trades
        the option path — keeps the class valid for the plain engine)."""
        close = candles["close"]
        move = close.pct_change(int(self.lookback_bars)) * 100.0
        return move.abs() >= float(self.move_threshold_pct)

    def generate_market_view(self, candles: pd.DataFrame) -> MarketView | None:
        if candles is None or candles.empty or "close" not in candles.columns:
            return None
        if len(candles) <= int(self.lookback_bars):
            return None

        close = candles["close"]
        move_pct = (
            (float(close.iloc[-1]) - float(close.iloc[-1 - int(self.lookback_bars)]))
            / float(close.iloc[-1 - int(self.lookback_bars)])
            * 100.0
        )
        if abs(move_pct) < 1e-9:
            return None

        threshold = float(self.move_threshold_pct)
        # Confidence ramps 0→1 across one full extra threshold of movement.
        confidence = min(1.0, abs(move_pct) / (2.0 * threshold))
        if confidence < float(self.min_confidence):
            return None  # sub-threshold drift — no trade

        return MarketView(
            direction=Direction.BULLISH if move_pct > 0 else Direction.BEARISH,
            confidence=confidence,
            underlying=str(self.underlying).upper(),
            spot_price=float(close.iloc[-1]),
            bar_timestamp=candles.index[-1],
        )
