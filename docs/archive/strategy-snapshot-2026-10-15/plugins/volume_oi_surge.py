"""Volume-surge options — direction from a burst of traded volume.

Trades only when the *market shouts*: a candle whose volume expands
``surge_multiple`` times over its trailing average (default 1.5× — a real
participation burst on index bars) AND whose close finishes
the move decisively. Volume without conviction (a big candle that closes
mid-range) stays silent — pump bars without participation are noise.

The options layer is where volume earns its keep in this strategy: the runner
should be spawned with a volume-topped strike selection::

    instrument.expression = {
        "type": {"BULLISH": "long_call", "BEARISH": "long_put"},
        "strike_selection": "volume",
        "quantity": 1,
    }

``strike_selection: "volume"`` (added to the bridge's ``_pick_strikes``) picks
the strike near ATM carrying the highest traded volume — liquidity where
participants actually traded, instead of the geometrically closest strike.
When no quote row carries volume data (e.g. a synthetic chain, which books 0),
the selection degrades to ATM rather than inventing a preference.

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

from backtest.strategy.base import Strategy
from backtest.strategy.intent import Direction, MarketView


class VolumeOISurge(Strategy):
    """Volume-surge gate whose runner picks the busiest strike."""

    name = "volume_oi_surge"
    eligible_instruments = ["NIFTY", "BANKNIFTY"]
    recommended_expression = {
        "type": {"BULLISH": "long_call", "BEARISH": "long_put"},
        "strike_selection": "volume",
        "quantity": 1,
    }
    description = (
        "Options traded on volume bursts: candle volume surge + decisive "
        "close; spawn with instrument.expression "
        "{'strike_selection': 'volume'} so the bridge buys the strike with "
        "the most traded volume near ATM."
    )
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
