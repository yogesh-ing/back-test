"""Portfolio Greeks aggregation, scenario revaluation and Greek-limit alerts.

PRD §1.1 — "I thought I was delta-neutral, but I'm massively long."

Units (all position-level, signed: long adds, short subtracts)
-------------------------------------------------------------
``delta_units``      Δ × units — underlying-equivalent units. Only meaningful
                     *within* one underlying, so it is reported per underlying
                     and per strategy, never as a portfolio total.
``delta_1pct``       ₹ P&L for a +1% move in the underlying (Δ·S·1%). Summable.
``gamma_1pct``       change in ``delta_units`` for a +1% move (Γ·S·1%).
``gamma_pnl_1pct``   ₹ convexity P&L for a ±1% move (½·Γ·(S·1%)²) — negative
                     when short gamma. Summable.
``vega``             ₹ P&L per +1 vol-point IV change. Summable.
``theta_day``        ₹ P&L per calendar day of decay. Summable.

Scenarios are **full Black-Scholes revaluation** of every leg at the shocked
spot/IV/time, not a Taylor expansion — a delta-gamma estimate under-states a
short-gamma book's loss exactly when it matters (large moves). The
delta-gamma number is reported beside it so the gap is visible.

Spot shocks are applied to every underlying at once (β = 1). For a book of
Indian index options + large caps that is the conservative reading of a
"market" move; per-underlying betas are a later refinement.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional

from backtest.monitoring.config import MonitorConfig
from backtest.monitoring.models import (
    SEVERITY_CRITICAL,
    SEVERITY_INFO,
    SEVERITY_WARNING,
    Alert,
    MonitorPosition,
    PortfolioInputs,
    clean_float,
)
from backtest.options.greeks import BlackScholes

logger = logging.getLogger("backtest.monitoring.greeks")

MIN_IV = 0.01  # IV floor after a negative shock (1 vol point)


def money(value: float, symbol: str = "₹") -> str:
    """Signed amount with the sign before the symbol: −₹6,594."""
    return f"{'−' if value < 0 else ''}{symbol}{abs(value):,.0f}"


@dataclass(frozen=True)
class LegGreeks:
    """Per-unit Greeks plus signed, position-level money metrics for one leg."""

    position: MonitorPosition
    model_price: float  # per unit
    delta: float  # per unit
    gamma: float
    vega: float  # per unit, per vol point
    theta_day: float  # per unit, per calendar day
    delta_units: float
    delta_1pct: float
    gamma_1pct: float
    gamma_pnl_1pct: float
    position_vega: float
    position_theta_day: float
    model_value: float  # signed mark value at model price

    def to_dict(self) -> Dict[str, Any]:
        p = self.position
        return {
            "position_id": p.position_id,
            "strategy_id": p.strategy_id,
            "strategy_name": p.strategy_name,
            "symbol": p.symbol,
            "underlying": p.underlying,
            "instrument_type": p.instrument_type,
            "side": p.side,
            "units": clean_float(p.units, 2),
            "lots": clean_float(p.lots, 2),
            "option_type": p.option_type,
            "strike": clean_float(p.strike, 2),
            "expiry": (p.expiry.isoformat() if hasattr(p.expiry, "isoformat")
                       else str(p.expiry)) if p.expiry else None,
            "dte_days": clean_float(p.dte_days, 2),
            "iv": clean_float(p.iv, 4),
            "iv_source": p.iv_source,
            "underlying_price": clean_float(p.underlying_price, 2),
            "current_price": clean_float(p.current_price, 2),
            "model_price": clean_float(self.model_price, 2),
            "structure_id": p.structure_id,
            "structure_type": p.structure_type,
            "delta": clean_float(self.delta, 4),
            "gamma": clean_float(self.gamma, 6),
            "vega": clean_float(self.vega, 4),
            "theta_day": clean_float(self.theta_day, 4),
            "delta_units": clean_float(self.delta_units, 2),
            "delta_1pct": clean_float(self.delta_1pct, 2),
            "gamma_1pct": clean_float(self.gamma_1pct, 2),
            "gamma_pnl_1pct": clean_float(self.gamma_pnl_1pct, 2),
            "position_vega": clean_float(self.position_vega, 2),
            "position_theta_day": clean_float(self.position_theta_day, 2),
        }


_SUM_KEYS = (
    "delta_1pct",
    "gamma_1pct",
    "gamma_pnl_1pct",
    "vega",
    "theta_day",
)


def _empty_bucket() -> Dict[str, float]:
    return {k: 0.0 for k in _SUM_KEYS}


class PortfolioGreeksAggregator:
    """Aggregate Greeks across every open leg, revalue scenarios, check limits."""

    def __init__(self, config: Optional[MonitorConfig] = None) -> None:
        self.config = config or MonitorConfig()
        self._bs = BlackScholes(
            risk_free_rate=self.config.risk_free_rate,
            volatility=self.config.default_iv,
        )

    def _m(self, value: float) -> str:
        return money(value, self.config.currency_symbol)

    # ------------------------------------------------------------------ #
    # Per-leg
    # ------------------------------------------------------------------ #

    def _years(self, pos: MonitorPosition, extra_days: float = 0.0) -> float:
        dte = pos.dte_days if pos.dte_days is not None else 0.0
        return max(dte - extra_days, 0.0) / 365.0

    def _price(
        self,
        pos: MonitorPosition,
        spot: float,
        iv: Optional[float] = None,
        extra_days: float = 0.0,
    ) -> float:
        """Model price per unit (share price for equity)."""
        if not pos.is_option:
            return spot
        vol = max(iv if iv is not None else (pos.iv or self.config.default_iv), MIN_IV)
        return self._bs.price(
            spot=spot,
            strike=float(pos.strike or 0.0),
            expiry_years=self._years(pos, extra_days),
            option_type=pos.option_type or "CE",
            volatility=vol,
        )

    def leg_greeks(self, pos: MonitorPosition) -> LegGreeks:
        spot = float(pos.underlying_price)
        units = float(pos.units)
        sign = pos.direction
        if pos.is_option:
            g = self._bs.greeks(
                spot=spot,
                strike=float(pos.strike or 0.0),
                expiry_years=self._years(pos),
                option_type=pos.option_type or "CE",
                volatility=max(pos.iv or self.config.default_iv, MIN_IV),
            )
            price, delta, gamma, vega = g.price, g.delta, g.gamma, g.vega
            theta_day = g.theta / 365.0
        else:
            price, delta, gamma, vega, theta_day = spot, 1.0, 0.0, 0.0, 0.0

        delta_units = delta * units * sign
        gamma_units = gamma * units * sign
        move = spot * 0.01
        return LegGreeks(
            position=pos,
            model_price=price,
            delta=delta,
            gamma=gamma,
            vega=vega,
            theta_day=theta_day,
            delta_units=delta_units,
            delta_1pct=delta_units * move,
            gamma_1pct=gamma_units * move,
            gamma_pnl_1pct=0.5 * gamma_units * move * move,
            position_vega=vega * units * sign,
            position_theta_day=theta_day * units * sign,
            model_value=price * units * sign,
        )

    # ------------------------------------------------------------------ #
    # Aggregation
    # ------------------------------------------------------------------ #

    def calculate(self, inputs: PortfolioInputs) -> Dict[str, Any]:
        """Full Greeks report for a portfolio snapshot (JSON-safe dict)."""
        legs = [self.leg_greeks(p) for p in inputs.positions]
        base = inputs.capital_base

        totals = _empty_bucket()
        by_underlying: Dict[str, Dict[str, Any]] = {}
        by_strategy: Dict[str, Dict[str, Any]] = {}
        net_premium = 0.0

        for leg in legs:
            p = leg.position
            vals = {
                "delta_1pct": leg.delta_1pct,
                "gamma_1pct": leg.gamma_1pct,
                "gamma_pnl_1pct": leg.gamma_pnl_1pct,
                "vega": leg.position_vega,
                "theta_day": leg.position_theta_day,
            }
            for k, v in vals.items():
                totals[k] += v
            if p.is_option:
                # Credit positive: a short leg received premium.
                net_premium += -p.direction * p.entry_price * p.units

            u = by_underlying.setdefault(
                p.underlying,
                {
                    **_empty_bucket(),
                    "underlying": p.underlying,
                    "spot": p.underlying_price,
                    "delta_units": 0.0,
                    "positions": 0,
                    "lot_size": p.lot_size,
                },
            )
            s = by_strategy.setdefault(
                p.strategy_id,
                {
                    **_empty_bucket(),
                    "strategy_id": p.strategy_id,
                    "strategy_name": p.strategy_name,
                    "strategy_kind": p.strategy_kind,
                    "mode": p.mode,
                    "positions": 0,
                    "long_legs": 0,
                    "short_legs": 0,
                    "net_premium": 0.0,
                    "underlyings": set(),
                    "delta_units": defaultdict(float),
                },
            )
            for bucket in (u, s):
                for k, v in vals.items():
                    bucket[k] += v
                bucket["positions"] += 1
            u["delta_units"] += leg.delta_units
            s["delta_units"][p.underlying] += leg.delta_units
            s["underlyings"].add(p.underlying)
            s["long_legs" if p.direction > 0 else "short_legs"] += 1
            if p.is_option:
                s["net_premium"] += -p.direction * p.entry_price * p.units

        margin_used = sum(b.margin_used for b in inputs.strategies)
        for book in inputs.strategies:
            if book.strategy_id in by_strategy:
                by_strategy[book.strategy_id]["margin_used"] = book.margin_used

        # 2%-move convexity: ½Γ(S·2%)² = 4 × the 1% figure.
        gamma_pnl_2pct = totals["gamma_pnl_1pct"] * 4.0

        report: Dict[str, Any] = {
            "as_of": inputs.as_of,
            "mode": inputs.mode,
            "capital_base": clean_float(base, 2),
            "position_count": len(legs),
            "option_legs": sum(1 for leg in legs if leg.position.is_option),
            "totals": {
                "delta_1pct": clean_float(totals["delta_1pct"], 2),
                "gamma_1pct_pnl": clean_float(totals["gamma_pnl_1pct"], 2),
                "gamma_2pct_pnl": clean_float(gamma_pnl_2pct, 2),
                "vega": clean_float(totals["vega"], 2),
                "theta_day": clean_float(totals["theta_day"], 2),
                "net_premium": clean_float(net_premium, 2),
                "margin_used": clean_float(margin_used, 2),
                "margin_capital": clean_float(inputs.total_capital, 2),
                "margin_pct": clean_float(
                    margin_used / inputs.total_capital if inputs.total_capital else 0.0, 4
                ),
            },
            "ratios": {
                "delta_1pct": clean_float(abs(totals["delta_1pct"]) / base if base else 0.0, 5),
                "gamma_2pct": clean_float(
                    -gamma_pnl_2pct / base if base and gamma_pnl_2pct < 0 else 0.0, 5
                ),
                "vega_1pt": clean_float(abs(totals["vega"]) / base if base else 0.0, 5),
                "theta_day": clean_float(
                    -totals["theta_day"] / base if base and totals["theta_day"] < 0 else 0.0, 5
                ),
            },
            "by_underlying": [
                self._round_bucket(u) for u in sorted(
                    by_underlying.values(), key=lambda b: -abs(b["delta_1pct"])
                )
            ],
            "by_strategy": [
                self._round_strategy(s) for s in sorted(
                    by_strategy.values(), key=lambda b: b["strategy_name"] or ""
                )
            ],
            "legs": [leg.to_dict() for leg in legs],
            "iv_sources": self._iv_source_counts(legs),
        }
        report["scenarios"] = self.scenarios(legs, base)
        report["alerts"] = [
            a.to_dict()
            for a in self.check_limits(report, legs, inputs)
        ]
        return report

    @staticmethod
    def _iv_source_counts(legs: Iterable[LegGreeks]) -> Dict[str, int]:
        counts: Dict[str, int] = defaultdict(int)
        for leg in legs:
            if leg.position.is_option:
                counts[leg.position.iv_source or "unknown"] += 1
        return dict(counts)

    @staticmethod
    def _round_bucket(b: Dict[str, Any]) -> Dict[str, Any]:
        out = dict(b)
        for k in _SUM_KEYS + ("delta_units", "spot"):
            out[k] = clean_float(out.get(k), 2)
        return out

    @staticmethod
    def _round_strategy(s: Dict[str, Any]) -> Dict[str, Any]:
        out = dict(s)
        for k in _SUM_KEYS + ("net_premium", "margin_used"):
            out[k] = clean_float(out.get(k, 0.0), 2)
        out["underlyings"] = sorted(s["underlyings"])
        out["delta_units"] = {
            u: clean_float(v, 2) for u, v in sorted(s["delta_units"].items())
        }
        return out

    # ------------------------------------------------------------------ #
    # Scenarios
    # ------------------------------------------------------------------ #

    def _revalue(
        self,
        legs: List[LegGreeks],
        spot_pct: float = 0.0,
        iv_pts: float = 0.0,
        days: float = 0.0,
    ) -> float:
        """Full-revaluation P&L of the whole book under one shock."""
        pnl = 0.0
        for leg in legs:
            p = leg.position
            spot = p.underlying_price * (1.0 + spot_pct / 100.0)
            iv = None
            if p.is_option:
                iv = max((p.iv or self.config.default_iv) + iv_pts / 100.0, MIN_IV)
            new_price = self._price(p, spot, iv=iv, extra_days=days)
            pnl += (new_price - leg.model_price) * p.units * p.direction
        return pnl

    @staticmethod
    def _delta_gamma(legs: List[LegGreeks], spot_pct: float) -> float:
        total = 0.0
        for leg in legs:
            ds = leg.position.underlying_price * spot_pct / 100.0
            total += leg.delta_units * ds + 0.5 * (leg.gamma * leg.position.units
                                                   * leg.position.direction) * ds * ds
        return total

    def scenarios(self, legs: List[LegGreeks], base: float) -> Dict[str, Any]:
        cfg = self.config.scenarios

        def _pct(pnl: float) -> Optional[float]:
            return clean_float(pnl / base, 5) if base else None

        spot_rows = []
        for move in cfg.spot_moves_pct:
            full = self._revalue(legs, spot_pct=move)
            spot_rows.append(
                {
                    "label": f"Spot {move:+.1f}%",
                    "spot_pct": move,
                    "pnl": clean_float(full, 2),
                    "pnl_pct": _pct(full),
                    "delta_gamma_pnl": clean_float(self._delta_gamma(legs, move), 2),
                }
            )
        iv_rows = []
        for shift in cfg.iv_shifts_pts:
            full = self._revalue(legs, iv_pts=shift)
            iv_rows.append(
                {
                    "label": f"IV {shift:+.0f} pts",
                    "iv_pts": shift,
                    "pnl": clean_float(full, 2),
                    "pnl_pct": _pct(full),
                }
            )
        decay = self._revalue(legs, days=1.0)
        stress_rows = []
        for sc in cfg.stress:
            try:
                full = self._revalue(
                    legs,
                    spot_pct=float(sc.get("spot_pct", 0.0)),
                    iv_pts=float(sc.get("iv_pts", 0.0)),
                    days=float(sc.get("days", 0.0)),
                )
            except (TypeError, ValueError):
                logger.warning("[greeks] bad stress scenario %r — skipped", sc)
                continue
            stress_rows.append(
                {
                    "label": str(sc.get("name") or "stress"),
                    "spot_pct": sc.get("spot_pct", 0.0),
                    "iv_pts": sc.get("iv_pts", 0.0),
                    "days": sc.get("days", 0.0),
                    "pnl": clean_float(full, 2),
                    "pnl_pct": _pct(full),
                }
            )
        every = spot_rows + iv_rows + stress_rows
        worst = min(every, key=lambda r: r["pnl"] or 0.0) if every else None
        return {
            "method": "full_revaluation",
            "beta_assumption": "all underlyings move together (beta = 1)",
            "spot": spot_rows,
            "iv": iv_rows,
            "time_decay_1d": {"pnl": clean_float(decay, 2), "pnl_pct": _pct(decay)},
            "stress": stress_rows,
            "worst": worst,
        }

    # ------------------------------------------------------------------ #
    # Limits → alerts
    # ------------------------------------------------------------------ #

    @staticmethod
    def _structure_contributions(
        legs: List[LegGreeks], attr: str
    ) -> List[Dict[str, Any]]:
        """Group a metric by structure (or equity position) — the unit an
        operator can actually close — largest magnitude first."""
        groups: Dict[str, Dict[str, Any]] = {}
        for leg in legs:
            p = leg.position
            key = p.structure_id or p.position_id
            g = groups.setdefault(
                key,
                {
                    "key": key,
                    "strategy_name": p.strategy_name,
                    "label": (
                        f"{p.underlying} {p.structure_type}"
                        if p.structure_type
                        else f"{p.symbol} {p.side.lower()}"
                    ),
                    "value": 0.0,
                },
            )
            g["value"] += getattr(leg, attr)
        return sorted(groups.values(), key=lambda g: -abs(g["value"]))

    def check_limits(
        self,
        report: Dict[str, Any],
        legs: List[LegGreeks],
        inputs: PortfolioInputs,
    ) -> List[Alert]:
        limits = self.config.greeks
        ratios = report["ratios"]
        totals = report["totals"]
        base = inputs.capital_base
        alerts: List[Alert] = []
        if not legs or base <= 0:
            return alerts

        # -- Delta ---------------------------------------------------------
        sev = limits.delta_1pct.severity(ratios["delta_1pct"] or 0.0)
        if sev:
            d1 = totals["delta_1pct"] or 0.0
            bias = "LONG" if d1 > 0 else "SHORT"
            hedges = []
            for u in report["by_underlying"]:
                units = u.get("delta_units") or 0.0
                verb = "Sell" if units > 0 else "Buy"
                lot = u.get("lot_size") or 1
                if lot > 1:
                    lots = units / lot
                    if abs(lots) < 0.5:  # below one tradable lot — not a hedge
                        continue
                    hedges.append(f"{verb} ~{abs(lots):.1f} {u['underlying']} futures lots")
                elif abs(units) >= 1:
                    hedges.append(f"{verb} ~{abs(units):,.0f} {u['underlying']} shares")
            alerts.append(
                Alert(
                    key="greeks:delta:portfolio",
                    category="greeks",
                    severity=sev,
                    title=f"Directional {bias} bias",
                    message=(
                        f"Net delta ≈ {self._m(d1)} per 1% move "
                        f"({(ratios['delta_1pct'] or 0) * 100:.2f}% of equity). "
                        f"A 1% move against you costs ≈ {self._m(abs(d1))}."
                    ),
                    recommendation=(
                        "Neutralise with a delta hedge: " + "; ".join(hedges)
                        if hedges
                        else "Reduce the largest directional position."
                    ),
                    metric="delta_1pct_ratio",
                    value=ratios["delta_1pct"],
                    threshold=getattr(limits.delta_1pct, "critical" if sev == "critical"
                                      else "warn"),
                    subject="portfolio",
                )
            )

        # -- Gamma (short only) -------------------------------------------
        sev = limits.gamma_2pct.severity(ratios["gamma_2pct"] or 0.0)
        if sev:
            g2 = totals["gamma_2pct_pnl"] or 0.0
            top = [c for c in self._structure_contributions(legs, "gamma_pnl_1pct")
                   if c["value"] < 0]
            rec = None
            if top:
                biggest = top[0]
                share = biggest["value"] / (totals["gamma_1pct_pnl"] or -1.0)
                rec = (
                    f"Close or hedge {biggest['strategy_name']} · {biggest['label']} — "
                    f"removes ≈ {min(share, 1.0) * 100:.0f}% of the short gamma "
                    f"(≈ {self._m(abs(biggest['value']) * 4)} of the 2%-move convexity loss)."
                )
            alerts.append(
                Alert(
                    key="greeks:gamma:portfolio",
                    category="greeks",
                    severity=sev,
                    title="Short gamma — losses accelerate on large moves",
                    message=(
                        f"Convexity alone loses ≈ {self._m(abs(g2))} on a ±2% move "
                        f"({(ratios['gamma_2pct'] or 0) * 100:.2f}% of equity), on top of delta."
                    ),
                    recommendation=rec,
                    metric="gamma_2pct_ratio",
                    value=ratios["gamma_2pct"],
                    threshold=getattr(limits.gamma_2pct, "critical" if sev == "critical"
                                      else "warn"),
                    subject="portfolio",
                    context={"top_contributors": top[:3]},
                )
            )

        # -- Vega ----------------------------------------------------------
        sev = limits.vega_1pt.severity(ratios["vega_1pt"] or 0.0)
        if sev:
            vega = totals["vega"] or 0.0
            side = "long" if vega > 0 else "short"
            danger = "an IV crush" if vega > 0 else "an IV spike (VIX up)"
            top = self._structure_contributions(legs, "position_vega")
            rec = (
                f"Largest contributor: {top[0]['strategy_name']} · {top[0]['label']} "
                f"({self._m(top[0]['value'])}/vol pt)."
                if top else None
            )
            alerts.append(
                Alert(
                    key="greeks:vega:portfolio",
                    category="greeks",
                    severity=sev,
                    title=f"Large {side} vega",
                    message=(
                        f"{self._m(vega)} per vol point — {danger} of 5 pts moves P&L by "
                        f"≈ {self._m(abs(vega) * 5)}."
                    ),
                    recommendation=rec,
                    metric="vega_1pt_ratio",
                    value=ratios["vega_1pt"],
                    threshold=getattr(limits.vega_1pt, "critical" if sev == "critical"
                                      else "warn"),
                    subject="portfolio",
                )
            )

        # -- Theta (paying decay only) ------------------------------------
        sev = limits.theta_day.severity(ratios["theta_day"] or 0.0)
        theta = totals["theta_day"] or 0.0
        if sev:
            alerts.append(
                Alert(
                    key="greeks:theta:portfolio",
                    category="greeks",
                    severity=sev,
                    title="Heavy time-decay bleed",
                    message=(
                        f"The book pays ≈ {self._m(abs(theta))}/day in decay "
                        f"({(ratios['theta_day'] or 0) * 100:.2f}% of equity per day)."
                    ),
                    recommendation="Long premium needs the move soon — review time stops.",
                    metric="theta_day_ratio",
                    value=ratios["theta_day"],
                    threshold=getattr(limits.theta_day, "critical" if sev == "critical"
                                      else "warn"),
                    subject="portfolio",
                )
            )
        elif (
            theta > 0
            and (totals["gamma_1pct_pnl"] or 0.0) < 0
            and theta / base >= limits.theta_day.warn / 3.0  # material income only
        ):
            alerts.append(
                Alert(
                    key="greeks:theta_income:portfolio",
                    category="greeks",
                    severity=SEVERITY_INFO,
                    title="Premium-seller profile",
                    message=(
                        f"Collecting ≈ {self._m(theta)}/day of theta — paid for by short "
                        f"gamma; the income is compensation for gap risk."
                    ),
                    subject="portfolio",
                )
            )

        # -- Worst modelled scenario --------------------------------------
        worst = (report.get("scenarios") or {}).get("worst")
        if worst and (worst.get("pnl") or 0.0) < 0:
            loss = -(worst["pnl"] or 0.0)
            ratio = loss / base
            sev = self.config.scenarios.worst_loss.severity(ratio)
            headroom = None
            if inputs.daily_loss_limit:
                headroom = inputs.daily_loss_limit + min(inputs.daily_pnl, 0.0)
                if loss >= headroom > 0:
                    sev = SEVERITY_CRITICAL
            if sev:
                msg = (
                    f"Scenario '{worst['label']}' loses ≈ {self._m(loss)} "
                    f"({ratio * 100:.2f}% of equity)."
                )
                if headroom is not None and loss >= headroom > 0:
                    msg += (
                        f" That exceeds the {self._m(headroom)} left before the daily-loss "
                        "breaker trips — the breaker would fire mid-move, at the worst fill."
                    )
                alerts.append(
                    Alert(
                        key="scenario:worst:portfolio",
                        category="scenario",
                        severity=sev,
                        title="Stress loss beyond tolerance",
                        message=msg,
                        recommendation=(
                            "Cut the exposure driving this scenario before the move, "
                            "not after — see the Greek alerts for the largest contributor."
                        ),
                        metric="worst_scenario_loss_ratio",
                        value=ratio,
                        threshold=getattr(
                            self.config.scenarios.worst_loss,
                            "critical" if sev == SEVERITY_CRITICAL else "warn",
                        ),
                        subject="portfolio",
                        context={"scenario": worst, "daily_loss_headroom": headroom},
                    )
                )

        # -- Data-quality note --------------------------------------------
        defaulted = sum(
            1 for leg in legs if leg.position.is_option and leg.position.iv_source == "default"
        )
        if defaulted:
            alerts.append(
                Alert(
                    key="greeks:iv_default:portfolio",
                    category="greeks",
                    severity=SEVERITY_WARNING if defaulted > 1 else SEVERITY_INFO,
                    title="Greeks on assumed volatility",
                    message=(
                        f"{defaulted} leg(s) have no market or contract IV — their Greeks "
                        f"use the {self.config.default_iv * 100:.0f}% default and are "
                        "approximate."
                    ),
                    subject="portfolio",
                )
            )
        return alerts
