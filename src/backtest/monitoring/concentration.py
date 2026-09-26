"""Concentration & liquidity-clustering monitor.

PRD §1.2 — "I thought I was diversified across 5 strategies, but they're all
just NIFTY plays."

Exposure measure
----------------
The primary measure is **gross underlying notional** (units × spot for an
option leg, qty × price for equity) — the exchange/SEBI exposure convention.
Option *premium* would badly under-state exposure (a short straddle's premium
is small; the NIFTY it is short gamma to is not). Delta-adjusted notional is
reported alongside as the directional view.

Dimensions
----------
* by underlying        — share of gross notional per underlying
* by correlated group  — e.g. NIFTY + BANKNIFTY are one "Indian index" bet
* by strategy stacking — distinct strategies piling onto one underlying
* by strike cluster    — legs at one underlying/strike/expiry: the exit
  liquidity problem when everything must close at once
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, List, Optional

from backtest.monitoring.config import MonitorConfig
from backtest.monitoring.greeks import money
from backtest.monitoring.models import (
    SEVERITY_WARNING,
    Alert,
    MonitorPosition,
    PortfolioInputs,
    clean_float,
)


class ConcentrationMonitor:
    def __init__(self, config: Optional[MonitorConfig] = None) -> None:
        self.config = config or MonitorConfig()
        self._group_of: Dict[str, str] = {}
        for gid, symbols in (self.config.concentration.groups or {}).items():
            for sym in symbols or []:
                self._group_of[str(sym).upper()] = str(gid)

    def group_for(self, underlying: str) -> str:
        return self._group_of.get(str(underlying).upper(), str(underlying).upper())

    @staticmethod
    def _delta_notional(pos: MonitorPosition, delta_units: Optional[float]) -> float:
        if delta_units is None:
            delta_units = pos.units * pos.direction if not pos.is_option else 0.0
        return delta_units * pos.underlying_price

    def calculate(
        self,
        inputs: PortfolioInputs,
        leg_delta_units: Optional[Dict[str, float]] = None,
    ) -> Dict[str, Any]:
        """Concentration report. ``leg_delta_units`` (position_id → Δ units)
        comes from the Greeks pass so both views agree on delta."""
        deltas = leg_delta_units or {}
        positions = inputs.positions

        total = sum(p.notional for p in positions)
        by_und: Dict[str, Dict[str, Any]] = {}
        for p in positions:
            row = by_und.setdefault(
                p.underlying,
                {
                    "underlying": p.underlying,
                    "group": self.group_for(p.underlying),
                    "notional": 0.0,
                    "delta_notional": 0.0,
                    "premium_value": 0.0,
                    "positions": 0,
                    "strategies": set(),
                    "long_delta_strategies": set(),
                    "short_delta_strategies": set(),
                },
            )
            row["notional"] += p.notional
            dn = self._delta_notional(p, deltas.get(p.position_id))
            row["delta_notional"] += dn
            if p.is_option:
                row["premium_value"] += p.current_price * p.units * p.direction
            row["positions"] += 1
            row["strategies"].add(p.strategy_name)

        # Directional side per (underlying, strategy) — for the stacking view.
        strat_delta: Dict[tuple, float] = defaultdict(float)
        for p in positions:
            strat_delta[(p.underlying, p.strategy_name)] += self._delta_notional(
                p, deltas.get(p.position_id)
            )
        for (und, strat), dn in strat_delta.items():
            if dn > 0:
                by_und[und]["long_delta_strategies"].add(strat)
            elif dn < 0:
                by_und[und]["short_delta_strategies"].add(strat)

        by_group: Dict[str, Dict[str, Any]] = {}
        for row in by_und.values():
            g = by_group.setdefault(
                row["group"],
                {"group": row["group"], "notional": 0.0, "underlyings": []},
            )
            g["notional"] += row["notional"]
            g["underlyings"].append(row["underlying"])

        # Strike clusters (options only).
        clusters: Dict[str, Dict[str, Any]] = {}
        for p in positions:
            if not p.is_option or p.strike is None:
                continue
            expiry = (p.expiry.isoformat() if hasattr(p.expiry, "isoformat")
                      else str(p.expiry)) if p.expiry else "?"
            key = f"{p.underlying}|{p.strike:g}|{expiry}"
            c = clusters.setdefault(
                key,
                {
                    "key": key,
                    "underlying": p.underlying,
                    "strike": p.strike,
                    "expiry": expiry,
                    "positions": 0,
                    "lots": 0.0,
                    "units": 0.0,
                    "notional": 0.0,
                    "strategies": set(),
                    "option_types": set(),
                },
            )
            c["positions"] += 1
            c["lots"] += p.lots
            c["units"] += p.units
            c["notional"] += p.notional
            c["strategies"].add(p.strategy_name)
            if p.option_type:
                c["option_types"].add(p.option_type)

        # -- serialise -------------------------------------------------------
        und_rows = []
        for row in sorted(by_und.values(), key=lambda r: -r["notional"]):
            und_rows.append(
                {
                    "underlying": row["underlying"],
                    "group": row["group"],
                    "notional": clean_float(row["notional"], 2),
                    "pct": clean_float(row["notional"] / total if total else 0.0, 4),
                    "delta_notional": clean_float(row["delta_notional"], 2),
                    "premium_value": clean_float(row["premium_value"], 2),
                    "positions": row["positions"],
                    "strategies": sorted(row["strategies"]),
                    "strategy_count": len(row["strategies"]),
                    "long_delta_strategies": sorted(row["long_delta_strategies"]),
                    "short_delta_strategies": sorted(row["short_delta_strategies"]),
                }
            )
        group_rows = [
            {
                "group": g["group"],
                "notional": clean_float(g["notional"], 2),
                "pct": clean_float(g["notional"] / total if total else 0.0, 4),
                "underlyings": sorted(g["underlyings"]),
            }
            for g in sorted(by_group.values(), key=lambda g: -g["notional"])
        ]
        cluster_rows = [
            {
                **{k: v for k, v in c.items() if k not in ("strategies", "option_types")},
                "strike": clean_float(c["strike"], 2),
                "lots": clean_float(c["lots"], 2),
                "units": clean_float(c["units"], 2),
                "notional": clean_float(c["notional"], 2),
                "strategies": sorted(c["strategies"]),
                "option_types": sorted(c["option_types"]),
            }
            for c in sorted(clusters.values(), key=lambda c: (-c["positions"], c["key"]))
        ]

        # Herfindahl index → effective number of underlyings.
        hhi = sum((r["pct"] or 0.0) ** 2 for r in und_rows)
        report = {
            "as_of": inputs.as_of,
            "mode": inputs.mode,
            # Distinct closable exposures (a 2-leg spread is one) and the
            # strategies holding them — the gates for the % alerts.
            "exposure_count": len({p.structure_id or p.position_id for p in positions}),
            "strategy_count": len({p.strategy_id for p in positions}),
            "total_notional": clean_float(total, 2),
            "capital_base": clean_float(inputs.capital_base, 2),
            "gross_leverage": clean_float(
                total / inputs.capital_base if inputs.capital_base else 0.0, 3
            ),
            "position_count": len(positions),
            "herfindahl": clean_float(hhi, 4),
            "effective_underlyings": clean_float(1.0 / hhi if hhi else 0.0, 2),
            "by_underlying": und_rows,
            "by_group": group_rows,
            "strike_clusters": cluster_rows,
        }
        report["alerts"] = [a.to_dict() for a in self.check(report)]
        return report

    def check(self, report: Dict[str, Any]) -> List[Alert]:
        cfg = self.config.concentration
        alerts: List[Alert] = []
        gated = (
            report["exposure_count"] >= cfg.min_positions
            and report["strategy_count"] >= cfg.min_strategies
        )
        if gated:
            for row in report["by_underlying"]:
                pct = row["pct"] or 0.0
                sev = cfg.underlying_pct.severity(pct)
                if not sev:
                    continue
                spread = (
                    f" across {row['strategy_count']} strategies"
                    if row["strategy_count"] > 1 else ""
                )
                alerts.append(
                    Alert(
                        key=f"concentration:underlying:{row['underlying']}",
                        category="concentration",
                        severity=sev,
                        title=f"{pct * 100:.0f}% of exposure in {row['underlying']}",
                        message=(
                            f"{money(row['notional'], self.config.currency_symbol)} gross "
                            f"notional in {row['underlying']}"
                            f"{spread} — max recommended "
                            f"{cfg.underlying_pct.warn * 100:.0f}%."
                        ),
                        recommendation=(
                            "These are one bet, not several: size new entries against the "
                            "combined exposure, or diversify the underlying."
                        ),
                        metric="underlying_pct",
                        value=pct,
                        threshold=cfg.underlying_pct.warn,
                        subject=row["underlying"],
                        context={"strategies": row["strategies"]},
                    )
                )

            # Group concentration is only news when it spans >1 underlying —
            # otherwise it repeats the underlying alert.
            for g in report["by_group"]:
                if len(g["underlyings"]) < 2:
                    continue
                pct = g["pct"] or 0.0
                sev = cfg.group_pct.severity(pct)
                if sev:
                    alerts.append(
                        Alert(
                            key=f"concentration:group:{g['group']}",
                            category="concentration",
                            severity=sev,
                            title=f"{pct * 100:.0f}% in correlated group {g['group']}",
                            message=(
                                f"{', '.join(g['underlyings'])} move together — treat them as "
                                "one exposure."
                            ),
                            metric="group_pct",
                            value=pct,
                            threshold=cfg.group_pct.warn,
                            subject=g["group"],
                        )
                    )

        for row in report["by_underlying"]:
            if row["strategy_count"] >= cfg.strategies_per_underlying:
                same_side = max(
                    len(row["long_delta_strategies"]), len(row["short_delta_strategies"])
                )
                alerts.append(
                    Alert(
                        key=f"concentration:stacking:{row['underlying']}",
                        category="concentration",
                        severity=SEVERITY_WARNING,
                        title=f"{row['strategy_count']} strategies stacked on "
                              f"{row['underlying']}",
                        message=(
                            f"{', '.join(row['strategies'])} all trade {row['underlying']}"
                            + (f"; {same_side} of them lean the same direction."
                               if same_side >= 2 else ".")
                        ),
                        recommendation="Per-strategy limits do not see this — check the "
                                       "combined delta on the Greeks panel.",
                        metric="strategies_per_underlying",
                        value=float(row["strategy_count"]),
                        threshold=float(cfg.strategies_per_underlying),
                        subject=row["underlying"],
                    )
                )

        for c in report["strike_clusters"]:
            if c["positions"] < cfg.strike_cluster_positions:
                continue
            alerts.append(
                Alert(
                    key=f"concentration:strike:{c['key']}",
                    category="concentration",
                    severity=SEVERITY_WARNING,
                    title=f"{c['positions']} positions at {c['underlying']} {c['strike']:g}",
                    message=(
                        f"{c['lots']:g} lots across {len(c['strategies'])} strategies at one "
                        f"strike (expiry {c['expiry']}) — exiting together competes for the "
                        "same order book."
                    ),
                    recommendation="Stagger exits or spread strikes; avoid market orders "
                                   "into the close.",
                    metric="strike_cluster_positions",
                    value=float(c["positions"]),
                    threshold=float(cfg.strike_cluster_positions),
                    subject=c["key"],
                )
            )
        return alerts
