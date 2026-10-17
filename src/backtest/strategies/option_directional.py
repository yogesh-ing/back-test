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

from backtest.strategy.templates import DirectionalOptionStrategy
from backtest.strategy.intent import Direction, MarketView


class DirectionalOptions(DirectionalOptionStrategy):
    """EMA-momentum directional view for the options expression layer."""

    name = "directional_options"
    # 2026-09-22: declares where its MarketView can be expressed — the spawn
    # form renders these as a dropdown (no free-text instrument typing) and
    # runner/create enforces the same set. Keep in sync with the chain
    # sources (synthetic BS generator + mStock FNO index set).
    eligible_instruments = ["NIFTY", "BANKNIFTY"]
    # issues.txt S2: this strategy belongs in the index-options segment —
    # the spawn form preselects it (backtest → paper → live stay in one
    # capital partition). Must match a name in config/segments.yaml.
    default_segment = "options_index"
    description = (
        "Directional options — emits a bullish/bearish MarketView from short-term "
        "EMA momentum; feed it into the expression layer (long call/put, spreads)."
    )
    description_sections = {
        'overview': (
            'Directional index options: converts short-term EMA momentum on NIFTY into a declared'
            'bullish/bearish view, expressed as ATM long calls or puts.'
        ),
        'regime': (
            "Works when intraday momentum persists long enough for the option's delta to pay."
            'Struggles in choppy, theta-heavy tape — repeated small premium losses and time decay'
            'erode the account even when direction calls are roughly right.'
        ),
        'entry': (
            'Bullish when the EMA momentum score clears the minimum confidence, bearish on the'
            'mirror; the score scales the recent move (points) against the scale parameter.'
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
            'ema_period=9, scale_points=100.0 (move size that maps to full confidence),'
            'min_confidence=0.3 (signals below this are silence — no trade). underlying=NIFTY.'
            'Size defaults to 1 lot; quantity (and, where listed, exit levels) can be overridden'
            'at spawn within the declared menu.'
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
