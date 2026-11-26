"""Multi-factor market-regime detectors (V1, read-only).

Seven independent factors, each derived from data the platform already holds.
Every factor reports a ``status``, a ``tone`` (positive / negative / neutral /
unknown) and its ``source`` so a proxy can never pass as the real input:

* **VIX**       — India VIX level (reuses :class:`MarketRegimeDetector`).
* **Trend**     — benchmark close vs its long SMA (default 200-day).
* **Volatility**— realised vol vs VIX (is the market pricing its own risk?).
* **IV/RV**     — implied (portfolio) vol vs realised vol dislocation.
* **Breadth**   — share of the NIFTY 200 universe above its long SMA.
* **Session**   — the NSE IST intraday window (opening / trend / chop / gamma).
* **Gamma**     — portfolio net gamma vs the critical threshold.

Nothing here places, resizes or pauses anything. The detectors are pure
functions over plain inputs so they are trivially unit-testable; the service
layer owns caching and DB access.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

from backtest.diary.regimes import NSE_SESSION_REGIMES, classify_ist_session_regime
from backtest.intelligence.regime import LABELS as REGIME_LABELS
from backtest.timekeeping import iso_utc, now_utc

#: Ordered factor keys — the order the UI renders them in.
FACTOR_ORDER = ("vix", "trend", "volatility", "iv_rv", "breadth", "session", "gamma")

#: Factors whose tone expresses market direction (used by the composite).
DIRECTIONAL = ("vix", "trend", "breadth", "gamma")

_SESSION_LABELS = {key: title for key, _span, title, _s, _e in NSE_SESSION_REGIMES}
_SESSION_SPANS = {key: span for key, span, _title, _s, _e in NSE_SESSION_REGIMES}


@dataclass
class Factor:
    """One detector's verdict."""

    key: str
    label: str
    status: str
    tone: str = "unknown"
    value: Optional[float] = None
    display: str = "—"
    detail: str = ""
    source: str = "unknown"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "status": self.status,
            "tone": self.tone,
            "value": None if self.value is None else round(float(self.value), 4),
            "display": self.display,
            "detail": self.detail,
            "source": self.source,
        }


def _mean(values: Sequence[float]) -> Optional[float]:
    if not values:
        return None
    return sum(values) / len(values)


def _sma(closes: Sequence[float], window: int) -> Optional[float]:
    if window <= 0 or len(closes) < window:
        return None
    return _mean(closes[-window:])


# --------------------------------------------------------------------------- #
# Individual detectors
# --------------------------------------------------------------------------- #


def vix_factor(snap: Dict[str, Any]) -> Factor:
    """VIX level → regime (low / moderate / high), straight from the detector."""
    regime = str(snap.get("regime") or "unknown")
    vix = snap.get("vix")
    tone = {"low_vol": "positive", "moderate_vol": "neutral", "high_vol": "negative"}.get(
        regime, "unknown"
    )
    display = f"VIX {float(vix):.2f}" if isinstance(vix, (int, float)) else "VIX —"
    return Factor(
        key="vix",
        label="Volatility (VIX)",
        status=regime,
        tone=tone,
        value=float(vix) if isinstance(vix, (int, float)) else None,
        display=display,
        detail=str(snap.get("label") or REGIME_LABELS.get(regime, regime)),
        source=str(snap.get("source") or "none"),
    )


def trend_factor(closes: Sequence[float], window: int = 200) -> Factor:
    """Benchmark close vs its long SMA; slope confirms direction."""
    if len(closes) < window:
        return Factor(
            key="trend",
            label="Trend",
            status="unknown",
            tone="unknown",
            display=f"need {window} bars",
            detail=f"only {len(closes)} daily bars available",
        )
    sma_now = _sma(closes, window)
    sma_prev = _sma(closes[:-1], window)
    last = float(closes[-1])
    pct = (last / sma_now - 1.0) * 100.0 if sma_now else 0.0
    rising = (
        (sma_now >= sma_prev)
        if sma_prev is not None
        else (last > sma_now)
    )
    if last > sma_now and rising:
        status, tone = "up", "positive"
    elif last < sma_now and not rising:
        status, tone = "down", "negative"
    else:
        status, tone = "sideways", "neutral"
    return Factor(
        key="trend",
        label="Trend",
        status=status,
        tone=tone,
        value=pct,
        display=f"{pct:+.2f}% vs SMA{window}",
        detail=f"close {last:,.2f} vs SMA{window} {sma_now:,.2f} ({'rising' if rising else 'falling'})",
        source=f"1day:{window}sma",
    )


def volatility_factor(realized: Optional[float], vix: Optional[float]) -> Factor:
    """Realised vol vs implied VIX — is the market pricing its own risk?"""
    if realized is None or vix is None or vix <= 0:
        return Factor(
            key="volatility",
            label="Volatility (RV vs VIX)",
            status="unknown",
            tone="unknown",
            display="RV — / VIX —",
            detail="need both a realised-vol sample and a VIX print",
        )
    ratio = float(realized) / float(vix)
    if ratio > 1.2:
        status, tone = "elevated", "negative"
    elif ratio < 0.8:
        status, tone = "calm", "positive"
    else:
        status, tone = "in_line", "neutral"
    return Factor(
        key="volatility",
        label="Volatility (RV vs VIX)",
        status=status,
        tone=tone,
        value=ratio,
        display=f"RV {float(realized):.1f} vs VIX {float(vix):.1f}",
        detail=f"realised/implied ratio {ratio:.2f}",
        source="realized_vol:VIX",
    )


def iv_rv_factor(
    iv: Optional[float], realized: Optional[float], buffer: float = 5.0
) -> Factor:
    """Implied vol vs realised vol — is optionality rich or cheap?

    ``iv`` is the portfolio's implied-vol proxy (volume-weighted IV of open
    option legs); ``realized`` is the benchmark realised vol. A positive edge
    (IV > RV + buffer) means vol is expensive — good for sellers.
    """
    if iv is None or realized is None:
        missing = []
        if iv is None:
            missing.append("implied vol (no option legs with IV)")
        if realized is None:
            missing.append("realised vol (no 1-day bar history)")
        return Factor(
            key="iv_rv",
            label="IV/RV Dislocation",
            status="unknown",
            tone="unknown",
            display="IV — vs RV —",
            detail="no data — " + ", ".join(missing) + "; IV/RV dislocation cannot be computed",
            source="portfolio_iv:realized_vol (no data)",
        )
    edge = float(iv) - float(realized)
    if edge > buffer:
        status, tone = "sell_vol", "positive"
    elif edge < -buffer:
        status, tone = "buy_vol", "negative"
    else:
        status, tone = "fair", "neutral"
    return Factor(
        key="iv_rv",
        label="IV/RV Dislocation",
        status=status,
        tone=tone,
        value=edge,
        display=f"IV {float(iv):.1f} vs RV {float(realized):.1f}",
        detail=f"edge {edge:+.1f} vol pts (buffer ±{buffer:g})",
        source="portfolio_iv:realized_vol",
    )


def breadth_factor(closes_by_symbol: Dict[str, Sequence[float]], window: int = 200) -> Factor:
    """Share of the universe trading above its long SMA."""
    above = 0
    total = 0
    for closes in closes_by_symbol.values():
        sma = _sma(closes, window)
        if sma is None or sma <= 0:
            continue
        total += 1
        if float(closes[-1]) > sma:
            above += 1
    if total == 0:
        return Factor(
            key="breadth",
            label="Breadth",
            status="unknown",
            tone="unknown",
            display=f"need {window} bars",
            detail="no symbol in the universe has enough history",
        )
    pct = above / total * 100.0
    if pct >= 60.0:
        status, tone = "broad", "positive"
    elif pct <= 40.0:
        status, tone = "narrow", "negative"
    else:
        status, tone = "mixed", "neutral"
    return Factor(
        key="breadth",
        label="Breadth",
        status=status,
        tone=tone,
        value=pct,
        display=f"{above}/{total} above SMA{window} ({pct:.0f}%)",
        detail=f"{above} of {total} NIFTY 200 names above their {window}-day average",
        source=f"1day:{window}sma:nifty200",
    )


def session_factor(now: Optional[datetime] = None) -> Factor:
    """The NSE IST intraday window — informational, never directional."""
    stamp = now or now_utc()
    key = classify_ist_session_regime(iso_utc(stamp))
    label = _SESSION_LABELS.get(key, "Off-Hours")
    span = _SESSION_SPANS.get(key, "pre/post / special")
    return Factor(
        key="session",
        label="Session",
        status=key,
        tone="neutral",
        display=label,
        detail=f"{span} IST",
        source="clock:IST",
    )


def gamma_factor(net_gamma: Optional[float], critical: float = -150.0) -> Factor:
    """Portfolio net gamma vs the critical threshold (1% spot move)."""
    if net_gamma is None:
        return Factor(
            key="gamma",
            label="Gamma",
            status="unknown",
            tone="unknown",
            display="net γ —",
            detail="no open option legs to aggregate",
        )
    g = float(net_gamma)
    if g <= critical:
        status, tone = "critical", "negative"
    elif g <= critical * 0.5:
        status, tone = "watch", "negative"
    else:
        status, tone = "ok", "positive"
    return Factor(
        key="gamma",
        label="Gamma",
        status=status,
        tone=tone,
        value=g,
        display=f"net γ {g:,.0f}",
        detail=f"Δ change per 1% spot move (critical {critical:,.0f})",
        source="portfolio_greeks",
    )


# --------------------------------------------------------------------------- #
# Composite
# --------------------------------------------------------------------------- #


def _composite(factors: Dict[str, Factor]) -> Dict[str, Any]:
    tones = [factors[k].tone for k in DIRECTIONAL if k in factors]
    risk_off = sum(1 for t in tones if t == "negative")
    risk_on = sum(1 for t in tones if t == "positive")
    unknown = sum(1 for t in tones if t == "unknown")
    gamma_critical = factors.get("gamma") is not None and factors["gamma"].status == "critical"

    if gamma_critical or risk_off >= 2:
        label, tone = "risk_off", "negative"
    elif risk_on >= 3 and risk_off == 0:
        label, tone = "risk_on", "positive"
    else:
        label, tone = "neutral", "neutral"
    return {
        "label": label,
        "tone": tone,
        "risk_off": risk_off,
        "risk_on": risk_on,
        "neutral": len(DIRECTIONAL) - risk_off - risk_on - unknown,
        "unknown": unknown,
    }


def build_factors(
    *,
    regime_snap: Dict[str, Any],
    benchmark_closes: Optional[Sequence[float]] = None,
    breadth_closes: Optional[Dict[str, Sequence[float]]] = None,
    net_gamma: Optional[float] = None,
    realized_vol: Optional[float] = None,
    atm_iv: Optional[float] = None,
    now: Optional[datetime] = None,
    sma_window: int = 200,
    gamma_critical: float = -150.0,
    iv_rv_buffer: float = 5.0,
    overrides: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Assemble every factor plus a directional composite.

    ``realized_vol`` falls back to the regime snapshot's own value so callers
    that only have the snapshot still get a volatility factor.

    ``overrides`` (dev-gated) manually sets a factor's fields:
    { "trend": { "status": "up", "tone": "positive", "value": 5.0, ... } }
    Used only for testing regime impact without waiting for live flips.
    """
    if realized_vol is None:
        realized_vol = regime_snap.get("realized_vol")
    vix = regime_snap.get("vix")

    factors: Dict[str, Factor] = {
        "vix": vix_factor(regime_snap),
        "trend": trend_factor(benchmark_closes or [], sma_window),
        "volatility": volatility_factor(realized_vol, vix if isinstance(vix, (int, float)) else None),
        "iv_rv": iv_rv_factor(atm_iv, realized_vol, iv_rv_buffer),
        "breadth": breadth_factor(breadth_closes or {}, sma_window),
        "session": session_factor(now),
        "gamma": gamma_factor(net_gamma, gamma_critical),
    }
    # Apply dev-gated manual overrides (shallow merge into each factor).
    if overrides:
        for key, ov in overrides.items():
            if key in factors and isinstance(ov, dict):
                f = factors[key]
                for field in ("status", "tone", "value", "display", "detail", "source"):
                    if field in ov:
                        setattr(f, field, ov[field])
    return {
        "factors": [factors[k].to_dict() for k in FACTOR_ORDER],
        "composite": _composite(factors),
        "sma_window": int(sma_window),
        "generated_at": iso_utc(now or now_utc()),
    }