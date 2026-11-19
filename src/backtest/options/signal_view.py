"""Bridge an equity strategy's signal series into the options driver's views.

The options backtest driver consumes :class:`MarketView` objects; equity
strategies expose ``generate_signals(candles) -> pd.Series`` of ``+1/0/-1``.
Plan V3 (gaps table) pins the mechanism: the signal is the *input*, the option
structure is the *output* — the equity backtest is never run separately.

This wrapper precomputes the signal series once over the full frame and reads
the value at each bar's own timestamp. That is equivalent to calling the
strategy per expanding window **for causal strategies** (rolling indicators,
stateful loops that only look backwards), which every registered equity
strategy is. A strategy that peeked ahead would already be broken in the equity
backtest; this bridge does not introduce look-ahead of its own.
"""

from __future__ import annotations

from decimal import Decimal

import pandas as pd

from backtest.strategy.intent import Direction, MarketView

__all__ = ["SignalViewStrategy"]


class SignalViewStrategy:
    """Duck-typed strategy (``name`` + ``generate_market_view``) for the driver.

    ``+1`` → BULLISH, ``-1`` → BEARISH, ``0`` → no view (no trade, and not a
    signal flip). ``confidence`` sets outright-vs-spread via the driver's
    decider threshold (≥0.7 buys the outright option).
    """

    def __init__(
        self,
        strategy,
        candles: pd.DataFrame,
        *,
        confidence: float = 0.8,
        underlying: str = "NIFTY",
    ) -> None:
        self._strategy = strategy
        self._signals = strategy.generate_signals(candles)
        self._confidence = float(confidence)
        self._underlying = underlying

    @property
    def name(self) -> str:
        return str(getattr(self._strategy, "name", "") or "signal_view")

    def generate_market_view(self, candles: pd.DataFrame) -> MarketView | None:
        if candles.empty:
            return None
        idx = candles.index[-1]
        try:
            signal = float(self._signals.loc[idx])
        except (KeyError, TypeError, ValueError):
            return None
        if signal > 0:
            direction = Direction.BULLISH
        elif signal < 0:
            direction = Direction.BEARISH
        else:
            return None
        return MarketView(
            direction=direction,
            confidence=self._confidence,
            underlying=self._underlying,
            spot_price=Decimal(str(float(candles["close"].iloc[-1]))),
            bar_timestamp=None,
        )