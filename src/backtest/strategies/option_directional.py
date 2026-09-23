"""Directional options strategy — the first built-in producer of option intents.

Gap G3.1: every other built-in strategy emits equity signals only; this one
overrides :meth:`generate_market_view` so the expression layer
(``options/selector.py`` → ``options/structures.py``) has something to consume
in a live app session.

Logic (momentum gate on a short EMA):
* Close above the short EMA → BULLISH view → e.g. long call / bull call spread.
* Close below the short EMA → BEARISH view → e.g. long put / bear put spread.
* Otherwise → NEUTRAL → no option trade.

Confidence scales with the distance between close and EMA relative to the
ATR-style ``scale_points`` normaliser, clamped to ``[min_confidence, 1.0]``.
The view carries the spot price, which is what strike selectors need.
"""

from __future__ import annotations

from decimal import Decimal

import pandas as pd

from backtest.strategy.base import Strategy
from backtest.strategy.intent import Direction, MarketView


class DirectionalOptions(Strategy):
    """EMA-momentum directional view for the options expression layer."""

    name = "directional_options"
    # 2026-09-22: declares where its MarketView can be expressed — the spawn
    # form renders these as a dropdown (no free-text instrument typing) and
    # runner/create enforces the same set. Keep in sync with the chain
    # sources (synthetic BS generator + mStock FNO index set).
    eligible_instruments = ["NIFTY", "BANKNIFTY"]
    description = (
        "Directional options — emits a bullish/bearish MarketView from short-term "
        "EMA momentum; feed it into the expression layer (long call/put, spreads)."
    )
    version = "1.0"
    author = "Trading Bot"
    params = {
        "ema_period": {
            "default": 9,
            "min": 2,
            "max": 200,
            "type": "int",
            "label": "EMA Period",
            "tooltip": "Short EMA length over the close series.",
        },
        "scale_points": {
            "default": 100.0,
            "min": 1.0,
            "max": 100000.0,
            "type": "float",
            "label": "Confidence Scale (pts)",
            "tooltip": (
                "Close-to-EMA distance in points that maps to full confidence. "
                "Index-sized: ~100 for NIFTY."
            ),
        },
        "min_confidence": {
            "default": 0.3,
            "min": 0.0,
            "max": 1.0,
            "type": "float",
            "label": "Min Confidence",
            "tooltip": "Views below this conviction are dropped (None returned).",
        },
        "underlying": {
            "default": "NIFTY",
            "type": "str",
            "label": "Underlying",
            "tooltip": "Index the view applies to (NIFTY / BANKNIFTY).",
        },
    }

    def entries(self, candles: pd.DataFrame) -> pd.Series:
        """Equity-style entry: close crossing above the EMA (keeps the
        generic engine happy if this strategy is ever run without options)."""
        ema = candles["close"].ewm(span=int(self.ema_period), adjust=False).mean()
        return candles["close"] > ema

    def generate_market_view(self, candles: pd.DataFrame) -> MarketView | None:
        """Momentum-gated directional view.

        Returns ``None`` when the signal is neutral or below
        ``min_confidence`` — the expression layer treats that as
        "no option trade this bar".
        """
        if candles is None or candles.empty or "close" not in candles.columns:
            return None

        close = candles["close"]
        ema = close.ewm(span=int(self.ema_period), adjust=False).mean()

        last_close = float(close.iloc[-1])
        last_ema = float(ema.iloc[-1])
        distance = last_close - last_ema

        if abs(distance) < 1e-9:
            return None

        direction = Direction.BULLISH if distance > 0 else Direction.BEARISH

        scale = max(float(self.scale_points), 1e-9)
        raw_confidence = abs(distance) / scale
        confidence = min(1.0, max(float(self.min_confidence), raw_confidence))
        if raw_confidence < float(self.min_confidence):
            return None

        try:
            spot = Decimal(str(last_close))
        except Exception:  # noqa: BLE001 — never let a bad bar kill the view
            return None

        return MarketView(
            direction=direction,
            confidence=confidence,
            underlying=str(self.underlying),
            spot_price=spot,
            bar_timestamp=candles.index[-1] if len(candles.index) else None,
            metadata={
                "ema": last_ema,
                "close": last_close,
                "distance": distance,
                "strategy": self.name,
            },
        )
