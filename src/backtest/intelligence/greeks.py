"""Portfolio Greeks aggregation across every open position.

Pure computation over :class:`ExposureLeg` rows — the collectors
(:mod:`backtest.intelligence.collectors`) turn runner books, option bridges
and the manual options book into legs; this module never touches them.

Units (documented in ``config/portfolio_intelligence.yaml``):

* ``net_delta`` — share-equivalent units (Σ sign · Δ · units). ₹ P&L per
  1-point move of the underlying.
* ``net_gamma`` — change in net delta for a **1% move** of each underlying
  (Σ sign · Γ · units · S · 1%). Raw per-point gamma is not comparable across
  NIFTY (24,800) and a ₹300 stock; the 1%-normalised figure is.
* ``net_vega`` — ₹ per +1 implied-volatility point.
* ``net_theta`` — ₹ per calendar day.

Model: Black-Scholes (the same :class:`~backtest.options.greeks.BlackScholes`
the option books price with), using each leg's own implied vol where known.
Scenarios are **full revaluations** (not Taylor approximations), so they
capture the convexity the Greeks only hint at.

Expiry day: a leg expiring today is priced with half a trading day left
instead of zero — at T=0 Black-Scholes gamma collapses to 0, which would
hide exactly the expiry-day gamma a short straddle carries.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from backtest.options.greeks import BlackScholes

#: Fraction of a year used for a leg that expires today (half a session).
EXPIRY_DAY_YEARS = 0.5 / 365.0
#: IV floor for scenario revaluation (a -5 pt shock on a 4% IV must not go <0).
MIN_VOL = 0.01
#: Fallback IV when a leg's own vol is unknown — flagged in ``warnings``.
DEFAULT_IV = 0.15


@dataclass
class ExposureLeg:
    """One priced leg of exposure, platform-agnostic."""

    source_id: str
    source_label: str
    strategy: str
    mode: str
    position_key: str
    kind: str  # "option" | "equity"
    underlying: str
    units: float  # absolute quantity in underlying units (lots × lot size)
    sign: int  # +1 long, -1 short
    spot: Optional[float] = None
    option_type: Optional[str] = None  # CE / PE
    strike: Optional[float] = None
    expiry: Optional[date] = None
    iv: Optional[float] = None
    reference_date: Optional[date] = None
    structure_type: Optional[str] = None
    current_price: Optional[float] = None
    stale: bool = False

    def notional(self) -> float:
        """Gross underlying notional this leg controls (units × spot)."""
        px = self.spot if self.spot else (self.current_price or 0.0)
        return abs(float(self.units)) * float(px or 0.0)


@dataclass
class LegGreeks:
    delta: float = 0.0  # share-equivalents
    gamma_pt: float = 0.0  # delta change per 1 point
    gamma_1pct: float = 0.0  # delta change per 1% move
    gamma_rupees_1pct: float = 0.0  # convexity P&L of a 1% move
    vega: float = 0.0  # ₹ per vol point
    theta: float = 0.0  # ₹ per day
    value: float = 0.0  # signed mark value (₹)
    iv_used: Optional[float] = None
    iv_assumed: bool = False
    years: Optional[float] = None


def _bias(value: float, pos: str, neg: str, flat: str = "neutral", eps: float = 1e-9) -> str:
    if value > eps:
        return pos
    if value < -eps:
        return neg
    return flat


def _r(x: float, nd: int = 2) -> float:
    return round(float(x), nd) if math.isfinite(x) else 0.0


class PortfolioGreeksAggregator:
    """Aggregate Greeks + scenario P&L across a list of :class:`ExposureLeg`."""

    def __init__(self, risk_free_rate: float = 0.06, default_iv: float = DEFAULT_IV) -> None:
        self.bs = BlackScholes(risk_free_rate=risk_free_rate, volatility=default_iv)
        self.default_iv = float(default_iv)

    # ------------------------------------------------------------------ #
    # Per-leg
    # ------------------------------------------------------------------ #

    def _years(self, leg: ExposureLeg) -> Optional[float]:
        if leg.expiry is None:
            return None
        ref = leg.reference_date or datetime.now(timezone.utc).date()
        expiry = leg.expiry.date() if isinstance(leg.expiry, datetime) else leg.expiry
        days = (expiry - ref).days
        if days < 0:
            return None  # already expired — settlement, not risk
        return EXPIRY_DAY_YEARS if days == 0 else days / 365.0

    def leg_greeks(self, leg: ExposureLeg) -> Optional[LegGreeks]:
        """Greeks for one leg, or ``None`` when they cannot be computed."""
        qty = float(leg.sign) * abs(float(leg.units))
        if leg.kind != "option":
            px = float(leg.spot or leg.current_price or 0.0)
            if px <= 0:
                return None
            return LegGreeks(delta=qty, value=qty * px)
        spot = float(leg.spot or 0.0)
        strike = float(leg.strike or 0.0)
        opt = str(leg.option_type or "").upper()
        years = self._years(leg)
        if spot <= 0 or strike <= 0 or opt not in ("CE", "PE") or years is None:
            return None
        vol = leg.iv if leg.iv and leg.iv > 0 else None
        assumed = vol is None
        vol = float(vol or self.default_iv)
        g = self.bs.greeks(
            spot=spot, strike=strike, expiry_years=years, option_type=opt, volatility=vol
        )
        gamma_pt = qty * g.gamma
        move = spot * 0.01
        return LegGreeks(
            delta=qty * g.delta,
            gamma_pt=gamma_pt,
            gamma_1pct=gamma_pt * move,
            gamma_rupees_1pct=0.5 * gamma_pt * move * move,
            vega=qty * g.vega,
            theta=qty * g.theta / 365.0,
            value=qty * g.price,
            iv_used=vol,
            iv_assumed=assumed,
            years=years,
        )

    def scenario_pnl(self, leg: ExposureLeg, spot_pct: float, vol_pts: float) -> Optional[float]:
        """Full-revaluation P&L of one leg under a spot / vol shock."""
        qty = float(leg.sign) * abs(float(leg.units))
        if leg.kind != "option":
            px = float(leg.spot or leg.current_price or 0.0)
            return qty * px * spot_pct if px > 0 else None
        spot = float(leg.spot or 0.0)
        strike = float(leg.strike or 0.0)
        opt = str(leg.option_type or "").upper()
        years = self._years(leg)
        if spot <= 0 or strike <= 0 or opt not in ("CE", "PE") or years is None:
            return None
        vol = float(leg.iv if leg.iv and leg.iv > 0 else self.default_iv)
        base = self.bs.price(spot, strike, years, opt, volatility=vol)
        shocked = self.bs.price(
            spot * (1.0 + spot_pct),
            strike,
            years,
            opt,
            volatility=max(MIN_VOL, vol + vol_pts / 100.0),
        )
        return qty * (shocked - base)

    # ------------------------------------------------------------------ #
    # Portfolio
    # ------------------------------------------------------------------ #

    def calculate(
        self,
        legs: Iterable[ExposureLeg],
        scenarios: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        t0 = time.perf_counter()
        legs = list(legs)
        totals = LegGreeks()
        by_source: Dict[str, Dict[str, Any]] = {}
        by_underlying: Dict[str, Dict[str, Any]] = {}
        missing: List[Dict[str, Any]] = []
        assumed_iv: List[str] = []
        position_keys: set = set()
        priced: List[ExposureLeg] = []

        for leg in legs:
            src = by_source.setdefault(
                leg.source_id,
                {
                    "source_id": leg.source_id,
                    "label": leg.source_label,
                    "strategy": leg.strategy,
                    "mode": leg.mode,
                    "delta": 0.0,
                    "gamma": 0.0,
                    "gamma_rupees_1pct": 0.0,
                    "vega": 0.0,
                    "theta": 0.0,
                    "_positions": set(),
                    "legs": 0,
                    "underlyings": set(),
                    "stale": False,
                },
            )
            src["_positions"].add(leg.position_key)
            src["legs"] += 1
            src["underlyings"].add(leg.underlying)
            src["stale"] = src["stale"] or leg.stale
            position_keys.add((leg.source_id, leg.position_key))
            g = self.leg_greeks(leg)
            if g is None:
                missing.append(
                    {
                        "source": leg.source_label,
                        "position_key": leg.position_key,
                        "underlying": leg.underlying,
                        "reason": "no spot/expiry/strike — Greeks unavailable",
                    }
                )
                continue
            priced.append(leg)
            if g.iv_assumed:
                assumed_iv.append(f"{leg.underlying} {leg.strike or ''}{leg.option_type or ''}")
            for name in ("delta", "gamma_1pct", "gamma_rupees_1pct", "vega", "theta", "value"):
                setattr(totals, name, getattr(totals, name) + getattr(g, name))
            src["delta"] += g.delta
            src["gamma"] += g.gamma_1pct
            src["gamma_rupees_1pct"] += g.gamma_rupees_1pct
            src["vega"] += g.vega
            src["theta"] += g.theta
            u = by_underlying.setdefault(
                leg.underlying,
                {
                    "underlying": leg.underlying,
                    "delta": 0.0,
                    "gamma": 0.0,
                    "gamma_pt": 0.0,
                    "vega": 0.0,
                    "theta": 0.0,
                    "spot": leg.spot,
                    "legs": 0,
                    "delta_rupees_1pct": 0.0,
                },
            )
            u["delta"] += g.delta
            u["gamma"] += g.gamma_1pct
            u["gamma_pt"] += g.gamma_pt
            u["vega"] += g.vega
            u["theta"] += g.theta
            u["legs"] += 1
            u["spot"] = leg.spot or u["spot"]
            u["delta_rupees_1pct"] += g.delta * float(leg.spot or leg.current_price or 0) * 0.01

        # Scenarios — full revaluation per leg.
        scenario_rows: List[Dict[str, Any]] = []
        for sc in scenarios or []:
            pnl = 0.0
            for leg in priced:
                value = self.scenario_pnl(
                    leg, float(sc.get("spot_pct", 0.0)), float(sc.get("vol_pts", 0.0))
                )
                if value is not None:
                    pnl += value
            scenario_rows.append(
                {
                    "key": sc.get("key"),
                    "label": sc.get("label") or sc.get("key"),
                    "spot_pct": sc.get("spot_pct", 0.0),
                    "vol_pts": sc.get("vol_pts", 0.0),
                    "pnl": _r(pnl, 0),
                }
            )

        delta_rupees_1pct = sum(u["delta_rupees_1pct"] for u in by_underlying.values())
        total_abs_gamma = sum(abs(s["gamma"]) for s in by_source.values()) or 0.0
        breakdown = []
        for s in by_source.values():
            positions = len(s.pop("_positions"))
            breakdown.append(
                {
                    **{k: v for k, v in s.items() if k not in ("underlyings",)},
                    "underlyings": sorted(s["underlyings"]),
                    "delta": _r(s["delta"]),
                    "gamma": _r(s["gamma"]),
                    "gamma_rupees_1pct": _r(s["gamma_rupees_1pct"], 0),
                    "vega": _r(s["vega"]),
                    "theta": _r(s["theta"]),
                    "positions": positions,
                    "gamma_share": (
                        _r(abs(s["gamma"]) / total_abs_gamma, 4) if total_abs_gamma else 0.0
                    ),
                }
            )
        breakdown.sort(key=lambda r: r["gamma"])  # most short-gamma first
        for u in by_underlying.values():
            for k in ("delta", "gamma", "vega", "theta", "delta_rupees_1pct"):
                u[k] = _r(u[k])
            u["gamma_pt"] = round(u["gamma_pt"], 6)

        net_delta = totals.delta
        net_gamma = totals.gamma_1pct
        net_vega = totals.vega
        net_theta = totals.theta
        return {
            "net_delta": _r(net_delta),
            "net_gamma": _r(net_gamma),
            "net_vega": _r(net_vega),
            "net_theta": _r(net_theta),
            "delta_rupees_1pct": _r(delta_rupees_1pct, 0),
            "gamma_rupees_1pct": _r(totals.gamma_rupees_1pct, 0),
            "mark_value": _r(totals.value, 0),
            "bias": {
                "delta": _bias(net_delta, "long bias", "short bias"),
                "gamma": _bias(net_gamma, "long γ", "short γ"),
                "vega": _bias(net_vega, "long vol", "short vol"),
                "theta": _bias(net_theta, "collecting decay", "paying decay"),
            },
            "positions": len(position_keys),
            "legs": len(legs),
            "legs_priced": len(priced),
            "breakdown_by_strategy": breakdown,
            "by_underlying": by_underlying,
            "scenarios": {row["key"]: row["pnl"] for row in scenario_rows},
            "scenario_list": scenario_rows,
            "missing_greeks": missing,
            "warnings": (
                [f"IV unknown for {len(assumed_iv)} leg(s) — assumed {self.default_iv:.0%}"]
                if assumed_iv
                else []
            )
            + (
                [f"Greeks unavailable for {len(missing)} leg(s) — excluded from totals"]
                if missing
                else []
            ),
            "units": {
                "net_delta": "share-equivalent units (₹ per 1-pt move)",
                "net_gamma": "Δ change per 1% move of each underlying",
                "net_vega": "₹ per +1 IV point",
                "net_theta": "₹ per day",
            },
            "model": "black_scholes_full_revaluation",
            "compute_ms": round((time.perf_counter() - t0) * 1000.0, 2),
        }


@dataclass
class GreeksSnapshot:
    """Cached snapshot (the SSE stream shares one per TTL)."""

    data: Dict[str, Any] = field(default_factory=dict)
    ts: float = 0.0
