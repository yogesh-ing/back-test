"""Time-based alternator option strategy — strategy-plan draft #3, adapted (2026-09-18).

Original draft (strategy-plan/Time-Based Auto Trade Generator .txt)
-------------------------------------------------------------------
"Every 15 minutes, buy ATM Call or ATM Put, alternating each trade,
till 3:00 PM", exits 20% target / 15% SL / 30-min time stop.

Adaptation to this system
-------------------------
* The strategy contract is DETERMINISTIC (conformance battery: same candles
  twice → identical output), so "alternate each trade" state and wall-clock
  schedules can't live in the strategy. The deterministic equivalent: alternate
  by BAR PARITY — an even index of the current bar in the frame → BULLISH,
  odd → BEARISH. Same rhythm as the draft, no hidden state.
* The draft's 15-min trigger assumes 15-min bars; our feed is 1-hour (G6).
  One bar = one alternation step. ``alternate_every`` lets you stretch it.
* Exit rules belong to the Playbook: stop_loss_pct 0.15 / take_profit_pct
  0.20 (draft: 15%/20%), and the DTE/time square-off stands in for the
  30-minute hold cap.
* This strategy is deliberately view-agnostic — it is a high-turnover
  *execution/mechanics* test instrument, not an edge. Expect ≈ coin-flip
  P&L minus costs; that is exactly what makes it useful for today's
  paper/live validation: it must trade often and book cleanly.
"""

from __future__ import annotations

import pandas as pd

from backtest.strategy.templates import DirectionalOptionStrategy
from backtest.strategy.intent import Direction, MarketView


class TimeAlternator(DirectionalOptionStrategy):
    """Alternate ATM call/put every N bars (draft #3, deterministic form)."""

    name = "time_alternator"
    # 2026-09-22: instrument eligibility (spawn-form dropdown + create-API
    # enforcement). Index strategies trade the FNO index set.
    eligible_instruments = ["NIFTY", "BANKNIFTY"]
    description = (
        "Draft #3 adapted: alternates BULLISH/BEARISH by bar parity — a "
        "high-turnover mechanics validator that must trade often and "
        "cleanly, not an edge."
    )
    description_sections = {
        'overview': (
            'Mechanics validator, not an edge: alternates bullish/bearish views by bar parity to'
            'force constant, clean turnover through the whole execution chain.'
        ),
        'regime': (
            'Deliberately regime-blind — it flips on schedule in trends, ranges, and crashes'
            'alike. Its P&L is noise by design; judge it only on whether trades open/close'
            'correctly.'
        ),
        'entry': (
            'Every alternate_every bars the view flips (bullish on even cycles, bearish on odd).'
            'There is no market condition whatsoever.'
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
            'option can never lose more than the premium paid. In practice most exits are the'
            'parity flip reversing the position.'
        ),
        'parameters': (
            'alternate_every=1, min_confidence=0.0, underlying=NIFTY. Size defaults to 1 lot;'
            'quantity (and, where listed, exit levels) can be overridden at spawn within the'
            'declared menu.'
        ),
    }
    version = "1.0"
    author = "strategy-plan draft 3"

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
        "alternate_every": {
            "default": 1,
            "min": 1,
            "max": 8,
            "type": "int",
            "label": "Alternate Every N Bars",
            "tooltip": "Bars per alternation step (1 = every bar flips).",
        },
        "min_confidence": {
            "default": 0.0,
            "min": 0.0,
            "max": 1.0,
            "type": "float",
            "label": "Min Confidence",
            "tooltip": "Fixed 0.5 confidence; raise to suppress trades.",
        },
        "underlying": {
            "default": "NIFTY",
            "type": "str",
            "label": "Underlying",
            "tooltip": "Index the view applies to (NIFTY / BANKNIFTY).",
        },
    }

    def entries(self, candles: pd.DataFrame) -> pd.Series:
        """Equity-path fallback: long on parity-even bars, flat on odd."""
        step = max(1, int(self.alternate_every))
        parity = (pd.Series(range(len(candles)), index=candles.index) // step) % 2
        return parity == 0

    def generate_market_view(self, candles: pd.DataFrame) -> MarketView | None:
        if candles is None or candles.empty or "close" not in candles.columns:
            return None

        step = max(1, int(self.alternate_every))
        # Parity of the current bar in bars-per-step units — deterministic
        # on any window length ≥ 1 (no dependence on frame start).
        step_index = (len(candles) - 1) // step
        bullish = step_index % 2 == 0

        spot = float(candles["close"].iloc[-1])
        if spot <= 0:
            return None

        confidence = 0.5
        if confidence < float(self.min_confidence):
            return None

        return MarketView(
            direction=Direction.BULLISH if bullish else Direction.BEARISH,
            confidence=confidence,
            underlying=str(self.underlying).upper(),
            spot_price=spot,
            bar_timestamp=candles.index[-1],
        )
