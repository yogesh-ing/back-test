"""Concentration by underlying and by strike.

Exposure measure: **gross underlying notional** — ``|units| × spot`` for
every leg (option legs count the underlying they control, equity legs their
market value). It is the standard gross-exposure view and, unlike
delta-adjusted notional, does not make a delta-neutral short straddle look
like zero exposure. Delta-adjusted notional is reported alongside.

Percentages are shares of the total gross notional, so they add to 100%.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List

from backtest.intelligence.greeks import ExposureLeg


def _fmt_strike(strike: float) -> str:
    return str(int(strike)) if float(strike).is_integer() else f"{strike:g}"


class ConcentrationMonitor:
    def __init__(
        self,
        max_pct: float = 0.60,
        min_positions: int = 2,
        strike_cluster_min: int = 3,
    ) -> None:
        self.max_pct = float(max_pct)
        self.min_positions = int(min_positions)
        self.strike_cluster_min = int(strike_cluster_min)

    def calculate(self, legs: Iterable[ExposureLeg], greeks: Dict[str, Any] = None) -> Dict:
        legs = list(legs)
        by_u: Dict[str, Dict[str, Any]] = {}
        by_strike: Dict[str, Dict[str, Any]] = {}
        positions_total: set = set()
        total = 0.0
        for leg in legs:
            notional = leg.notional()
            total += notional
            positions_total.add((leg.source_id, leg.position_key))
            u = by_u.setdefault(
                leg.underlying,
                {
                    "underlying": leg.underlying,
                    "exposure": 0.0,
                    "_positions": set(),
                    "_sources": set(),
                    "legs": 0,
                },
            )
            u["exposure"] += notional
            u["_positions"].add((leg.source_id, leg.position_key))
            u["_sources"].add(leg.source_label)
            u["legs"] += 1
            if leg.kind == "option" and leg.strike:
                key = f"{leg.underlying}_{_fmt_strike(float(leg.strike))}"
                s = by_strike.setdefault(
                    key,
                    {
                        "key": key,
                        "underlying": leg.underlying,
                        "strike": float(leg.strike),
                        "exposure": 0.0,
                        "_positions": set(),
                        "_sources": set(),
                        "legs": 0,
                        "option_types": set(),
                    },
                )
                s["exposure"] += notional
                s["_positions"].add((leg.source_id, leg.position_key))
                s["_sources"].add(leg.source_label)
                s["legs"] += 1
                s["option_types"].add(leg.option_type or "")

        delta_by_u = (greeks or {}).get("by_underlying", {}) if greeks else {}
        underlying_rows: Dict[str, Dict[str, Any]] = {}
        for name, u in sorted(by_u.items(), key=lambda kv: -kv[1]["exposure"]):
            pct = (u["exposure"] / total) if total else 0.0
            d = delta_by_u.get(name, {})
            spot = d.get("spot") or 0.0
            underlying_rows[name] = {
                "exposure": round(u["exposure"], 2),
                "pct": round(pct * 100.0, 2),
                "share": round(pct, 4),
                "positions": len(u["_positions"]),
                "legs": u["legs"],
                "sources": sorted(u["_sources"]),
                "delta_notional": round(abs(float(d.get("delta", 0.0))) * float(spot or 0), 2),
                "high": pct > self.max_pct,
            }
        strike_rows: Dict[str, Dict[str, Any]] = {}
        for key, s in sorted(by_strike.items(), key=lambda kv: -len(kv[1]["_positions"])):
            strike_rows[key] = {
                "underlying": s["underlying"],
                "strike": s["strike"],
                "positions": len(s["_positions"]),
                "legs": s["legs"],
                "exposure": round(s["exposure"], 2),
                "sources": sorted(s["_sources"]),
                "option_types": sorted(t for t in s["option_types"] if t),
                "clustered": len(s["_positions"]) >= self.strike_cluster_min,
            }

        alerts: List[Dict[str, Any]] = []
        if len(positions_total) >= self.min_positions:
            for name, row in underlying_rows.items():
                if row["share"] > self.max_pct:
                    alerts.append(
                        {
                            "type": "high_concentration",
                            "underlying": name,
                            "pct": row["pct"],
                            "threshold_pct": round(self.max_pct * 100, 2),
                        }
                    )
        for key, row in strike_rows.items():
            if row["clustered"]:
                alerts.append(
                    {
                        "type": "strike_clustering",
                        "key": key,
                        "underlying": row["underlying"],
                        "strike": row["strike"],
                        "positions": row["positions"],
                        "threshold": self.strike_cluster_min,
                    }
                )
        return {
            "total_exposure": round(total, 2),
            "positions": len(positions_total),
            "by_underlying": underlying_rows,
            "by_strike": strike_rows,
            "alerts": alerts,
            "thresholds": {
                "max_pct": round(self.max_pct * 100, 2),
                "strike_cluster_min": self.strike_cluster_min,
                "min_positions": self.min_positions,
            },
            "measure": "gross underlying notional (|units| × spot)",
        }
