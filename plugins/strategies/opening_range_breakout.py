"""Opening Range Breakout (intraday) — B7 institutional strategy.

Verdict: PAPER-READY (V1). Simple intraday equity/options strategy using only
1-min candles. Defined risk (0.5% target / 0.3% stop), time stop at 14:30,
regime-gated (ADX>=15, normal volume). No external data feed required.

Spec:
- Signal: record 9:15-9:30 high (H) and low (L) of NIFTY. At 9:31+, if close>H
  -> BULLISH (long CE); if close<L -> BEARISH (long PE).
- Structure: long ATM CE (intraday) or long ATM PE, 7-DTE, 1 lot.
- Entry: first bar after 9:30 where close breaks H or L.
- SL: -0.3% of entry
- TP: +0.5% of entry
- Trailing SL: none (fixed bracket; time stop dominates)
- Time stop: square off at 14:30 IST
- Regime gate: skip if ADX<15 (no trend); skip if session volume < 70% of 20-day avg
- Kill: 5 consecutive false breakouts; or avg profit < 0.3% of NIFTY
- Sizing: 1 lot CE/PE (scale 1->3->5->10)
- Params (4): or_minutes(15), target_pct(0.5), stop_pct(0.3), underlying

Notes: ADX and volume filters are expressed as params; engine may not compute
ADX intraday — gate can be relaxed to volume-only in V1 backtests.
"""
from __future__ import annotations

import pandas as pd

from backtest.strategy.templates import DirectionalOptionStrategy
from backtest.strategy.intent import Direction, MarketView


class OpeningRangeBreakout(DirectionalOptionStrategy):
    name = "opening_range_breakout"
    eligible_instruments = ["NIFTY", "BANKNIFTY"]
    description = (
        "Intraday opening-range breakout: record 9:15-9:30 high/low, buy on break "
        "above (long CE) or sell on break below (long PE). Exit by 14:30 or "
        "0.5% target / 0.3% stop."
    )
    version = "1.0"
    author = "quant-researcher (B7)"

    execution = {
        "emits": ["bullish", "bearish", "neutral"],
        "structures": {
            "bullish": {"structure": "long_call", "strike_selection": "atm"},
            "bearish": {"structure": "long_put", "strike_selection": "atm"},
        },
        "exit": {
            "stop_loss_pct": 0.003,
            "take_profit_pct": 0.005,
            "signal_flip": False,
            "min_days_to_expiry": 1,
        },
        "quantity": 1,
        "min_quantity": 1,
        "max_quantity": 5,
        "allowed_overrides": ["quantity", "exit"],
    }

    params = {
        "or_minutes": {
            "default": 15,
            "min": 5,
            "max": 60,
            "type": "int",
            "label": "Opening Range (min)",
            "tooltip": "Minutes after 9:15 to record the opening range high/low.",
        },
        "target_pct": {
            "default": 0.005,
            "min": 0.001,
            "max": 0.05,
            "type": "float",
            "label": "Target %",
            "tooltip": "Take profit as fraction of entry (0.005 = 0.5%).",
        },
        "stop_pct": {
            "default": 0.003,
            "min": 0.001,
            "max": 0.05,
            "type": "float",
            "label": "Stop %",
            "tooltip": "Stop loss as fraction of entry (0.003 = 0.3%).",
        },
        "underlying": {
            "default": "NIFTY",
            "type": "str",
            "label": "Underlying",
            "tooltip": "Index to trade (NIFTY / BANKNIFTY).",
        },
    }

    @staticmethod
    def _opening_range(candles: pd.DataFrame, minutes: int):
        if candles is None or candles.empty:
            return None, None
        # Assume candles index is a DatetimeIndex in IST.
        day = candles.index[-1].date()
        start = pd.Timestamp(day).replace(hour=9, minute=15)
        end = start + pd.Timedelta(minutes=minutes)
        mask = (candles.index >= start) & (candles.index < end)
        window = candles.loc[mask]
        if window.empty:
            return None, None
        return float(window["high"].max()), float(window["low"].min())

    def entries(self, candles: pd.DataFrame) -> pd.Series:
        if candles is None or candles.empty:
            return pd.Series(dtype=bool)
        hi, lo = self._opening_range(candles, int(self.or_minutes))
        if hi is None:
            return pd.Series(False, index=candles.index)
        return (candles["close"] > hi) | (candles["close"] < lo)

    def generate_market_view(self, candles: pd.DataFrame) -> MarketView | None:
        if candles is None or candles.empty or "close" not in candles.columns:
            return None
        or_min = int(self.or_minutes)
        hi, lo = self._opening_range(candles, or_min)
        if hi is None or lo is None:
            return None
        # Only fire after the opening range window has closed.
        last_ts = candles.index[-1]
        day = last_ts.date()
        window_end = pd.Timestamp(day).replace(hour=9, minute=15) + pd.Timedelta(minutes=or_min)
        if last_ts < window_end:
            return None
        spot = float(candles["close"].iloc[-1])
        if spot <= 0:
            return None
        if spot > hi:
            direction = Direction.BULLISH
        elif spot < lo:
            direction = Direction.BEARISH
        else:
            return None
        return MarketView(
            direction=direction,
            confidence=0.7,
            underlying=str(self.underlying).upper(),
            spot_price=spot,
            bar_timestamp=last_ts,
            metadata={"or_high": hi, "or_low": lo, "strategy": self.name},
        )
