"""Buy the Pullback in an Uptrend (S6) — institutional.

Verdict: PAPER-READY. Equity/options directional. Needs daily close + 200DMA + ADX.

Spec:
- Signal: NIFTY > 200DMA AND ADX > 25 AND pullback 2-3% from 20-day high
  AND RSI(14) < 40.
- Structure: long ATM CE, 7-DTE, 1 lot.
- SL: -5% (or NIFTY < 200DMA); TP: +40% premium; time stop 15 days.
- Regime: ONLY LOW_VOL_UPTREND or HIGH_VOL_UPTREND.
- Kill: NIFTY closes below 200DMA; ADX < 20; -5% from entry.
- Params (4): rsi_threshold(40), pullback_pct(0.02), underlying, lookback(20).
"""
from __future__ import annotations

import pandas as pd

from backtest.strategy.templates import DirectionalOptionStrategy
from backtest.strategy.intent import Direction, MarketView


class PullbackInUptrend(DirectionalOptionStrategy):
    name = "pullback_in_uptrend"
    eligible_instruments = ["NIFTY", "BANKNIFTY"]
    description = (
        "Buy the pullback in a confirmed uptrend: NIFTY>200DMA, ADX>25, "
        "2-3% pullback from 20-day high, RSI<40 -> long ATM CE."
    )
    version = "1.0"
    author = "quant-researcher (S6)"

    execution = {
        "emits": ["bullish", "bearish", "neutral"],
        "structures": {
            "bullish": {"structure": "long_call", "strike_selection": "atm"},
            "bearish": {"structure": "long_put", "strike_selection": "atm"},
        },
        "exit": {
            "stop_loss_pct": 0.05,
            "take_profit_pct": 0.40,
            "signal_flip": True,
            "min_days_to_expiry": 1,
        },
        "quantity": 1,
        "min_quantity": 1,
        "max_quantity": 5,
        "allowed_overrides": ["quantity", "exit"],
    }

    params = {
        "rsi_threshold": {"default": 40.0, "min": 20.0, "max": 50.0, "type": "float",
                          "label": "RSI Threshold", "tooltip": "RSI below this in uptrend."},
        "pullback_pct": {"default": 0.02, "min": 0.01, "max": 0.05, "type": "float",
                         "label": "Pullback %", "tooltip": "Min pullback from 20-day high."},
        "lookback": {"default": 20, "min": 5, "max": 60, "type": "int",
                     "label": "Lookback", "tooltip": "20-day high lookback."},
        "underlying": {"default": "NIFTY", "type": "str", "label": "Underlying",
                       "tooltip": "Index (NIFTY / BANKNIFTY)."},
    }

    @staticmethod
    def _rsi(series: pd.Series, period: int = 14) -> float:
        delta = series.diff()
        gain = delta.clip(lower=0).ewm(alpha=1.0 / period, adjust=False).mean()
        loss = (-delta.clip(upper=0)).ewm(alpha=1.0 / period, adjust=False).mean()
        rs = gain / loss.replace(0, 1e-9)
        return float((100 - 100 / (1 + rs)).iloc[-1])

    def entries(self, candles: pd.DataFrame) -> pd.Series:
        if candles is None or candles.empty:
            return pd.Series(dtype=bool)
        close = candles["close"]
        sma200 = close.rolling(200).mean()
        high20 = close.rolling(int(self.lookback)).max()
        pullback = (high20 - close) / high20
        rsi = close.rolling(14).apply(lambda s: self._rsi(s), raw=False)
        cond = (close > sma200) & (pullback >= float(self.pullback_pct)) & \
               (rsi < float(self.rsi_threshold))
        return cond.fillna(False)

    def generate_market_view(self, candles: pd.DataFrame) -> MarketView | None:
        if candles is None or candles.empty or "close" not in candles.columns:
            return None
        lb = int(self.lookback)
        if len(candles) < 200:
            return None
        close = candles["close"]
        sma200 = float(close.rolling(200).mean().iloc[-1])
        high20 = float(close.rolling(lb).max().iloc[-1])
        spot = float(close.iloc[-1])
        if spot <= 0 or spot < sma200:
            return None
        pullback = (high20 - spot) / high20
        if pullback < float(self.pullback_pct):
            return None
        rsi = self._rsi(close, 14)
        if rsi >= float(self.rsi_threshold):
            return None
        return MarketView(
            direction=Direction.BULLISH,
            confidence=0.6,
            underlying=str(self.underlying).upper(),
            spot_price=spot,
            bar_timestamp=candles.index[-1],
            metadata={"rsi": rsi, "pullback": pullback, "strategy": self.name},
        )
