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

    Option-native strategies (those that override ``generate_market_view``,
    e.g. ``CE_instant_buy`` / ``PE_instant_buy``) are priced straight from
    their own view so a put strategy is never fed bullish signals. Equity
    strategies keep the existing ``generate_signals`` → view bridge.
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
        self._confidence = float(confidence)
        self._underlying = underlying
        # Lazy import keeps backtest.options at the bottom of the import stack.
        from backtest.strategy.base import Strategy

        self._option_native = (
            type(strategy).generate_market_view is not Strategy.generate_market_view
        )
        if not self._option_native:
            self._signals = strategy.generate_signals(candles)

    @property
    def name(self) -> str:
        return str(getattr(self._strategy, "name", "") or "signal_view")

    def generate_market_view(self, candles: pd.DataFrame) -> MarketView | None:
        if candles.empty:
            return None
        if self._option_native:
            # Option-native strategies emit their own MarketView (e.g. BULLISH
            # for CE_instant_buy, BEARISH for PE_instant_buy). Use it directly,
            # normalizing price fields to Decimal so the Decimal-strike selector
            # never sees a float spot/target/stop price.
            view = self._strategy.generate_market_view(candles)
            if view is None:
                return None

            def _dec(value):
                if value is None:
                    return None
                return value if isinstance(value, Decimal) else Decimal(str(value))

            return MarketView(
                direction=view.direction,
                confidence=view.confidence,
                underlying=view.underlying,
                spot_price=_dec(view.spot_price),
                target_price=_dec(view.target_price),
                stop_price=_dec(view.stop_price),
                bar_timestamp=view.bar_timestamp,
                metadata=dict(view.metadata or {}),
            )
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
