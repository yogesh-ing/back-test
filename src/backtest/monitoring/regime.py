"""Market regime detection and strategy regime-fit.

PRD §1.3 — "My strategy worked great last month (low vol), now it's bleeding."

Vol index source (always reported as ``vol_index_source``)
-----------------------------------------------------------
1. ``vix``          — a ``vix`` column on the frame (e.g. INDIAVIX from the DB)
2. ``option_iv``    — mean IV of the book's open legs on the benchmark
3. ``realized``     — realized vol stands in for the index (a proxy, labelled
                      as such — a regime call on a proxy is weaker evidence)

Realized vol is annualised from the **actual bar spacing**: the forward-test
runners use minute bars, where ``√252`` would under-state vol by ~√375.

Regime-fit profiles come from ``config/monitoring.yaml → strategy_profiles``
first, then are inferred from the option structure (short premium → low vol)
or strategy name (momentum → trending/higher vol). Anything else is reported
as ``unprofiled`` rather than guessed.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from backtest.monitoring.config import MonitorConfig, StrategyProfile
from backtest.monitoring.models import (
    SEVERITY_INFO,
    SEVERITY_WARNING,
    Alert,
    StrategyBook,
    clean_float,
)

REGIME_LOW = "low_vol"
REGIME_MODERATE = "moderate_vol"
REGIME_HIGH = "high_vol"
REGIME_UNKNOWN = "unknown"

REGIME_LABELS = {
    REGIME_LOW: "Low volatility",
    REGIME_MODERATE: "Moderate volatility",
    REGIME_HIGH: "High volatility",
    REGIME_UNKNOWN: "Unknown (not enough data)",
}

#: Option structures that SELL premium (short gamma / short vega). A bare
#: ``strangle`` is the repo's credit strangle (options_bridge maps it to
#: ShortStrangle: SELL call + SELL put); a bare ``straddle`` follows the same
#: convention. The open legs override either way — see ``legs_side``.
SHORT_PREMIUM_STRUCTURES = {
    "short_straddle", "short_strangle", "strangle", "straddle", "iron_condor",
    "iron_butterfly", "bull_put_spread", "bear_call_spread", "credit_spread",
    "covered_call",
}
#: Structures that BUY premium outright (long gamma / long vega).
LONG_PREMIUM_STRUCTURES = {"long_call", "long_put", "long_straddle", "long_strangle"}
#: Debit spreads — directional, vega-light.
DEBIT_SPREAD_STRUCTURES = {"bull_call_spread", "bear_put_spread"}

TREND_KEYWORDS = ("momentum", "breakout", "donchian", "macd", "trend", "roc", "scalper",
                  "burst", "directional")
REVERSION_KEYWORDS = ("reversion", "rsi", "bollinger", "mean", "pullback", "vwap")


def periods_per_year(index: pd.Index, trading_days: float = 252.0,
                     session_minutes: float = 375.0) -> float:
    """Bars per trading year, from the median bar spacing."""
    if len(index) < 3:
        return trading_days
    try:
        stamps = pd.to_datetime(index)
        spacing = pd.Series(stamps).diff().dropna().median()
        minutes = spacing.total_seconds() / 60.0
    except (TypeError, ValueError, AttributeError):
        return trading_days
    if not minutes or math.isnan(minutes) or minutes >= 60 * 20:
        # daily or slower (weekends inflate the median → still daily)
        return trading_days if minutes < 60 * 24 * 5 else trading_days / 5.0
    return trading_days * max(session_minutes / minutes, 1.0)


def bars_to_frame(bars: List[Dict[str, Any]]) -> pd.DataFrame:
    """Runner bar dicts → OHLC frame indexed by timestamp."""
    if not bars:
        return pd.DataFrame()
    df = pd.DataFrame(bars)
    ts_col = next((c for c in ("ts", "timestamp", "time", "date") if c in df.columns), None)
    if ts_col is not None:
        df.index = pd.to_datetime(df[ts_col], errors="coerce")
        df = df[~df.index.isna()]
    for col in ("open", "high", "low", "close"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.sort_index()


def legs_side(sides: List[str]) -> Optional[str]:
    """``"short"``/``"long"`` when EVERY open option leg is on that side.

    The legs are the ground truth for premium direction — a structure name can
    be ambiguous ("strangle"). Mixed books (spreads, condors) return ``None``
    and keep the structure-name profile.
    """
    norm = {str(x).upper() for x in sides}
    if norm == {"SHORT"}:
        return "short"
    if norm == {"LONG"}:
        return "long"
    return None


class MarketRegimeDetector:
    def __init__(self, config: Optional[MonitorConfig] = None) -> None:
        self.config = config or MonitorConfig()

    # ------------------------------------------------------------------ #
    # Classification
    # ------------------------------------------------------------------ #

    def _classify(self, vol_index: Optional[float], realized: Optional[float]) -> str:
        c = self.config.regime
        if vol_index is None and realized is None:
            return REGIME_UNKNOWN
        vi = vol_index if vol_index is not None else realized
        rv = realized if realized is not None else vi
        if vi > c.high_vol_min or rv > c.realized_high_min:
            return REGIME_HIGH
        if vi < c.low_vol_max and rv < c.realized_low_max:
            return REGIME_LOW
        return REGIME_MODERATE

    def detect(
        self,
        market_data: pd.DataFrame,
        symbol: str = "",
        implied_vol: Optional[float] = None,
        data_source: str = "runner_bars",
    ) -> Dict[str, Any]:
        """Classify the current regime from an OHLC frame (optional ``vix``).

        ``implied_vol`` (annualised decimal) is used as the vol index when the
        frame has no ``vix`` column.
        """
        c = self.config.regime
        out: Dict[str, Any] = {
            "symbol": symbol,
            "data_source": data_source,
            "bars": int(len(market_data)),
            "regime": REGIME_UNKNOWN,
            "label": REGIME_LABELS[REGIME_UNKNOWN],
            "vol_index": None,
            "vol_index_source": None,
            "vol_index_change_pct": None,
            "change_basis": None,
            "history_source": None,
            "realized_vol": None,
            "realized_vol_prev": None,
            "transitioning": False,
            "range_expanding": False,
            "current_range_pct": None,
            "avg_range_pct": None,
            "periods_per_year": None,
            "history": [],
            "timestamp": None,
        }
        if market_data is None or market_data.empty or "close" not in market_data:
            return out
        df = market_data.dropna(subset=["close"])
        if len(df) < 3:
            return out
        out["timestamp"] = str(df.index[-1])

        ppy = periods_per_year(df.index, c.trading_days, c.session_minutes)
        out["periods_per_year"] = clean_float(ppy, 1)
        rets = np.log(df["close"]).diff()
        window = min(c.realized_window, max(len(df) - 1, 2))
        rv_series = rets.rolling(window, min_periods=max(window // 2, 2)).std() * math.sqrt(
            ppy) * 100.0
        realized = clean_float(rv_series.iloc[-1], 2)
        lb = c.transition_lookback
        prev_rv = clean_float(rv_series.iloc[-1 - lb], 2) if len(rv_series) > lb else None
        out["realized_vol"] = realized
        out["realized_vol_prev"] = prev_rv

        # Vol index + its history for the chart. The transition test always
        # compares a series with ITSELF: the book's option IV is a point
        # reading with no history, so its transition is judged on realized vol
        # (IV now vs realized then would read a structural IV/RV gap as a
        # "−90% collapse").
        if "vix" in df.columns and df["vix"].notna().any():
            vix = pd.to_numeric(df["vix"], errors="coerce").ffill()
            vol_index = clean_float(vix.iloc[-1], 2)
            now_v = vol_index
            prev_v = clean_float(vix.iloc[-1 - lb], 2) if len(vix) > lb else None
            source, basis, hist = "vix", "vix", vix
        elif implied_vol:
            vol_index = clean_float(implied_vol * 100.0, 2)
            now_v, prev_v = realized, prev_rv
            source, basis, hist = "option_iv", "realized", rv_series
        else:
            vol_index, now_v, prev_v = realized, realized, prev_rv
            source, basis, hist = "realized", "realized", rv_series
        out["vol_index"] = vol_index
        out["vol_index_source"] = source
        out["history_source"] = basis

        if now_v is not None and prev_v:
            change = (now_v - prev_v) / prev_v * 100.0
            out["change_basis"] = basis
            out["vol_index_change_pct"] = clean_float(change, 2)
            out["transitioning"] = bool(abs(change) > c.transition_change_pct)

        if {"high", "low"}.issubset(df.columns):
            rng = (df["high"] - df["low"]) / df["close"] * 100.0
            rw = min(c.range_window, len(rng))
            avg = rng.rolling(rw, min_periods=max(rw // 2, 2)).mean().shift(1).iloc[-1]
            cur = rng.iloc[-1]
            out["current_range_pct"] = clean_float(cur, 3)
            out["avg_range_pct"] = clean_float(avg, 3)
            if out["avg_range_pct"]:
                out["range_expanding"] = bool(cur > avg * c.range_expansion_mult)

        regime = self._classify(vol_index, realized)
        out["regime"] = regime
        out["label"] = REGIME_LABELS[regime]
        tail = hist.dropna().tail(60)
        out["history"] = [
            {"ts": str(ts), "value": clean_float(v, 2)} for ts, v in tail.items()
        ]
        return out

    # ------------------------------------------------------------------ #
    # Strategy fit
    # ------------------------------------------------------------------ #

    def profile_for(self, book: StrategyBook, side: Optional[str] = None
                    ) -> Optional[StrategyProfile]:
        """Configured profile, else inferred. ``side`` is :func:`legs_side` of
        the strategy's open option legs — it outranks the structure name."""
        profiles = self.config.strategy_profiles
        for key in (book.strategy_name, book.strategy_kind, book.structure_type):
            if key and str(key).lower() in profiles:
                return profiles[str(key).lower()]
        c = self.config.regime
        st = (book.structure_type or "").lower()
        if side == "short":
            return StrategyProfile(REGIME_LOW, [0.0, c.low_vol_max + 1.0],
                                   ["short_premium", "short_gamma"], "inferred:legs")
        if side == "long":
            return StrategyProfile(REGIME_HIGH, [c.low_vol_max - 1.0, 100.0],
                                   ["long_premium", "long_gamma"], "inferred:legs")
        if st in SHORT_PREMIUM_STRUCTURES:
            return StrategyProfile(REGIME_LOW, [0.0, c.low_vol_max + 1.0],
                                   ["short_premium", "short_gamma"], "inferred:structure")
        if st in LONG_PREMIUM_STRUCTURES:
            return StrategyProfile(REGIME_HIGH, [c.low_vol_max - 1.0, 100.0],
                                   ["long_premium", "long_gamma"], "inferred:structure")
        if st in DEBIT_SPREAD_STRUCTURES:
            return StrategyProfile(REGIME_MODERATE, [0.0, c.high_vol_min + 5.0],
                                   ["directional", "defined_risk"], "inferred:structure")
        name = f"{book.strategy_kind} {book.strategy_name}".lower()
        if any(k in name for k in TREND_KEYWORDS):
            return StrategyProfile(REGIME_HIGH, [c.low_vol_max - 3.0, 100.0],
                                   ["trend"], "inferred:name")
        if any(k in name for k in REVERSION_KEYWORDS):
            return StrategyProfile(REGIME_LOW, [0.0, c.high_vol_min - 3.0],
                                   ["mean_reversion"], "inferred:name")
        return None

    def strategy_fit(self, book: StrategyBook, regime: Dict[str, Any],
                     side: Optional[str] = None) -> Dict[str, Any]:
        profile = self.profile_for(book, side)
        vol = regime.get("vol_index")
        row: Dict[str, Any] = {
            "strategy_id": book.strategy_id,
            "strategy_name": book.strategy_name,
            "strategy_kind": book.strategy_kind,
            "structure_type": book.structure_type,
            "current_regime": regime.get("regime"),
            "current_vol": vol,
            "profile": profile.to_dict() if profile else None,
        }
        if profile is None:
            row.update(fit="unprofiled",
                       recommendation="Add a profile in config/monitoring.yaml "
                                      "(strategy_profiles) to get regime-fit checks.")
            return row
        if regime.get("regime") == REGIME_UNKNOWN or vol is None:
            row.update(fit="unknown", recommendation="Regime unknown — not enough bars yet.")
            return row
        lo, hi = (profile.optimal_vol_range or [0.0, 1000.0])[:2]
        in_range = lo <= vol <= hi
        match = profile.optimal_regime in (None, regime.get("regime"))
        if match and in_range:
            fit = "optimal"
        elif not in_range:
            fit = "mismatch"
        else:
            fit = "acceptable"
        row["fit"] = fit
        row["recommendation"] = self._recommendation(fit, profile, regime)
        return row

    @staticmethod
    def _recommendation(fit: str, profile: StrategyProfile, regime: Dict[str, Any]) -> str:
        if fit == "optimal":
            return "Continue normal operation."
        if fit == "mismatch":
            if "short_premium" in profile.tags:
                return ("Vol is outside this premium seller's range — reduce size or pause "
                        "new entries; tighten stops.")
            if "long_premium" in profile.tags:
                return "Vol is cheap for a premium buyer — decay will dominate; be selective."
            return "Outside the optimised vol range — monitor closely, consider tighter stops."
        if regime.get("transitioning"):
            return "Acceptable, but the regime is shifting — watch the next few sessions."
        return "Acceptable — monitor for regime shifts."

    @staticmethod
    def _current_vol_text(regime: Dict[str, Any]) -> str:
        """"current 12.96" — plus realized vol when it is a different series,
        since either one can put the book in the high-vol regime."""
        text = f"current {regime.get('vol_index')}"
        if regime.get("vol_index_source") != "realized" and regime.get("realized_vol") is not None:
            text += f" ({regime.get('vol_index_source')}), realized {regime.get('realized_vol')}"
        return text

    def _transition_message(self, regime: Dict[str, Any], change: float) -> str:
        bars = self.config.regime.transition_lookback
        if regime.get("change_basis") == "vix":
            what = f"Vol index {regime.get('vol_index')}"
        else:
            what = f"Realized vol {regime.get('realized_vol')}"
            if regime.get("vol_index_source") == "option_iv":
                what += f" (book IV {regime.get('vol_index')})"
        return f"{what} moved {change:+.1f}% over {bars} bars — regime in transition."

    def check(self, regime: Dict[str, Any], fits: List[Dict[str, Any]]) -> List[Alert]:
        alerts: List[Alert] = []
        symbol = regime.get("symbol") or "market"
        if regime.get("transitioning"):
            change = regime.get("vol_index_change_pct") or 0.0
            direction = "spiking" if change > 0 else "collapsing"
            alerts.append(
                Alert(
                    key=f"regime:transition:{symbol}",
                    category="regime",
                    severity=SEVERITY_WARNING if change > 0 else SEVERITY_INFO,
                    title=f"Volatility {direction} on {symbol} ({change:+.0f}%)",
                    message=self._transition_message(regime, change),
                    metric="vol_index_change_pct",
                    value=change,
                    threshold=self.config.regime.transition_change_pct,
                    subject=symbol,
                )
            )
        if regime.get("range_expanding"):
            alerts.append(
                Alert(
                    key=f"regime:range:{symbol}",
                    category="regime",
                    severity=SEVERITY_INFO,
                    title=f"Range expansion on {symbol}",
                    message=(
                        f"Latest bar range {regime.get('current_range_pct')}% vs "
                        f"{regime.get('avg_range_pct')}% average."
                    ),
                    subject=symbol,
                )
            )
        mismatched = [f for f in fits if f.get("fit") == "mismatch"]
        for f in mismatched:
            tags = (f.get("profile") or {}).get("tags") or []
            sev = SEVERITY_WARNING
            if "short_premium" in tags and regime.get("regime") == REGIME_HIGH:
                sev = "critical"
            vr = (f.get("profile") or {}).get("optimal_vol_range") or []
            alerts.append(
                Alert(
                    key=f"regime:fit:{f['strategy_id']}",
                    category="regime",
                    severity=sev,
                    title=f"{f['strategy_name']} is outside its regime",
                    message=(
                        f"Optimised for vol {vr[0]:g}–{vr[1]:g}" if len(vr) == 2 else
                        "Outside its optimised range"
                    ) + f"; {self._current_vol_text(regime)} ({regime.get('label')}).",
                    recommendation=f.get("recommendation"),
                    metric="vol_index",
                    value=regime.get("vol_index"),
                    subject=f["strategy_id"],
                )
            )
        return alerts
