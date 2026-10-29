"""Delta-momentum — directional views honed by delta-targeted strikes.

Trades the classic trend break while letting the option layer control the
Greeks profile: the runner is meant to spawn with an expression that pins
strikes by delta, e.g.::

    instrument.expression = {
        "type": {"BULLISH": "long_call", "BEARISH": "long_put"},
        "strike_selection": "delta",
        "delta_target": 0.40,
        "quantity": 1,
    }

``strike_selection: "delta"`` is already supported by the bridge
(``forward/options_bridge.py`` → ``DeltaSelector``), so a 0.40 delta-target
buys slightly OTM-lean legs: less extrinsic burn per point of direction,
theta pays for trend continuation rather than flat decay.

Signal (on the close series, deterministic):
  BULLISH: close above EMA(ema_slow) AND EMA(ema_trigger) sits above
           EMA(ema_slow) by at least ``slope_points`` index points.
  BEARISH: close below EMA(ema_slow) AND EMA(ema_trigger) sits below
           EMA(ema_slow) by at least ``slope_points``.
  Otherwise NEUTRAL → no view (the expression layer trades nothing).

Confidence scales with the trigger/slow EMA separation relative to
``slope_points`` — this strategy wants firm trend, not whipsaw flips near
the mean. Missing indicator warmup (fewer bars than the slow EMA) returns
no view, like every other strategy's warmup contract.
"""

from __future__ import annotations

import pandas as pd

from backtest.strategy.templates import DirectionalOptionStrategy
from backtest.strategy.intent import Direction, MarketView


class DeltaMomentum(DirectionalOptionStrategy):
    """EMA trend gate whose runner rides delta-targeted strikes."""

    name = "delta_momentum"
    eligible_instruments = ["NIFTY", "BANKNIFTY"]
    # The bridge picks strikes by delta when spawned with this expression —
    # the Greek lever of this strategy lives here, not in direction detection.
    recommended_expression = {
        "type": {"BULLISH": "long_call", "BEARISH": "long_put"},
        "strike_selection": "delta",
        "delta_target": 0.40,
        "quantity": 1,
    }
    description = (
        "Delta-honed directional options: EMA fast/slow trend with slope "
        "confirmation; spawn with instrument.expression "
        "{'strike_selection': 'delta', 'delta_target': 0.40} to buy "
        "delta-targeted legs instead of ATM."
    )
    description_sections = {
        'overview': (
            'Delta-honed directional options: EMA trend with slope confirmation on NIFTY,'
            'expressible as ATM legs or delta-targeted (0.40) strikes via the spawn expression.'
        ),
        'regime': (
            'Works when trends develop with a measurable slope — quiet drift without slope stays'
            'untraded by design. Struggles at trend exhaustion: slope confirms late, so entries'
            'cluster near local tops/bottoms.'
        ),
        'entry': (
            'Bullish when the trigger EMA is above the slow EMA with slope beyond the points'
            'threshold; bearish mirrored. Slope below the threshold emits silence.'
        ),
        'construction': (
            'Bullish view buys an at-the-money long call, bearish view an at-the-money long put'
            "on the underlying's nearest eligible expiry — a defined-risk, single-leg structure."
            'Size is 1 lot by default (override 1-5 lots where the spawn form allows). Spawned'
            'with a delta expression, the bridge instead picks the strike nearest delta 0.40'
            '(cheaper, more leveraged, faster theta).'
        ),
        'exits_risk': (
            'Declared exit plan: hard stop at -50% of premium paid, target at +100%, and exit'
            'when the directional signal flips. Entries are blocked inside 1 day of expiry. Worst'
            'case on a trade is the stop level (gap risk on illiquid strikes aside); a long'
            'option can never lose more than the premium paid.'
        ),
        'parameters': (
            'ema_slow=21, ema_trigger=9, slope_points=25.0, underlying=NIFTY. Size defaults to 1'
            'lot; quantity (and, where listed, exit levels) can be overridden at spawn within the'
            'declared menu.'
        ),
    }
    version = "1.0"
    author = "Trading Bot"

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
        "ema_slow": {
            "default": 21,
            "min": 3,
            "max": 100,
            "type": "int",
            "label": "Trend EMA",
            "tooltip": "Slow trend EMA the close must be above/below (default 21).",
        },
        "ema_trigger": {
            "default": 9,
            "min": 2,
            "max": 50,
            "type": "int",
            "label": "Trigger EMA",
            "tooltip": "Fast EMA whose separation from the trend EMA confirms slope.",
        },
        "slope_points": {
            "default": 25.0,
            "min": 1.0,
            "max": 10000.0,
            "type": "float",
            "label": "Slope Threshold (pts)",
            "tooltip": (
                "Trigger-vs-slow EMA separation in index points required before "
                "a view fires (index-sized: ~25 for NIFTY)."
            ),
        },
        "underlying": {
            "default": "NIFTY",
            "type": "str",
            "label": "Underlying",
            "tooltip": "Underlying index the view applies to (NIFTY / BANKNIFTY).",
        },
    }

    # ------------------------------------------------------------------
    # Deterministic indicator helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _ema(series: pd.Series, period: int) -> pd.Series:
        return series.ewm(span=int(period), adjust=False).mean()

    # ------------------------------------------------------------------
    # Strategy contract
    # ------------------------------------------------------------------

    def entries(self, candles: pd.DataFrame) -> pd.Series:
        """Equity-path fallback: slope-gated signal bars (any direction)."""
        if candles is None or candles.empty or len(candles) < max(
            int(self.ema_slow), int(self.ema_trigger)
        ):
            return pd.Series(
                False, index=candles.index if candles is not None else None
            )
        slow = self._ema(candles["close"], int(self.ema_slow))
        fast = self._ema(candles["close"], int(self.ema_trigger))
        threshold = float(self.slope_points)
        bull = (candles["close"] > slow) & (fast > slow + threshold)
        bear = (candles["close"] < slow) & (fast < slow - threshold)
        return bull | bear

    def generate_market_view(self, candles: pd.DataFrame) -> MarketView | None:
        if candles is None or candles.empty or "close" not in candles.columns:
            return None
        warmup = max(int(self.ema_slow), int(self.ema_trigger))
        if len(candles) < warmup:
            return None

        close = candles["close"]
        slow = self._ema(close, int(self.ema_slow))
        fast = self._ema(close, int(self.ema_trigger))

        last_close = float(close.iloc[-1])
        last_slow = float(slow.iloc[-1])
        last_fast = float(fast.iloc[-1])
        separation = last_fast - last_slow
        threshold = max(float(self.slope_points), 1e-9)

        # Direction needs price beyond the trend EMA AND the trigger EMA
        # shifted away from it by the threshold — a double-confirmation
        # momentum gate, not a bare cross.
        if last_close > last_slow and separation >= threshold:
            direction = Direction.BULLISH
        elif last_close < last_slow and separation <= -threshold:
            direction = Direction.BEARISH
        else:
            return None

        # Confidence: separation is the trend's own evidence. Half of the
        # threshold buys 0.5; twice the threshold maps to ~1.0.
        confidence = min(1.0, 0.5 + abs(separation) / (threshold * 4.0))

        return MarketView(
            direction=direction,
            confidence=confidence,
            underlying=str(self.underlying).upper(),
            spot_price=last_close,
            bar_timestamp=candles.index[-1],
            metadata={
                "ema_slow": last_slow,
                "ema_trigger": last_fast,
                "separation": separation,
                "strike_selection": "delta",
                "strategy": self.name,
            },
        )
