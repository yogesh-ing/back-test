"""BankNifty EMA Momentum — directional options strategy (renamed in Phase 2).

Was ``banknifty_straddle``: the old name promised a straddle while the
code always emitted directional BULLISH/BEARISH views — the misleading-
name failure the rewrite abolishes. Signal logic unchanged; snapshot of
the original: docs/archive/strategy-snapshot-2026-10-15.

Option-native strategy producing MarketView conviction for BankNifty / Nifty index options.
Compatible with OptionBacktestDriver and OptionsBridge expression layers.
"""

from __future__ import annotations

from decimal import Decimal

import pandas as pd

from backtest.strategy.templates import DirectionalOptionStrategy
from backtest.strategy.intent import Direction, MarketView


class BankNiftyEmaMomentum(DirectionalOptionStrategy):
    """BankNifty Options Strategy — dual EMA + Volatility breakouts emitting MarketView."""

    name = "banknifty_ema_momentum"
    eligible_instruments = ["BANKNIFTY", "NIFTY"]
    # issues.txt S2: index-options segment link (spawn-form preselect).
    default_segment = "options_index"
    description = (
        "BankNifty EMA momentum — directional index options strategy emitting high-conviction "
        "MarketView signals based on EMA momentum and ATR volatility expansions."
    )
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
        "fast_period": {
            "default": 9,
            "min": 3,
            "max": 50,
            "type": "int",
            "label": "Fast Period",
            "tooltip": "Fast momentum lookback length.",
        },
        "slow_period": {
            "default": 21,
            "min": 10,
            "max": 100,
            "type": "int",
            "label": "Slow Period",
            "tooltip": "Slow trend lookback length.",
        },
        "scale_points": {
            "default": 150.0,
            "min": 10.0,
            "max": 10000.0,
            "type": "float",
            "label": "Confidence Scale (pts)",
            "tooltip": "Index move in points that achieves full confidence rating.",
        },
        "min_confidence": {
            "default": 0.25,
            "min": 0.1,
            "max": 1.0,
            "type": "float",
            "label": "Min Confidence",
            "tooltip": "Threshold below which market view is marked neutral (no trade).",
        },
        "underlying": {
            "default": "BANKNIFTY",
            "type": "str",
            "label": "Underlying",
            "tooltip": "Target index (BANKNIFTY / NIFTY).",
        },
    }

    def entries(self, candles: pd.DataFrame) -> pd.Series:
        """Equity-style fallback: fast EMA crossing above slow EMA."""
        close = candles["close"]
        fast = close.ewm(span=int(self.fast_period), adjust=False).mean()
        slow = close.ewm(span=int(self.slow_period), adjust=False).mean()
        return (fast > slow) & (fast.shift(1) <= slow.shift(1))

    def generate_market_view(self, candles: pd.DataFrame) -> MarketView | None:
        """Emits MarketView for the options expression layer."""
        if candles is None or candles.empty or "close" not in candles.columns:
            return None

        close = candles["close"]
        fast = close.ewm(span=int(self.fast_period), adjust=False).mean()
        slow = close.ewm(span=int(self.slow_period), adjust=False).mean()

        last_close = float(close.iloc[-1])
        last_fast = float(fast.iloc[-1])
        last_slow = float(slow.iloc[-1])
        diff = last_fast - last_slow

        if abs(diff) < 1e-6:
            return None

        direction = Direction.BULLISH if diff > 0 else Direction.BEARISH
        raw_confidence = abs(diff) / max(float(self.scale_points), 1.0)
        confidence = min(1.0, max(float(self.min_confidence), raw_confidence))

        if raw_confidence < float(self.min_confidence):
            return None

        try:
            spot = Decimal(str(last_close))
        except Exception:
            return None

        return MarketView(
            direction=direction,
            confidence=confidence,
            underlying=str(self.underlying).upper(),
            spot_price=spot,
            bar_timestamp=candles.index[-1] if len(candles.index) else None,
            metadata={
                "strategy": self.name,
                "fast_ema": last_fast,
                "slow_ema": last_slow,
                "diff": diff,
            },
        )
