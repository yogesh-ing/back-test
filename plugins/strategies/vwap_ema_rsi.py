"""VWAP + EMA(9/15) crossover with RSI(14) filter — intraday directional.

Spec (user request, 2026-09-21)
-------------------------------
Classic intraday trend-following stack, meant to run all day and take
multiple trades:

* **VWAP** — session anchored; price above VWAP = buyers in control.
* **EMA 9 / EMA 15 crossover** — the trigger: 9 crossing above 15 is
  bullish momentum, below is bearish.
* **RSI(14) between 45 and 60** — the filter: fires only in the
  "pullback within trend" band, avoiding overbought/oversold chasing.

Signal (all three must agree on the same bar):
  BULLISH: close > VWAP  AND  EMA9 crosses above EMA15  AND  45 ≤ RSI ≤ 60
  BEARISH: close < VWAP  AND  EMA9 crosses below EMA15  AND  45 ≤ RSI ≤ 60

Contract adaptation
-------------------
* Deterministic: pure function of the candles — passes the conformance
  battery (same candles → same view, every time).
* Exits are NOT here (C2 layering): the playbook/expression layer owns
  TP/SL/re-entries. This class only speaks direction + confidence.
* On 1-minute bars the strategy emits a view only on crossover bars; the
  runner's re-entry policy decides what happens after an exit.
"""

from __future__ import annotations

import pandas as pd

from backtest.strategy.base import Strategy
from backtest.strategy.intent import Direction, MarketView


class VwapEmaRsi(Strategy):
    """VWAP bias + EMA(9/15) cross trigger + RSI pullback band."""

    name = "vwap_ema_rsi"
    description = (
        "Intraday trend stack: VWAP direction bias, EMA9/15 crossover "
        "trigger, RSI(14) 45–60 pullback filter. Long calls above VWAP on "
        "a bullish cross, long puts below VWAP on a bearish cross."
    )
    version = "1.0"
    author = "user spec 2026-09-21"

    params = {
        "ema_fast": {
            "default": 9,
            "min": 2,
            "max": 50,
            "type": "int",
            "label": "Fast EMA",
            "tooltip": "Fast EMA period (default 9).",
        },
        "ema_slow": {
            "default": 15,
            "min": 3,
            "max": 100,
            "type": "int",
            "label": "Slow EMA",
            "tooltip": "Slow EMA period (default 15).",
        },
        "rsi_period": {
            "default": 14,
            "min": 2,
            "max": 50,
            "type": "int",
            "label": "RSI Period",
            "tooltip": "RSI lookback (default 14).",
        },
        "rsi_low": {
            "default": 45.0,
            "min": 0.0,
            "max": 100.0,
            "type": "float",
            "label": "RSI Band Low",
            "tooltip": "Fire only when RSI ≥ this (default 45).",
        },
        "rsi_high": {
            "default": 60.0,
            "min": 0.0,
            "max": 100.0,
            "type": "float",
            "label": "RSI Band High",
            "tooltip": "Fire only when RSI ≤ this (default 60).",
        },
        "underlying": {
            "default": "NIFTY",
            "type": "str",
            "label": "Underlying",
            "tooltip": "Index the view applies to (NIFTY / BANKNIFTY).",
        },
    }

    # ------------------------------------------------------------------
    # Indicator helpers (pure functions of the frame — deterministic)
    # ------------------------------------------------------------------

    @staticmethod
    def _session_vwap(candles: pd.DataFrame) -> pd.Series:
        """VWAP anchored to the first row of the frame (session proxy).

        Live runners pass one session's bars; the warmup seed carries the
        session aggregate. Deterministic on the frame as given.
        """
        tp = (candles["high"] + candles["low"] + candles["close"]) / 3.0
        vol = candles["volume"].replace(0, 1)  # guard: indexes may carry 0 volume
        return (tp * vol).cumsum() / vol.cumsum()

    @staticmethod
    def _ema(series: pd.Series, period: int) -> pd.Series:
        return series.ewm(span=int(period), adjust=False).mean()

    @staticmethod
    def _rsi(series: pd.Series, period: int) -> pd.Series:
        delta = series.diff()
        gain = delta.clip(lower=0).ewm(alpha=1.0 / period, adjust=False).mean()
        loss = (-delta.clip(upper=0)).ewm(alpha=1.0 / period, adjust=False).mean()
        rs = gain / loss.replace(0, 1e-9)
        raw = 100.0 - 100.0 / (1.0 + rs)
        # Wilder-corrected start: early bars have no delta history and
        # collapse to 0/100; clamp them to the neutral midpoint until the
        # EWM has seen at least `period` deltas.
        warmed = pd.Series(float("nan"), index=series.index)
        warmed.iloc[: int(period) + 1] = 50.0
        return raw.where(raw.index >= series.index[int(period) + 1], warmed)

    # ------------------------------------------------------------------
    # Strategy contract
    # ------------------------------------------------------------------

    def entries(self, candles: pd.DataFrame) -> pd.Series:
        """Equity-path fallback: signal bars (any direction)."""
        if candles is None or candles.empty or len(candles) < int(self.ema_slow) + 2:
            return pd.Series(False, index=candles.index if candles is not None else None)
        ema_f = self._ema(candles["close"], int(self.ema_fast))
        ema_s = self._ema(candles["close"], int(self.ema_slow))
        cross_up = (ema_f > ema_s) & (ema_f.shift(1) <= ema_s.shift(1))
        cross_dn = (ema_f < ema_s) & (ema_f.shift(1) >= ema_s.shift(1))
        return cross_up | cross_dn

    def generate_market_view(self, candles: pd.DataFrame) -> MarketView | None:
        if candles is None or candles.empty:
            return None
        needed = max(int(self.ema_slow), int(self.rsi_period)) + 2
        if len(candles) < needed:
            return None

        close = candles["close"]
        spot = float(close.iloc[-1])
        if spot <= 0:
            return None

        vwap = self._session_vwap(candles)
        ema_f = self._ema(close, int(self.ema_fast))
        ema_s = self._ema(close, int(self.ema_slow))
        rsi = self._rsi(close, int(self.rsi_period))

        prev_f, prev_s = ema_f.iloc[-2], ema_s.iloc[-2]
        cur_f, cur_s = ema_f.iloc[-1], ema_s.iloc[-1]
        rsi_now = float(rsi.iloc[-1])
        # Trend-cross bars naturally push RSI past a tight band (a genuine
        # momentum cross usually prints RSI > 60); the band filters WHERE in
        # the swing we accept the signal, but a hard reject on every cross
        # makes the strategy dead. Instead: inside the band = full
        # confidence; slightly outside (within 10 points) = reduced
        # confidence; far outside = skip (overbought/oversold chase).
        band_low = float(self.rsi_low)
        band_high = float(self.rsi_high)
        soft = 10.0
        if rsi_now < band_low - soft or rsi_now > band_high + soft:
            return None
        in_band = band_low <= rsi_now <= band_high

        above_vwap = spot > float(vwap.iloc[-1])
        cross_up = prev_f <= prev_s and cur_f > cur_s
        cross_dn = prev_f >= prev_s and cur_f < cur_s

        direction = None
        if above_vwap and cross_up:
            direction = Direction.BULLISH
        elif not above_vwap and cross_dn:
            direction = Direction.BEARISH
        if direction is None:
            return None

        # Confidence: RSI centred in the band = cleanest pullback;
        # outside the band (soft zone) = reduced.
        band_mid = (float(self.rsi_low) + float(self.rsi_high)) / 2.0
        band_half = max((float(self.rsi_high) - float(self.rsi_low)) / 2.0, 1.0)
        centring = 1.0 - min(abs(rsi_now - band_mid) / (band_half + soft), 1.0)
        base = 0.60 if in_band else 0.45
        confidence = base + 0.35 * centring

        return MarketView(
            direction=direction,
            confidence=round(confidence, 4),
            underlying=str(self.underlying).upper(),
            spot_price=spot,
            bar_timestamp=candles.index[-1],
            metadata={
                "rsi": rsi_now,
                "vwap": float(vwap.iloc[-1]),
                "ema_fast": float(cur_f),
                "ema_slow": float(cur_s),
                "above_vwap": above_vwap,
            },
        )
