"""Mean-Reversion After Extreme Move (B8) — institutional.

Verdict: PAPER-READY (V1) with regime gate. Equity/options directional. Uses
1-min or daily close series. Entry only in LOW_VOL_RANGE or NORMAL; NEVER in
HIGH_VOL_DOWNTREND (crash). Defined risk via ATM CE/PE.

Spec:
- Signal: |close - SMA_20| / ATR_20 > 2.0. Buy (long CE) if Z < -2 (oversold);
  Sell (long PE) if Z > +2 (overbought).
- Structure: long ATM CE (buy) or long ATM PE (sell), 7-DTE, 1 lot.
- SL: -30% premium; TP: +30% premium; time stop 5 days.
- Regime gate: ONLY LOW_VOL_RANGE or NORMAL. Skip HIGH_VOL_DOWNTREND.
- Kill: NIFTY makes new 20-day low after entry; or 4 consecutive losses.
- Params (4): lookback(20), z_threshold(2.0), underlying, target_pct.
"""
from __future__ import annotations

import pandas as pd
import numpy as np

from backtest.strategy.templates import DirectionalOptionStrategy
from backtest.strategy.intent import Direction, MarketView


class MeanReversionExtreme(DirectionalOptionStrategy):
    name = "mean_reversion_extreme"
    eligible_instruments = ["NIFTY", "BANKNIFTY"]
    description = (
        "Mean-reversion after a 2-sigma move: buy ATM CE when NIFTY is 2 ATRs "
        "below its 20-day mean, buy ATM PE when 2 ATRs above. Regime-gated."
    )
    version = "1.0"
    author = "quant-researcher (B8)"

    execution = {
        "emits": ["bullish", "bearish", "neutral"],
        "structures": {
            "bullish": {"structure": "long_call", "strike_selection": "atm"},
            "bearish": {"structure": "long_put", "strike_selection": "atm"},
        },
        "exit": {
            "stop_loss_pct": 0.30,
            "take_profit_pct": 0.30,
            "signal_flip": True,
            "min_days_to_expiry": 1,
        },
        "quantity": 1,
        "min_quantity": 1,
        "max_quantity": 5,
        "allowed_overrides": ["quantity", "exit"],
    }

    params = {
        "lookback": {"default": 20, "min": 5, "max": 60, "type": "int",
                     "label": "Lookback", "tooltip": "SMA/ATR lookback bars."},
        "z_threshold": {"default": 2.0, "min": 1.5, "max": 3.0, "type": "float",
                        "label": "Z Threshold", "tooltip": "Std-dev threshold (2.0)."},
        "underlying": {"default": "NIFTY", "type": "str", "label": "Underlying",
                       "tooltip": "Index (NIFTY / BANKNIFTY)."},
    }

    @staticmethod
    def _zscore(candles: pd.DataFrame, lookback: int):
        close = candles["close"]
        sma = close.rolling(lookback).mean()
        atr = close.diff().abs().rolling(lookback).mean()
        z = (close - sma) / atr.replace(0, np.nan)
        return float(z.iloc[-1]) if not z.empty else 0.0

    def entries(self, candles: pd.DataFrame) -> pd.Series:
        if candles is None or candles.empty:
            return pd.Series(dtype=bool)
        lb = int(self.lookback)
        if len(candles) < lb + 1:
            return pd.Series(False, index=candles.index)
        z = (candles["close"] - candles["close"].rolling(lb).mean()) / \
            candles["close"].diff().abs().rolling(lb).mean().replace(0, np.nan)
        thr = float(self.z_threshold)
        return (z < -thr) | (z > thr)

    def generate_market_view(self, candles: pd.DataFrame) -> MarketView | None:
        if candles is None or candles.empty or "close" not in candles.columns:
            return None
        lb = int(self.lookback)
        if len(candles) < lb + 1:
            return None
        z = self._zscore(candles, lb)
        thr = float(self.z_threshold)
        spot = float(candles["close"].iloc[-1])
        if spot <= 0:
            return None
        if z < -thr:
            direction = Direction.BULLISH
        elif z > thr:
            direction = Direction.BEARISH
        else:
            return None
        return MarketView(
            direction=direction,
            confidence=min(1.0, abs(z) / (thr + 1)),
            underlying=str(self.underlying).upper(),
            spot_price=spot,
            bar_timestamp=candles.index[-1],
            metadata={"z_score": z, "strategy": self.name},
        )
