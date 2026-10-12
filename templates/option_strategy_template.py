"""Option strategy template — emit a MarketView, the bridge trades it (U6.3).

The one-minute drop-in flow
---------------------------
1. Copy this file to ``plugins/strategies/<your_strategy>.py``.
2. Rename the class, change ``name`` (unique across all strategies).
3. Edit ``generate_market_view`` — that is the whole strategy.
4. Restart. The loader vets imports + conformance, registers the strategy,
   and the spawn form locks itself to index underlyings (option kind).

How an option strategy differs from an equity one
-------------------------------------------------
An equity strategy returns a *target position*. An option strategy returns a
**directional view** — :class:`~backtest.strategy.intent.MarketView` — and the
engine's OptionsBridge turns it into a structure (long call/put, spreads) via
the selector + playbook expression. You never touch chains, strikes, lots or
orders; the bridge owns that (C2: the engine hands you data, it executes the
rest). One open structure at a time is the bridge's job too — while one is
open, your next view is an *exit signal*, not a new entry.

The hooks (implement exactly one signal hook + the view)
--------------------------------------------------------
* ``generate_market_view(candles) -> MarketView | None`` — REQUIRED for the
  option classification. Return ``None`` for "no conviction" (no trade this
  bar); the bridge reads ``direction`` + ``confidence`` + ``spot_price``.
* ``entries(candles) -> Series[bool]`` — the trivial fallback below. The base
  contract requires *some* signal hook from every strategy (``validate()``
  refuses a class with neither ``generate_signals`` nor ``entries``); keep this
  stub so your class remains valid if it is ever run through the equity path.
* ``generate_signals(candles)`` — omit it; the default derives from entries.

The hard rules (same as the equity template — enforced by the loader)
----------------------------------------------------------------------
1. **C2 import ban** — no ``backtest.brokers`` / ``backtest.forward`` /
   ``backtest.options`` / ``backtest.data`` / ``backtest.live`` / ``requests``
   / ``urllib`` / ``websocket*``. AST-checked at load; refusal reason logged.
2. **Determinism** — same candles twice → identical view. No clock, no
   randomness. The battery value-compares two runs and refuses mismatches.
3. **Backward-looking only** — you see a trailing window ending at the
   current bar; the engine acts one bar later (no-lookahead invariant).
4. **Confidence is your risk dial** — the playbook caps loss per trade, but
   ``confidence`` below your ``min_confidence`` should mean ``None``: the
   cheapest trade is the one you never open.
"""

from __future__ import annotations

import pandas as pd

from backtest.strategy.base import Strategy
from backtest.strategy.intent import Direction, MarketView


class MyOptionMomentum(Strategy):
    """EMA-momentum view (template example): close vs EMA → BULLISH/BEARISH."""

    # Identity — name must be UNIQUE across built-ins + plugins.
    name = "my_option_momentum"
    description = (
        "Template: option momentum — BULLISH above the EMA, BEARISH below, "
        "None when the move is inside the confidence band."
    )
    version = "1.0"
    author = "your-name"

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
            "max": 100_000.0,
            "type": "float",
            "label": "Confidence Scale (pts)",
            "tooltip": (
                "Close-to-EMA distance (points) that maps to full confidence. "
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
        """Trivial equity fallback (the base contract requires a signal hook).

        Keeps the class valid outside the options path — e.g. if someone runs
        it through the plain backtest engine it simply trades close-over-EMA.
        """
        ema = candles["close"].ewm(span=int(self.ema_period), adjust=False).mean()
        return candles["close"] > ema

    def generate_market_view(self, candles: pd.DataFrame) -> MarketView | None:
        if candles is None or candles.empty or "close" not in candles.columns:
            return None

        close = float(candles["close"].iloc[-1])
        ema = float(candles["close"].ewm(span=int(self.ema_period), adjust=False).mean().iloc[-1])
        distance = close - ema
        if abs(distance) < 1e-9:
            return None

        confidence = min(1.0, abs(distance) / float(self.scale_points))
        if confidence < float(self.min_confidence):
            return None  # no conviction — the bridge treats None as "no trade"

        return MarketView(
            direction=Direction.BULLISH if distance > 0 else Direction.BEARISH,
            confidence=confidence,
            underlying=str(self.underlying).upper(),
            spot_price=close,
            bar_timestamp=candles.index[-1],
        )
