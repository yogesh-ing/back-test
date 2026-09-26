"""Strategy P&L correlation — the "correlation bomb" detector.

PRD §1.2 — five strategies that all go red together are one strategy.

Method
------
Each strategy's equity curve is turned into **₹ P&L changes** (``equity.diff()``)
on a common timeline (outer-joined, forward-filled: a strategy with no new mark
in an interval genuinely made ₹0 in it). Pearson correlation is computed
pairwise over overlapping observations, requiring ``min_observations``.

Using ₹ changes rather than % returns means the portfolio P&L is *exactly*
the sum of the strategy P&Ls, which gives two honest summary numbers:

* ``diversification_ratio`` = Σσᵢ / σ_portfolio  (1.0 = no diversification)
* ``effective_strategies``  = diversification_ratio² — roughly how many
  *independent* strategies the book behaves like.

Sign matters: a strong **positive** correlation means losses cluster (risk);
a strong **negative** one means the pair offsets (a hedge — reported as info,
never as a danger, unlike an ``abs(corr)`` test).
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from backtest.monitoring.config import MonitorConfig
from backtest.monitoring.models import (
    SEVERITY_INFO,
    Alert,
    StrategyBook,
    clean_float,
)


def _curve_series(curve: List[Dict[str, Any]]) -> Optional[pd.Series]:
    if not curve:
        return None
    ts, eq = [], []
    for point in curve:
        stamp = point.get("ts")
        value = point.get("equity")
        if stamp is None or value is None:
            continue
        ts.append(stamp)
        eq.append(float(value))
    if len(ts) < 2:
        return None
    index = pd.to_datetime(pd.Series(ts), errors="coerce", utc=True)
    series = pd.Series(eq, index=index.values)
    series = series[~series.index.isna()]
    # Last write wins for duplicate timestamps (a bar can re-mark).
    series = series[~series.index.duplicated(keep="last")].sort_index()
    return series if len(series) >= 2 else None


class CorrelationMonitor:
    def __init__(self, config: Optional[MonitorConfig] = None) -> None:
        self.config = config or MonitorConfig()

    def pnl_frame(self, strategies: List[StrategyBook], lookback: Optional[int] = None
                  ) -> pd.DataFrame:
        """Aligned ₹ P&L changes, one column per strategy name."""
        return self._aligned(strategies, lookback)[0]

    def _aligned(self, strategies: List[StrategyBook], lookback: Optional[int] = None
                 ) -> Tuple[pd.DataFrame, Optional[str]]:
        cols: Dict[str, pd.Series] = {}
        for book in strategies:
            series = _curve_series(book.equity_curve)
            if series is None:
                continue
            label = book.strategy_name or book.strategy_id
            if label in cols:  # two runners with one name — keep both, distinctly
                label = f"{label} ({book.strategy_id[:6]})"
            cols[label] = series
        if not cols:
            return pd.DataFrame(), None
        # Runners stamp their own wall-clock time on each mark, so two runners
        # on the same tick differ by microseconds and would never align. Snap
        # every curve to a common grid: the coarsest runner's median spacing.
        spacings = [
            s.index.to_series().diff().dropna().median() for s in cols.values() if len(s) > 2
        ]
        spacings = [sp for sp in spacings if pd.notna(sp) and sp.total_seconds() > 0]
        seconds = max(1, int(round(max(sp.total_seconds() for sp in spacings)))) \
            if spacings else 1
        grid = f"{seconds}s"
        snapped = {}
        for label, series in cols.items():
            floored = series.copy()
            floored.index = pd.DatetimeIndex(series.index).floor(grid)
            snapped[label] = floored[~floored.index.duplicated(keep="last")]
        equity = pd.DataFrame(snapped).sort_index().ffill()
        pnl = equity.diff().iloc[1:]
        n = lookback or self.config.correlation.lookback
        return (pnl.tail(n) if n else pnl), grid

    def calculate(self, strategies: List[StrategyBook], lookback: Optional[int] = None
                  ) -> Dict[str, Any]:
        """Correlation report from each strategy's own equity curve."""
        pnl, grid = self._aligned(strategies, lookback)
        return self.analyze(pnl, lookback=lookback, grid=grid)

    def calculate_from_equity(self, equity: pd.DataFrame, lookback: Optional[int] = None
                              ) -> Dict[str, Any]:
        """Correlation report from an already-aligned equity frame (rows =
        same-instant samples, columns = strategies) — no timestamp parsing."""
        pnl = equity.diff().iloc[1:] if len(equity) > 1 else equity.iloc[0:0]
        n = lookback or self.config.correlation.lookback
        return self.analyze(pnl.tail(n) if n else pnl, lookback=lookback, grid="tick")

    def analyze(self, pnl: pd.DataFrame, lookback: Optional[int] = None,
                grid: Optional[str] = None) -> Dict[str, Any]:
        cfg = self.config.correlation
        names = [str(c) for c in pnl.columns]
        report: Dict[str, Any] = {
            "method": "pearson on aligned money P&L changes",
            "lookback": lookback or cfg.lookback,
            "min_observations": cfg.min_observations,
            "strategies": names,
            "observations": int(len(pnl)),
            "grid": grid,
            "matrix": [],
            "pairs": [],
            "diversification_ratio": None,
            "effective_strategies": None,
            "strategies_with_variance": 0,
            "status": "ok",
        }
        if len(names) < 2:
            report["status"] = "need_two_strategies"
            report["alerts"] = []
            return report

        # Flat P&L (no position) has zero variance — correlation undefined.
        stdev = pnl.std(ddof=1)
        corr = pnl.corr(min_periods=cfg.min_observations).to_numpy()
        present = pnl.notna().to_numpy().astype(int)
        overlap = present.T @ present

        report["matrix"] = [[clean_float(v, 3) for v in row] for row in corr]

        pairs = []
        for i, a in enumerate(names):
            for j in range(i + 1, len(names)):
                obs = int(overlap[i, j])
                val = clean_float(corr[i, j], 3)
                if obs < cfg.min_observations:
                    status = "insufficient_data"
                elif val is None:
                    status = "no_variance"
                else:
                    status = "ok"
                pairs.append({"a": a, "b": names[j], "correlation": val,
                              "observations": obs, "status": status})
        report["pairs"] = sorted(
            pairs, key=lambda p: -(p["correlation"] if p["correlation"] is not None else -9)
        )

        # Diversification over the columns with variance and enough data.
        usable = [c for c in pnl.columns if (stdev.get(c) or 0.0) > 0]
        report["strategies_with_variance"] = len(usable)
        clean = pnl[usable].dropna() if usable else pd.DataFrame()
        if len(usable) >= 2 and len(clean) >= cfg.min_observations:
            port_sd = float(clean.sum(axis=1).std(ddof=1))
            sum_sd = float(clean.std(ddof=1).sum())
            if port_sd > 0 and math.isfinite(port_sd):
                dr = sum_sd / port_sd
                report["diversification_ratio"] = clean_float(dr, 3)
                report["effective_strategies"] = clean_float(dr * dr, 2)
        if not any(p["status"] == "ok" for p in pairs):
            report["status"] = "insufficient_data"

        report["alerts"] = [a.to_dict() for a in self.check(report)]
        return report

    def check(self, report: Dict[str, Any]) -> List[Alert]:
        cfg = self.config.correlation
        alerts: List[Alert] = []
        for pair in report.get("pairs", []):
            corr = pair.get("correlation")
            if pair.get("status") != "ok" or corr is None:
                continue
            subject = f"{pair['a']}|{pair['b']}"
            sev = cfg.positive.severity(corr) if corr > 0 else None
            if sev:
                alerts.append(
                    Alert(
                        key=f"correlation:pair:{subject}",
                        category="correlation",
                        severity=sev,
                        title=f"{pair['a']} & {pair['b']} move together ({corr:+.2f})",
                        message=(
                            f"Their P&L is {corr:.2f} correlated over {pair['observations']} "
                            "observations — when one loses, the other usually does too."
                        ),
                        recommendation="Size them as one position, or retire the weaker one.",
                        metric="pnl_correlation",
                        value=corr,
                        threshold=cfg.positive.warn,
                        subject=subject,
                    )
                )
            elif corr <= -abs(cfg.negative_info):
                alerts.append(
                    Alert(
                        key=f"correlation:offset:{subject}",
                        category="correlation",
                        severity=SEVERITY_INFO,
                        title=f"{pair['a']} & {pair['b']} offset each other ({corr:+.2f})",
                        message="Strongly negatively correlated — the pair acts as a hedge; "
                                "combined P&L is muted (and so are combined gains).",
                        metric="pnl_correlation",
                        value=corr,
                        subject=subject,
                    )
                )

        eff = report.get("effective_strategies")
        n = report.get("strategies_with_variance") or 0
        if eff is not None and n >= 3 and eff < max(1.5, n / 2.0):
            alerts.append(
                Alert(
                    key="correlation:diversification:portfolio",
                    category="correlation",
                    severity="warning",
                    title=f"{n} strategies behave like {eff:.1f}",
                    message=(
                        f"Diversification ratio {report['diversification_ratio']:.2f} — the "
                        "book's P&L swings nearly as much as the strategies' summed swings."
                    ),
                    recommendation="Effective diversification is lower than the strategy "
                                   "count suggests.",
                    metric="effective_strategies",
                    value=eff,
                    threshold=max(1.5, n / 2.0),
                    subject="portfolio",
                )
            )
        return alerts
