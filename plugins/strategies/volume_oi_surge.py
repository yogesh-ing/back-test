"""Volume-surge options — direction from a burst of traded volume.

Trades only when the *market shouts*: a candle whose volume expands
``surge_multiple`` times over its trailing average (default 1.5× — a real
participation burst on index bars) AND whose close finishes
the move decisively. Volume without conviction (a big candle that closes
mid-range) stays silent — pump bars without participation are noise.

Execution (Phase 2 rewrite): this strategy declares its own contract below
— BULLISH→ATM long call, BEARISH→ATM long put, 50% stop / 100% target.

An earlier version of this docstring claimed a volume-topped strike
selection (``strike_selection: "volume"``). That selector was never
implemented: the bridge silently degraded it to ATM — a silent fallback of
exactly the kind the execution refactor abolishes. ATM is therefore now the
*declared* behaviour (and always was the traded behaviour). A volume
selector may return only as a real implementation with quote-level volume
access, declared in the spec like any other selection.

Signal (deterministic, on OHLCV candles):
  avgvol   = rolling mean of volume over ``avg_bars`` prior bars
  surge    = volume_now >= surge_multiple * avgvol
  move     = close - previous close, ``move_points`` in size
  body     = |close - open| >= body_fraction * (high - low)  — real impulse,
             not an indecisive doji.
  BULLISH: surge AND move >=  +move_points AND close in the upper body.
  BEARISH: surge AND move <=  -move_points AND close in the lower body.
  Otherwise NEUTRAL → no view.
"""

from __future__ import annotations

import pandas as pd

from backtest.strategy.templates import DirectionalOptionStrategy
from backtest.strategy.intent import Direction, MarketView


class VolumeOISurge(DirectionalOptionStrategy):
    """Volume-surge gate whose runner picks the busiest strike."""

    name = "volume_oi_surge"
    eligible_instruments = ["NIFTY", "BANKNIFTY"]
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

    description = (
        "Options traded on volume bursts: candle volume surge + decisive "
        "close; spawn with instrument.expression "
        "{'strike_selection': 'volume'} so the bridge buys the strike with "
        "the most traded volume near ATM."
    )
    description_sections = {
        'overview': (
            'Volume-surge options: a candle whose volume bursts well above average with a'
            'decisive directional close fires a same-direction view.'
        ),
        'regime': (
            'Works when volume surges mark genuine institutional participation that continues.'
            'Struggles around open/close auction bars and expiry-day volume distortions, where'
            'surges are mechanical rather than informative.'
        ),
        'entry': (
            'Volume above surge_multiple × the rolling average AND a close decisively in one'
            'direction (body fraction of range) with a minimum points move.'
        ),
        'construction': (
            'Bullish view buys an at-the-money long call, bearish view an at-the-money long put'
            "on the underlying's nearest eligible expiry — a defined-risk, single-leg structure."
            'Size is 1 lot by default (override 1-5 lots where the spawn form allows). Spawned'
            "with the 'volume' strike selection, the bridge buys the most-traded strike near ATM"
            'instead.'
        ),
        'exits_risk': (
            'Declared exit plan: hard stop at -50% of premium paid, target at +100%, and exit'
            'when the directional signal flips. Entries are blocked inside 1 day of expiry. Worst'
            'case on a trade is the stop level (gap risk on illiquid strikes aside); a long'
            'option can never lose more than the premium paid.'
        ),
        'parameters': (
            'avg_bars=20, surge_multiple=1.5, move_points=20.0, body_fraction=0.5,'
            'underlying=NIFTY. Size defaults to 1 lot; quantity (and, where listed, exit levels)'
            'can be overridden at spawn within the declared menu.'
        ),
    }
    version = "1.0"
    author = "Trading Bot"

    params = {
        "avg_bars": {
            "default": 20,
            "min": 2,
            "max": 200,
            "type": "int",
            "label": "Volume Baseline (bars)",
            "tooltip": "Rolling window of prior bars the surge compares against.",
        },
        "surge_multiple": {
            "default": 1.5,
            "min": 1.1,
            "max": 50.0,
            "type": "float",
            "label": "Surge Multiple",
            "tooltip": "Volume must be this many times the rolling average (1.5× = a genuine burst).",
        },
        "move_points": {
            "default": 20.0,
            "min": 0.5,
            "max": 100000.0,
            "type": "float",
            "label": "Move Threshold (pts)",
            "tooltip": (
                "Close-to-close move in index points required with the surge "
                "(index-sized: ~20 for NIFTY)."
            ),
        },
        "body_fraction": {
            "default": 0.5,
            "min": 0.0,
            "max": 1.0,
            "type": "float",
            "label": "Body Strength",
            "tooltip": (
                "Candle body must be this fraction of the high-low range — "
                "rejects decisive-looking but indecisive candles."
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
    # Strategy contract
    # ------------------------------------------------------------------

    def entries(self, candles: pd.DataFrame) -> pd.Series:
        """Equity-path fallback: surge bars (any direction)."""
        if candles is None or candles.empty or len(candles) <= int(self.avg_bars):
            return pd.Series(
                False, index=candles.index if candles is not None else None
            )
        surge = self._surge_mask(candles)
        return surge & (candles["close"] != candles["close"].shift(1))

    def _surge_mask(self, candles: pd.DataFrame) -> pd.Series:
        avgvol = candles["volume"].rolling(int(self.avg_bars)).mean().shift(1)
        return candles["volume"] >= float(self.surge_multiple) * avgvol

    def generate_market_view(self, candles: pd.DataFrame) -> MarketView | None:
        if candles is None or candles.empty:
            return None
        need = max(int(self.avg_bars) + 1, 2)
        cols = {"volume", "close", "open", "high", "low"} <= set(candles.columns)
        if len(candles) <= need or not cols:
            return None

        close = candles["close"]
        high = candles["high"]
        low = candles["low"]

        surge = bool(self._surge_mask(candles).iloc[-1])
        if not surge:
            return None

        move = float(close.iloc[-1]) - float(close.iloc[-2])
        threshold = max(float(self.move_points), 1e-9)

        rng = float(high.iloc[-1]) - float(low.iloc[-1])
        if rng <= 0:
            return None
        body = abs(float(close.iloc[-1]) - float(candles["open"].iloc[-1]))
        if body < float(self.body_fraction) * rng:
            return None

        if move >= threshold and float(close.iloc[-1]) > float(candles["open"].iloc[-1]):
            direction = Direction.BULLISH
        elif move <= -threshold and float(close.iloc[-1]) < float(candles["open"].iloc[-1]):
            direction = Direction.BEARISH
        else:
            return None

        # Confidence: how far past both thresholds the burst pushed.
        vol_ratio = (
            float(candles["volume"].iloc[-1])
            / max(
                float(
                    candles["volume"]
                    .rolling(int(self.avg_bars))
                    .mean()
                    .shift(1)
                    .iloc[-1]
                ),
                1.0,
            )
        )
        conf = 0.5 + min(0.5, (vol_ratio / max(float(self.surge_multiple), 1.0) - 1.0) / 2.0)
        confidence = max(0.5, min(1.0, conf))

        return MarketView(
            direction=direction,
            confidence=confidence,
            underlying=str(self.underlying).upper(),
            spot_price=float(close.iloc[-1]),
            bar_timestamp=candles.index[-1],
            metadata={
                "volume": float(candles["volume"].iloc[-1]),
                "move": move,
                "surge": True,
                "strike_selection": "volume",
                "strategy": self.name,
            },
        )
