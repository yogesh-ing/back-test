"""Equity strategy template — copy me, rename me, ship me (U6.3 / D7).

The one-minute drop-in flow
---------------------------
1. Copy this file to ``plugins/strategies/<your_strategy>.py``
   (create the folder if it does not exist).
2. Rename the class and change ``name`` (must be unique — built-ins live in
   ``src/backtest/strategies/``).
3. Edit the logic. Restart the app (or re-run ``discover_plugins()``) — the
   loader vets the file (imports + conformance battery) and registers it; the
   spawn form picks it up automatically.
4. If the loader skips your file, the reason is in the log
   (``backtest.plugins`` logger) — fix and restart.

The hooks the engine calls (implement ONE signal hook)
------------------------------------------------------
* ``generate_signals(candles) -> Series[int]`` — target position per bar:
  ``1`` long, ``0`` flat, ``-1`` short (if ``allow_short``). The canonical
  equity hook. See ``docs/STRATEGY-AUTHORING.md`` for the full table.
* ``entries(candles) -> Series[bool]`` (+ optional ``exits``) — the
  entries/exits model; the base class builds target positions for you.
  Pick this **or** ``generate_signals``, never both.
* ``generate_market_view(candles) -> MarketView | None`` — option strategies
  only (see the option template). Override it and your strategy is classified
  ``signal_kind="option"``: the spawn form locks to index underlyings and the
  OptionsBridge trades structures from your view.

Parameters
----------
Declare them on ``params`` in schema form (``default``/``min``/``max``/
``type``/``label``/``tooltip``). Every entry becomes a field on the spawn
form — ``label``/``tooltip`` are what the user sees. Values arrive as
instance attributes (``self.period``) and are type-coerced for you.

The hard rules
--------------
1. **C2 — the engine hands you data.** Never import engine, broker, feed or
   data modules (``backtest.brokers``, ``backtest.forward``,
   ``backtest.options``, ``backtest.data``, ``backtest.live``, ``requests``,
   ``urllib``, ``websocket*``). The loader's AST check REFUSES such files at
   startup. Read ``candles``; return a Series. Nothing else.
2. **Determinism.** Same candles → same output, every time. No
   ``datetime.now()``, no ``random``, no reads of state outside the frame you
   were given. The conformance battery runs your strategy twice on identical
   candles and refuses differences — a backtest and a forward test must be
   the same function.
3. **Backward-looking only.** Signals computed at bar ``t`` are acted on at
   bar ``t+1`` by the engine (no-lookahead invariant). Use information up to
   and including the current bar; never "peek" forward (you can't — you only
   get the trailing window — and never try to).
"""

from __future__ import annotations

import pandas as pd

from backtest.strategy.base import Strategy


class MyEmaTrend(Strategy):
    """Long while close is above its EMA, flat otherwise (template example)."""

    # Identity — name must be UNIQUE across built-ins + plugins.
    name = "my_ema_trend"
    description = "Template: long while close > EMA(period), flat otherwise."
    version = "1.0"
    author = "your-name"

    params = {
        "period": {
            "default": 20,
            "min": 2,
            "max": 200,
            "type": "int",
            "label": "EMA Period",
            "tooltip": "Lookback for the trend EMA over the close series.",
        },
    }

    def generate_signals(self, candles: pd.DataFrame) -> pd.Series:
        ema = candles["close"].ewm(span=int(self.period), adjust=False).mean()
        return (candles["close"] > ema).astype(int)
