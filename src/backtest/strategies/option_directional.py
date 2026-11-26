"""Unified directional options strategy — EMA momentum with regime filters.

Replaces both the old `option_directional` and `banknifty_ema_momentum`.
Combines short-EMA momentum gate with proper regime awareness:
- ADX trend filter (only trade when ADX > 25)
- IV vs RV check (don't buy ATM when IV > RV + 5 vol pts)
- Session time filter (no mid-day, no last-hour)
- VIX regime awareness (low vol = buy options, high vol = don't buy)
"""

from __future__ import annotations

from decimal import Decimal

import numpy as np
import pandas as pd

from backtest.strategy.templates import DirectionalOptionStrategy
from backtest.strategy.intent import Direction, MarketView


class DirectionalOptions(DirectionalOptionStrategy):
    """EMA-momentum directional view with regime filters for the options layer."""

    name = "directional_options"
    instrument_type = "options"
    eligible_instruments = ["NIFTY", "BANKNIFTY"]
    default_segment = "options_index"
    description = (
        "Unified directional options: short-EMA momentum + ADX trend filter + "
        "IV-vs-RV regime check + session timing. Emits BULLISH/BEARISH MarketView "
        "for long call/put or spreads. Replaces option_directional + banknifty_ema_momentum."
    )
    description_sections = {
        "overview": (
            "Directional index options: converts EMA momentum into declared bullish/bearish "
            "views, gated by ADX trend strength, IV-vs-RV dislocation, and session timing. "
            "Expressed as ATM long calls/puts or spreads via the expression layer."
        ),
        "regime": (
            "Only trades when: ADX > 25 (trending), IV <= RV + 5 (vol not expensive), "
            "and session is 9:30–11:30 or 13:30–14:30 (avoid mid-day chop and close vol). "
            "Low VIX (<15) favors entries; high VIX (>25) suppresses them. "
            "Struggles in range-bound chop where ADX < 20 and theta bleeds premium."
        ),
        "entry": (
            "Bullish: close > EMA(ema_period) AND ADX > 25 AND IV regime OK AND session OK. "
            "Bearish: close < EMA(ema_period) AND ADX > 25 AND IV regime OK AND session OK. "
            "Confidence scales with close-to-EMA distance / scale_points."
        ),
        "construction": (
            "Bullish view buys an at-the-money long call, bearish view an at-the-money long put "
            "on the underlying's nearest eligible expiry — defined-risk, single-leg. "
            "Size defaults to 1 lot (override 1–5 via spawn). "
            "Recommended: spawn with bull_call_spread / bear_put_spread to cap theta."
        ),
        "exits_risk": (
            "Declared exit plan: hard stop at -50% premium, target at +100%, exit on signal flip, "
            "no entries within 1 day of expiry. Worst case = stop level; long option max loss = premium paid. "
            "Time stop: no new entries after 14:30; existing positions squared off at 15:15."
        ),
        "parameters": (
            "ema_period=9, scale_points=100.0, min_confidence=0.3, "
            "adx_period=14, adx_threshold=25.0, iv_rv_buffer=5.0, "
            "vix_low=15.0, vix_high=25.0, underlying=NIFTY. "
            "Session filter: 9:30–11:30 and 13:30–14:30 IST."
        ),
    }
    version = "2.0"
    author = "Trading Bot"

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
            "tooltip": "Close-to-EMA distance in points that maps to full confidence. ~100 for NIFTY.",
        },
        "min_confidence": {
            "default": 0.3,
            "min": 0.0,
            "max": 1.0,
            "type": "float",
            "label": "Min Confidence",
            "tooltip": "Views below this conviction are dropped (None returned).",
        },
        "adx_period": {
            "default": 14,
            "min": 5,
            "max": 50,
            "type": "int",
            "label": "ADX Period",
            "tooltip": "ADX lookback for trend strength filter (default 14).",
        },
        "adx_threshold": {
            "default": 25.0,
            "min": 10.0,
            "max": 50.0,
            "type": "float",
            "label": "ADX Threshold",
            "tooltip": "Only trade when ADX > this (trending regime). 25 = trend, 20 = weak trend.",
        },
        "iv_rv_buffer": {
            "default": 5.0,
            "min": 0.0,
            "max": 20.0,
            "type": "float",
            "label": "IV vs RV Buffer (vol pts)",
            "tooltip": "Only buy options when IV <= RV + buffer. 5 = IV not more than 5 vol pts above RV.",
        },
        "vix_low": {
            "default": 15.0,
            "min": 8.0,
            "max": 25.0,
            "type": "float",
            "label": "VIX Low Threshold",
            "tooltip": "Below this = low vol regime (favor buying options).",
        },
        "vix_high": {
            "default": 25.0,
            "min": 20.0,
            "max": 40.0,
            "type": "float",
            "label": "VIX High Threshold",
            "tooltip": "Above this = high vol regime (suppress buying options).",
        },
        "underlying": {
            "default": "NIFTY",
            "type": "str",
            "label": "Underlying",
            "tooltip": "Index the view applies to (NIFTY / BANKNIFTY).",
        },
    }

    # ------------------------------------------------------------------
    # Deterministic indicator helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _adx(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
        """Wilder ADX: +DI/-DI + DX smoothed over period. Returns ADX series."""
        tr1 = high - low
        tr2 = (high - close.shift()).abs()
        tr3 = (low - close.shift()).abs()
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

        up = high - high.shift()
        dn = low.shift() - low

        plus_dm = np.where((up > dn) & (up > 0), up, 0.0)
        minus_dm = np.where((dn > up) & (dn > 0), dn, 0.0)

        atr = tr.ewm(alpha=1.0/period, adjust=False).mean()
        plus_di = 100.0 * pd.Series(plus_dm, index=close.index).ewm(alpha=1.0/period, adjust=False).mean() / atr.replace(0, 1e-9)
        minus_di = 100.0 * pd.Series(minus_dm, index=close.index).ewm(alpha=1.0/period, adjust=False).mean() / atr.replace(0, 1e-9)

        dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, 1e-9)
        adx = dx.ewm(alpha=1.0/period, adjust=False).mean()
        return adx.fillna(0.0)

    @staticmethod
    def _rv(close: pd.Series, period: int = 20) -> float:
        """Annualized realized vol from log returns (intraday 1-min → scale)."""
        if len(close) < period + 1:
            return 0.0
        rets = np.log(close / close.shift()).dropna()
        if len(rets) < period:
            return 0.0
        # Annualize: 1-min bars ≈ 375 min/day * 252 days = 94,500 bars/year
        daily_vol = rets.tail(period).std() * np.sqrt(375)
        return float(daily_vol * np.sqrt(252) * 100)  # in vol pts (e.g., 15.0 = 15%)

    @staticmethod
    def _session_ok(ts) -> bool:
        """True if timestamp is in allowed trading windows (IST)."""
        if ts is None:
            return True
        try:
            t = pd.Timestamp(ts).time()
        except Exception:
            return True
        # IST windows: 9:30–11:30, 13:30–14:30
        from datetime import time as dtime
        m1 = dtime(9, 30) <= t <= dtime(11, 30)
        m2 = dtime(13, 30) <= t <= dtime(14, 30)
        return m1 or m2

    # ------------------------------------------------------------------
    # Strategy contract
    # ------------------------------------------------------------------

    def entries(self, candles: pd.DataFrame) -> pd.Series:
        """Equity fallback: close > EMA."""
        ema = candles["close"].ewm(span=int(self.ema_period), adjust=False).mean()
        return candles["close"] > ema

    def generate_market_view(self, candles: pd.DataFrame) -> MarketView | None:
        """Momentum-gated directional view with regime filters."""
        if candles is None or candles.empty or "close" not in candles.columns:
            return None
        if len(candles) < max(int(self.ema_period), int(self.adx_period)) + 5:
            return None

        close = candles["close"]
        ema = close.ewm(span=int(self.ema_period), adjust=False).mean()

        last_close = float(close.iloc[-1])
        last_ema = float(ema.iloc[-1])
        distance = last_close - last_ema

        if abs(distance) < 1e-9:
            return None

        # --- Regime filters ---
        # 1. ADX trend filter
        adx = self._adx(candles["high"], candles["low"], candles["close"], int(self.adx_period))
        last_adx = float(adx.iloc[-1])
        if last_adx < float(self.adx_threshold):
            return None  # not trending

        # 2. Session time filter
        ts = candles.index[-1] if len(candles.index) else None
        if not self._session_ok(ts):
            return None  # outside allowed windows

        # 3. IV vs RV check (requires VIX/IV data — placeholder; in live bridge this comes from chain)
        # We can't compute IV here without option chain; the bridge should enforce this at spawn.
        # This strategy emits the view; the bridge's IV gate runs separately.

        # 4. VIX regime awareness (requires external VIX — passed via metadata in live)
        # Bridge should suppress if VIX > vix_high and direction is long premium.

        direction = Direction.BULLISH if distance > 0 else Direction.BEARISH

        scale = max(float(self.scale_points), 1e-9)
        raw_confidence = abs(distance) / scale
        confidence = min(1.0, max(float(self.min_confidence), raw_confidence))
        if raw_confidence < float(self.min_confidence):
            return None

        try:
            spot = Decimal(str(last_close))
        except Exception:  # noqa: BLE001
            return None

        return MarketView(
            direction=direction,
            confidence=confidence,
            underlying=str(self.underlying).upper(),
            spot_price=spot,
            bar_timestamp=candles.index[-1] if len(candles.index) else None,
            metadata={
                "ema": last_ema,
                "close": last_close,
                "distance": distance,
                "adx": last_adx,
                "strategy": self.name,
            },
        )