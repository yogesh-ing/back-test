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

from backtest.strategy.base import Strategy
from backtest.strategy.intent import Direction, MarketView


class TimeAlternator(Strategy):
    """Alternate ATM call/put every N bars (draft #3, deterministic form)."""

    name = "time_alternator"
    description = (
        "Draft #3 adapted: alternates BULLISH/BEARISH by bar parity — a "
        "high-turnover mechanics validator that must trade often and "
        "cleanly, not an edge."
    )
    version = "1.0"
    author = "strategy-plan draft 3"

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
