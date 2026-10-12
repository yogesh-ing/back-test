"""Analytics service for live & paper trading performance evaluation.

Provides quantitative performance analytics, risk ratios, equity & drawdown curves,
trade distribution, monthly breakdowns, and edge degradation detection for
individual running strategies as well as portfolio-level aggregation.

This module moved here from ``backtest/api/analytics_service.py`` (PRD-003) so
the analytics layer has ONE home: the service layer no longer lives under
``backtest.api``, whose package ``__init__`` eagerly imports every blueprint —
which made ``backtest.analytics.cross_broker`` (an HTTP-layer consumer) unable
to import the service it builds on without closing an import cycle.

Conventions (docs/archive/ANALYTICS-TAB-GAPS.md fixes, 2026-09-28):

* **Clock** — naive timestamps are IST market time (bar/exit stamps come off
  the exchange clock); aware timestamps are trusted as-is. All period
  filtering compares in absolute time, so the UTC↔IST 5h30 skew can no
  longer shift "today" boundaries (fix #2).
* **Breakeven trades are neutral** — ``pnl == 0`` neither extends nor is a
  win/loss streak, in BOTH the max counters and the current streak (fix #8).
* **No-loss books** — ``profit_factor`` is ``None`` (frontend renders ∞),
  never a 99.99 magic number; ``sortino_ratio`` is ``None`` when there is no
  downside deviation to divide by (frontend renders "n/a") (fixes #5/#8).
* **Persisted history** — when the portfolio manager has a live trade
  persister, per-runner analytics merge the DB `trades` rows with the
  in-memory tail (memory wins on natural-key collisions) and report
  provenance in a ``history`` block, so the 200-trade memory cap stops
  silently truncating long histories (fix #3).
"""

from __future__ import annotations

import math
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from backtest.forward.portfolio_manager import get_portfolio_manager
from backtest.logging_config import get_logger

log = get_logger(__name__)

#: Indian market clock — naive bar/exit timestamps are stamped in this zone.
IST = timezone(timedelta(hours=5, minutes=30))

#: Health ratings below this sample size are statistically meaningless.
MIN_HEALTH_SAMPLE = 30

#: Option trading symbols end in the strike + CE/PE (e.g. NIFTY26OCT24800CE).
_OPTION_SYMBOL_RE = re.compile(r"\d+(CE|PE)$", re.IGNORECASE)

_PERF_CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "performance.yaml"
_rf_cache: Optional[float] = None


def strategy_description(strategy_name: Any) -> Optional[Dict[str, Any]]:
    """Registry metadata for a strategy — ``None`` when unregistered.

    Built-ins and plugins both carry ``description`` (the conformance battery
    enforces it), so this is real content for anything registered. Older
    strategies may have a stub sentence — the UI renders a backfill
    placeholder when the text is empty; keep the placeholder client-side so
    the backfill PR only touches strategy files, not UI code.
    """
    name = str(strategy_name or "").strip()
    if not name:
        return None
    try:
        from backtest.strategy.registry import get_strategy

        cls = get_strategy(name)
    except Exception:  # noqa: BLE001 — unknown strategy is a display concern
        return None
    if cls is None:
        return None
    description = str(getattr(cls, "description", "") or "").strip()
    if not description:
        return None
    return {
        "description": description,
        "version": str(getattr(cls, "version", "") or ""),
        "author": str(getattr(cls, "author", "") or ""),
    }


def _risk_free_rate() -> float:
    """Annual risk-free rate: env override → config/performance.yaml → 6%.

    Fix #8: the rate used to be hardcoded at 6%; now a config change is a
    config change. ``ANALYTICS_RISK_FREE_RATE`` wins (ops knob), then the
    active profile in ``config/performance.yaml`` (the same file the
    simulator's PerformanceCalculator reads), then the legacy 0.06 default.
    """
    global _rf_cache
    env = os.getenv("ANALYTICS_RISK_FREE_RATE")
    if env:
        try:
            return float(env)
        except ValueError:
            log.warning("ANALYTICS_RISK_FREE_RATE=%r is not a number — ignoring", env)
    if _rf_cache is not None:
        return _rf_cache
    rate = 0.06
    try:
        import yaml

        raw = yaml.safe_load(_PERF_CONFIG_PATH.read_text()) or {}
        profile = str(raw.get("active_profile", "default"))
        block = raw.get(profile) or (raw.get("profiles") or {}).get(profile) or {}
        default_block = raw.get("default") or {}
        rate = float(block.get("risk_free_rate", default_block.get("risk_free_rate", rate)))
    except Exception:  # noqa: BLE001 — config trouble must never break analytics
        log.debug("performance.yaml risk_free_rate unavailable — using %.2f", rate)
    _rf_cache = rate
    return rate


def _parse_ts(ts_val: Any) -> Optional[datetime]:
    """Parse a trade/equity timestamp; NAIVE values are IST market time.

    Fix #2: naive stamps used to be assumed UTC, shifting every period
    boundary by 5h30 against the exchange clock the runners actually stamp.
    """
    if not ts_val:
        return None
    if isinstance(ts_val, datetime):
        return ts_val if ts_val.tzinfo else ts_val.replace(tzinfo=IST)
    try:
        dt = pd.to_datetime(ts_val)
        if dt.tzinfo is None:
            dt = dt.tz_localize(IST)
        return dt.to_pydatetime()
    except Exception:
        return None


def _filter_by_period(trades: List[Dict[str, Any]], period: str) -> List[Dict[str, Any]]:
    if not period or period == "all_time":
        return trades
    now = datetime.now(timezone.utc)
    days_map = {"7d": 7, "30d": 30, "90d": 90, "1y": 365}
    days = days_map.get(period, 90)
    cutoff = now - pd.Timedelta(days=days)

    filtered = []
    for t in trades:
        exit_ts = _parse_ts(t.get("exit_ts") or t.get("timestamp") or t.get("entry_ts"))
        if exit_ts is None or exit_ts >= cutoff:
            filtered.append(t)
    return filtered


def _calculate_streaks(pnls: List[float]) -> Dict[str, Any]:
    if not pnls:
        return {
            "max_win_streak": 0,
            "max_loss_streak": 0,
            "current_streak": 0,
            "current_streak_is_win": False,
        }

    max_win = 0
    max_loss = 0
    cur_win = 0
    cur_loss = 0

    for p in pnls:
        if p > 0:
            cur_win += 1
            cur_loss = 0
            if cur_win > max_win:
                max_win = cur_win
        elif p < 0:
            cur_loss += 1
            cur_win = 0
            if cur_loss > max_loss:
                max_loss = cur_loss
        else:
            # Breakeven resets win/loss streak count
            cur_win = 0
            cur_loss = 0

    # Current streak — SAME convention as the max counters (fix #8):
    # breakeven (pnl == 0) is neutral, it neither extends a streak nor
    # belongs to one. A breakeven LAST trade means "no current streak".
    current_is_win = False
    current_len = 0
    if pnls[-1] != 0:
        current_is_win = pnls[-1] > 0
        for p in reversed(pnls):
            if p == 0:
                break  # neutral terminates the run
            if (p > 0) == current_is_win:
                current_len += 1
            else:
                break

    return {
        "max_win_streak": max_win,
        "max_loss_streak": max_loss,
        "current_streak": current_len,
        "current_streak_is_win": current_is_win,
    }


def compute_metrics_from_trades(
    trades: List[Dict[str, Any]],
    allocated_capital: float = 100_000.0,
    risk_free_rate: Optional[float] = None,
    equity_history: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Calculate Sharpe, Sortino, Calmar, Win Rate, Drawdown, Expectancy, and Streaks.

    ``risk_free_rate=None`` (default) resolves via :func:`_risk_free_rate`
    (env → config/performance.yaml → 6%). Sentinels: ``profit_factor`` is
    ``None`` for a no-loss book (∞); ``sortino_ratio`` is ``None`` when
    there is no downside deviation ("n/a", NOT silently the Sharpe).
    """
    if risk_free_rate is None:
        risk_free_rate = _risk_free_rate()
    capital = max(1.0, float(allocated_capital))
    pnls = [float(t.get("pnl", 0.0)) for t in trades]
    total_trades = len(pnls)

    if total_trades == 0:
        return {
            "total_trades": 0,
            "total_pnl": 0.0,
            "total_return_pct": 0.0,
            "annualized_return_pct": 0.0,
            "sharpe_ratio": 0.0,
            "sortino_ratio": 0.0,
            "calmar_ratio": 0.0,
            "win_rate": 0.0,
            "winning_trades": 0,
            "losing_trades": 0,
            "profit_factor": 0.0,
            "expectancy": 0.0,
            "max_drawdown_pct": 0.0,
            "max_drawdown_amount": 0.0,
            "avg_win": 0.0,
            "avg_loss": 0.0,
            "largest_win": 0.0,
            "largest_loss": 0.0,
            "recovery_factor": 0.0,
            "streaks": _calculate_streaks([]),
        }

    total_pnl = sum(pnls)
    total_return_pct = round((total_pnl / capital) * 100.0, 2)

    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    winning_trades = len(wins)
    losing_trades = len(losses)
    win_rate = round((winning_trades / total_trades) * 100.0, 1)

    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    if gross_loss > 0:
        profit_factor: Optional[float] = round(gross_profit / gross_loss, 2)
    elif gross_profit > 0:
        profit_factor = None  # ∞ — no losing trades (fix #8: null, not 99.99)
    else:
        profit_factor = 0.0

    expectancy = round(total_pnl / total_trades, 2)
    avg_win = round(sum(wins) / len(wins), 2) if wins else 0.0
    avg_loss = round(abs(sum(losses)) / len(losses), 2) if losses else 0.0
    largest_win = round(max(pnls), 2) if pnls else 0.0
    largest_loss = round(min(pnls), 2) if pnls else 0.0

    # Drawdown calculation
    # Prefer equity_history if available; otherwise derive from cumulative trade PnL
    max_dd_pct = 0.0
    max_dd_amount = 0.0

    if equity_history and len(equity_history) > 1:
        eq_series = pd.Series([float(pt.get("equity", capital)) for pt in equity_history])
        running_max = eq_series.cummax()
        dd_series = (eq_series - running_max) / running_max * 100.0
        max_dd_pct = abs(float(dd_series.min())) if not dd_series.empty else 0.0
        dd_amt_series = eq_series - running_max
        max_dd_amount = abs(float(dd_amt_series.min())) if not dd_amt_series.empty else 0.0
    else:
        # Step equity from trades
        cum_pnl = np.cumsum([0.0] + pnls)
        curve = capital + cum_pnl
        running_max = np.maximum.accumulate(curve)
        dd = (curve - running_max) / running_max * 100.0
        max_dd_pct = abs(float(np.min(dd))) if len(dd) > 0 else 0.0
        dd_amt = curve - running_max
        max_dd_amount = abs(float(np.min(dd_amt))) if len(dd_amt) > 0 else 0.0

    max_dd_pct = round(max_dd_pct, 2)
    max_dd_amount = round(max_dd_amount, 2)

    # Recovery factor
    recovery_factor = round(total_pnl / max_dd_amount, 2) if max_dd_amount > 0 else 0.0

    # Daily grouping for annualised Sharpe / Sortino
    daily_returns_list = []
    trade_dates = []
    for t in trades:
        ts = _parse_ts(t.get("exit_ts") or t.get("timestamp") or t.get("entry_ts"))
        trade_dates.append(
            (ts.date() if ts else datetime.now(timezone.utc).date(), float(t.get("pnl", 0.0)))
        )

    df_pnl = pd.DataFrame(trade_dates, columns=["date", "pnl"])
    if not df_pnl.empty:
        daily_pnl = df_pnl.groupby("date")["pnl"].sum()
        daily_ret = daily_pnl / capital
        daily_returns_list = daily_ret.tolist()

    sortino: Optional[float]
    if len(daily_returns_list) >= 2:
        ret_series = pd.Series(daily_returns_list)
        std_ret = ret_series.std(ddof=0)
        excess_daily_rf = risk_free_rate / 252.0
        excess_mean = ret_series.mean() - excess_daily_rf
        sharpe = math.sqrt(252.0) * (excess_mean / std_ret) if std_ret > 0 else 0.0

        downside = ret_series[ret_series < 0]
        downside_std = downside.std(ddof=0)
        # Fix #5: no downside deviation → "n/a" (None), never silently the
        # Sharpe number wearing a Sortino label.
        sortino = (
            math.sqrt(252.0) * (excess_mean / downside_std) if downside_std > 0 else None
        )
    elif len(pnls) >= 2:
        # Approximate per-trade Sharpe annualized assuming ~250 trading periods
        pnl_series = pd.Series(pnls) / capital
        std_pnl = pnl_series.std(ddof=0)
        mean_pnl = pnl_series.mean()
        sharpe = (
            math.sqrt(min(252, max(12, len(pnls)))) * (mean_pnl / std_pnl) if std_pnl > 0 else 0.0
        )
        downside = pnl_series[pnl_series < 0]
        downside_std = downside.std(ddof=0)
        sortino = (
            math.sqrt(min(252, max(12, len(pnls)))) * (mean_pnl / downside_std)
            if downside_std > 0
            else None
        )
    else:
        sharpe = 0.0
        sortino = 0.0

    # Calmar = ANNUALIZED return / max drawdown (fix #5). The old numerator
    # was the raw window return, so a 30d and a 1y view of the same book
    # produced incomparable "Calmar" numbers. Annualize over the observed
    # trade span (first→last exit, floor 1 day).
    parsed_dates = [d for d, _ in trade_dates if d is not None]
    if parsed_dates:
        span_days = max((max(parsed_dates) - min(parsed_dates)).days, 1)
    else:
        span_days = 1
    annualized_return_pct = round(total_return_pct * (365.0 / span_days), 2)
    if max_dd_pct > 0:
        calmar = round(annualized_return_pct / max_dd_pct, 2)
    else:
        calmar = annualized_return_pct if annualized_return_pct > 0 else 0.0

    return {
        "total_trades": total_trades,
        "total_pnl": round(total_pnl, 2),
        "total_return_pct": total_return_pct,
        "annualized_return_pct": annualized_return_pct,
        "sharpe_ratio": round(float(sharpe), 2),
        "sortino_ratio": round(float(sortino), 2) if sortino is not None else None,
        "calmar_ratio": calmar,
        "win_rate": win_rate,
        "winning_trades": winning_trades,
        "losing_trades": losing_trades,
        "profit_factor": profit_factor,
        "expectancy": expectancy,
        "max_drawdown_pct": max_dd_pct,
        "max_drawdown_amount": max_dd_amount,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "largest_win": largest_win,
        "largest_loss": largest_loss,
        "recovery_factor": recovery_factor,
        "streaks": _calculate_streaks(pnls),
    }


def get_health_rating(
    sharpe: float,
    max_dd_pct: float,
    win_rate: Optional[float] = None,
    profit_factor: Optional[float] = ...,  # type: ignore[assignment]
    total_trades: Optional[int] = None,
) -> Dict[str, str]:
    """Return health status indicator ⚪ / 🟢 / 🟡 / 🔴 and descriptive label.

    Fix #6 (2026-09-28):

    * ``win_rate`` no longer participates — a 50% WR floor punished valid
      low-winrate/high-RR structures (credit spreads lose most days by
      design). Edge quality is judged by **profit factor** instead
      (``None`` = no losing trades = passes).
    * ``total_trades`` gates the rating: below :data:`MIN_HEALTH_SAMPLE`
      trades the honest answer is "insufficient sample", not 🟢 on 3 trades.

    ``win_rate`` is retained (ignored) for call-site compatibility; the
    ``...`` sentinel on ``profit_factor`` distinguishes "not provided"
    (legacy caller — PF criterion skipped) from ``None`` (∞ — passes).
    """
    if total_trades is not None and total_trades < MIN_HEALTH_SAMPLE:
        return {
            "status": "gray",
            "badge": "⚪ Insufficient sample",
            "label": f"Insufficient sample (n={total_trades} < {MIN_HEALTH_SAMPLE})",
        }
    if profit_factor is ...:
        pf_healthy = True  # legacy caller — no PF available
    else:
        pf_healthy = profit_factor is None or float(profit_factor) >= 1.5
    if sharpe >= 1.5 and max_dd_pct <= 10.0 and pf_healthy:
        return {"status": "green", "badge": "🟢 Healthy", "label": "Excellent / Healthy"}
    if sharpe >= 1.0 and max_dd_pct <= 15.0:
        return {"status": "yellow", "badge": "🟡 Warning", "label": "Moderate / Watch closely"}
    return {"status": "red", "badge": "🔴 Alert", "label": "Underperforming / Critical"}


def _natural_key(t: Dict[str, Any]) -> tuple:
    """Dedupe key matching LiveTradePersister's flush key (exit+symbol+pnl)."""
    exit_ts = str(t.get("exit_ts") or "")[:19]
    return (exit_ts, str(t.get("symbol") or ""), f"{float(t.get('pnl') or 0.0):.2f}")


def _instrument_class(t: Dict[str, Any]) -> str:
    """equity | option — from the runner's ``kind`` tag, else symbol shape."""
    kind = str(t.get("kind") or "").lower()
    if kind in ("equity", "option"):
        return kind
    if _OPTION_SYMBOL_RE.search(str(t.get("symbol") or "")):
        return "option"
    return "equity"


class AnalyticsService:
    """Core analytics engine interfacing with PortfolioManager and runners."""

    def __init__(self):
        self.mgr = get_portfolio_manager()

    # ------------------------------------------------------------------
    # Persisted trade history (fix #3 — memory is a cache, not the truth)
    # ------------------------------------------------------------------

    def _load_persisted_trades(self, runner: Any) -> List[Dict[str, Any]]:
        """Cross-session trades for ``runner`` from the DB `trades` table.

        Reads through the SAME DatabaseManager the manager's
        LiveTradePersister writes with, keyed by the persister's portfolio
        naming convention (``"<name> [<instance8>]"``). Fail-soft: any
        problem (no persister, no DB, schema drift) returns ``[]`` and the
        tab falls back to the in-memory tail exactly as before.
        """
        persister = getattr(self.mgr, "_trade_persister", None)
        db = getattr(persister, "db", None)
        if db is None:
            return []
        try:
            from backtest.db.models import Portfolio as PortfolioRow
            from backtest.db.models import Trade as TradeRow

            name = f"{runner.config.name} [{runner.instance_id[:8]}]"
            out: List[Dict[str, Any]] = []
            with db.session() as session:
                row = (
                    session.query(PortfolioRow)
                    .filter(PortfolioRow.name == name)
                    .one_or_none()
                )
                if row is None:
                    return []
                rows = (
                    session.query(TradeRow)
                    .filter(TradeRow.portfolio_id == row.portfolio_id)
                    .order_by(TradeRow.exit_time)
                    .all()
                )
                for r in rows:
                    symbol = str(r.symbol or "")
                    trade = {
                        "symbol": symbol,
                        "kind": "option" if _OPTION_SYMBOL_RE.search(symbol) else "equity",
                        "side": "LONG" if str(r.direction or "long") == "long" else "SHORT",
                        "qty": float(r.quantity or 0),
                        "entry_price": float(r.entry_price or 0),
                        "exit_price": float(r.exit_price or 0),
                        "entry_ts": r.entry_time.isoformat() if r.entry_time else None,
                        "exit_ts": r.exit_time.isoformat() if r.exit_time else None,
                        "pnl": round(float(r.net_pnl or 0), 2),
                        "win": float(r.net_pnl or 0) >= 0,
                        "persisted": True,
                    }
                    out.append(trade)
            return out
        except Exception:  # noqa: BLE001 — persistence must never break the tab
            log.debug("persisted trade read failed — memory-only analytics", exc_info=True)
            return []

    def _runner_trades_with_history(self, runner: Any) -> tuple:
        """(merged trades, provenance dict) — DB rows + in-memory tail.

        Memory wins on natural-key collisions (it carries richer fields:
        coids, exit reasons, structure ids). Result is exit-time sorted.
        """
        memory = [dict(t) for t in runner.closed_trades]  # never mutate runner state
        persisted = self._load_persisted_trades(runner)
        seen = {_natural_key(t) for t in memory}
        merged = memory + [t for t in persisted if _natural_key(t) not in seen]
        merged.sort(key=lambda t: (t.get("exit_ts") or "", t.get("symbol") or ""))
        for t in merged:
            t["instrument_class"] = _instrument_class(t)
        history = {
            "memory_trades": len(memory),
            "persisted_trades": len(persisted),
            "merged_trades": len(merged),
            "source": "memory+db" if persisted else "memory",
        }
        return merged, history

    def collect_books(
        self, period: str = "30d", mode: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """One record per runner, with broker/segment attribution attached.

        This is the SINGLE read path for per-runner analytics. ``/analytics``
        overview (:meth:`get_portfolio_overview`) and the cross-broker
        aggregation (PRD-003, :mod:`backtest.analytics.cross_broker`) both
        consume it, so a runner's broker label, its trades and its metrics can
        never come from two different reads and disagree.

        Each record::

            {
              "instance_id", "name", "strategy_name", "mode", "status",
              "symbols", "allocated_capital",
              "broker",              # display label: the execution broker for
                                    # live runners, "paper" otherwise
              "segment",             # capital segment the runner trades in
              "execution_broker",    # the configured broker, None for paper
              "equity", "daily_pnl",
              "trades",              # period-filtered, broker-tagged
              "all_trades",          # unfiltered (period = "all_time")
              "metrics", "health", "history",
            }

        ``broker`` comes from :meth:`PortfolioManager.broker_of`, the same
        accessor the Orders tab and the risk page's per-broker rollup use, so
        the three views can never disagree about which venue a row belongs to.
        """
        books: List[Dict[str, Any]] = []
        for r_meta in self.mgr.list_instances(mode=mode):
            inst_id = r_meta.get("instance_id")
            runner = self.mgr.get_runner(inst_id)
            if not runner:
                continue

            allocated = float(r_meta.get("allocated_capital", 100_000.0))
            status = str(r_meta.get("status", "STOPPED")).upper()
            closed, history = self._runner_trades_with_history(runner)
            period_trades = _filter_by_period(closed, period)
            metrics = compute_metrics_from_trades(
                period_trades,
                allocated_capital=allocated,
                equity_history=runner.equity_curve,
            )
            health = get_health_rating(
                metrics["sharpe_ratio"],
                metrics["max_drawdown_pct"],
                metrics["win_rate"],
                profit_factor=metrics["profit_factor"],
                total_trades=metrics["total_trades"],
            )

            broker = self.mgr.broker_of(inst_id) or "paper"
            for t in period_trades:
                t["broker"] = broker
                t["segment"] = r_meta.get("segment")

            books.append({
                "instance_id": inst_id,
                "name": r_meta.get("name"),
                "strategy_name": r_meta.get("strategy_name"),
                "mode": r_meta.get("mode"),
                "status": status,
                "symbols": r_meta.get("symbols", []),
                "allocated_capital": allocated,
                "broker": broker,
                "segment": r_meta.get("segment"),
                "execution_broker": r_meta.get("execution_broker"),
                "equity": float(r_meta.get("equity") or 0.0),
                "daily_pnl": float(r_meta.get("daily_pnl") or 0.0),
                "trades": period_trades,
                "all_trades": closed,
                "metrics": metrics,
                "health": health,
                "history": history,
                "last_trade_ts": closed[-1].get("exit_ts") if closed else None,
                # Kept so the overview's sparkline can read the runner's own
                # equity curve without a second manager lookup per runner.
                "equity_curve": runner.equity_curve,
            })
        return books

    def get_portfolio_overview(
        self, period: str = "30d", mode: Optional[str] = None
    ) -> Dict[str, Any]:
        """Aggregate performance overview across all runners."""
        books = self.collect_books(period=period, mode=mode)
        all_closed_trades: List[Dict[str, Any]] = []
        strategy_cards: List[Dict[str, Any]] = []

        total_allocated_capital = 0.0
        active_count = 0

        for book in books:
            total_allocated_capital += book["allocated_capital"]
            if book["status"] in ("RUNNING", "ACTIVE"):
                active_count += 1

            allocated = book["allocated_capital"]
            period_trades = book["trades"]
            metrics = book["metrics"]

            # Mini equity curve (last 10-20 points) + time labels (fix #9:
            # index-labelled sparklines were misleading after decimation).
            mini_curve = []
            mini_curve_ts = []
            equity_curve = book["equity_curve"] or []
            if equity_curve:
                sample_pts = equity_curve[-20:]
                mini_curve = [round(float(p.get("equity", allocated)), 1) for p in sample_pts]
                mini_curve_ts = [str(p.get("ts") or "")[:16] for p in sample_pts]
            elif period_trades:
                tail = period_trades[-20:]
                cum = np.cumsum([float(t.get("pnl", 0)) for t in tail])
                mini_curve = [round(allocated + float(c), 1) for c in cum]
                mini_curve_ts = [str(t.get("exit_ts") or "")[:16] for t in tail]
            else:
                mini_curve = [allocated]
                mini_curve_ts = [""]

            for t in period_trades:
                et = dict(t)
                et["runner_id"] = book["instance_id"]
                et["strategy_name"] = book["strategy_name"]
                all_closed_trades.append(et)

            strategy_cards.append({
                "instance_id": book["instance_id"],
                "name": book["name"],
                "strategy_name": book["strategy_name"],
                "mode": book["mode"],
                "status": book["status"],
                "symbols": book["symbols"],
                "allocated_capital": allocated,
                # Multi-broker attribution (PRD-003): the strategy table and
                # the cross-broker rollup must agree on which venue a card
                # belongs to, so both read the same three keys.
                "broker": book["broker"],
                "segment": book["segment"],
                "execution_broker": book["execution_broker"],
                "metrics": metrics,
                "mini_curve": mini_curve,
                "mini_curve_ts": mini_curve_ts,
                "health": book["health"],
                "history": book["history"],
                "last_trade_ts": book["last_trade_ts"],
            })

        # Registry metadata (description/version/author) per strategy type —
        # one lookup per distinct strategy_name, not per runner.
        self._card_descriptions(strategy_cards)

        portfolio_metrics = compute_metrics_from_trades(
            all_closed_trades,
            allocated_capital=total_allocated_capital or 100_000.0,
        )

        # Portfolio aggregate equity curve
        portfolio_equity_curve = self._build_portfolio_equity_curve(books, period)

        # Recent alerts (e.g. degrading Sharpe, excessive DD)
        alerts = self._generate_overview_alerts(strategy_cards)

        return {
            "period": period,
            "mode": mode or "all",
            "portfolio_metrics": portfolio_metrics,
            "active_runners": active_count,
            "total_runners": len(books),
            "total_allocated_capital": total_allocated_capital,
            "strategy_cards": strategy_cards,
            "portfolio_equity_curve": portfolio_equity_curve,
            "alerts": alerts,
        }

    def _card_descriptions(self, cards: List[Dict[str, Any]]) -> None:
        """Attach ``strategy_meta`` (description/version/author) to each card."""
        cache: Dict[str, Optional[Dict[str, Any]]] = {}
        for card in cards:
            sname = str(card.get("strategy_name") or "")
            if sname not in cache:
                cache[sname] = strategy_description(sname)
            card["strategy_meta"] = cache[sname]

    def get_strategy_detail(
        self, instance_id: str, period: str = "90d"
    ) -> Optional[Dict[str, Any]]:
        """Deep dive analytics for a single strategy runner."""
        runner = self.mgr.get_runner(instance_id)
        if not runner:
            return None

        state = runner.get_state()
        detail = runner.get_detail()
        allocated = float(state.get("allocated_capital", 100_000.0))
        closed, history = self._runner_trades_with_history(runner)
        period_trades = _filter_by_period(closed, period)

        metrics = compute_metrics_from_trades(
            period_trades,
            allocated_capital=allocated,
            equity_history=runner.equity_curve,
        )

        health = get_health_rating(
            metrics["sharpe_ratio"],
            metrics["max_drawdown_pct"],
            metrics["win_rate"],
            profit_factor=metrics["profit_factor"],
            total_trades=metrics["total_trades"],
        )

        equity_curve_data = self._build_runner_equity_curve(runner, period_trades, allocated)
        monthly_breakdown = self._build_monthly_breakdown(period_trades, allocated)
        trade_distribution = self._build_trade_distribution(period_trades)
        rolling_metrics = self._calculate_rolling_metrics(
            period_trades, allocated, window_trades=10
        )
        edge_degradation = self._detect_edge_degradation(rolling_metrics, metrics)

        return {
            "instance_id": instance_id,
            "name": state.get("name"),
            "strategy_name": state.get("strategy_name"),
            "mode": state.get("mode"),
            "source": state.get("source"),
            "status": state.get("status"),
            "symbols": state.get("symbols", []),
            "timeframe": state.get("timeframe"),
            "allocated_capital": allocated,
            "params": detail.get("params", {}),
            "strategy_meta": strategy_description(state.get("strategy_name")),
            "period": period,
            "metrics": metrics,
            "health": health,
            "equity_curve": equity_curve_data,
            "monthly_breakdown": monthly_breakdown,
            "trade_distribution": trade_distribution,
            "rolling_metrics": rolling_metrics,
            "edge_degradation": edge_degradation,
            "recent_trades": period_trades[-50:],
            "history": history,
        }

    def _build_portfolio_equity_curve(
        self, books: List[Dict[str, Any]], period: str
    ) -> List[Dict[str, Any]]:
        """Date-joined portfolio equity with CARRY-FORWARD (fix #7).

        The old points-map summed only the dates each runner reported: a
        runner starting mid-period contributed nothing to earlier dates, so
        the portfolio curve jumped a whole allocation the day it appeared.
        Now every runner contributes to EVERY date — its allocated capital
        before its first observation, its last known equity after — and the
        P&L baseline is the curve's ACTUAL first value, not the sum of
        allocations.
        """
        total_initial = (
            sum(float(r.get("allocated_capital", 100_000.0)) for r in books) or 100_000.0
        )

        # Per-runner: date → last observed equity that date.
        per_runner: List[tuple] = []  # (allocated, {date: equity})
        all_dates: set = set()
        for book in books:
            equity_curve = book.get("equity_curve") or []
            allocated = float(book.get("allocated_capital", 100_000.0))
            by_date: Dict[str, float] = {}
            for pt in equity_curve:
                ts = pt.get("ts")
                if not ts:
                    continue
                by_date[str(ts)[:10]] = float(pt.get("equity", allocated))
            if by_date:
                per_runner.append((allocated, by_date))
                all_dates.update(by_date.keys())

        if not all_dates:
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            return [{"date": today, "equity": total_initial, "pnl": 0.0, "drawdown_pct": 0.0}]

        sorted_dates = sorted(all_dates)
        # Carry-forward walk: before a runner's first date → its allocation;
        # after → its last known equity.
        summed: Dict[str, float] = {d: 0.0 for d in sorted_dates}
        for allocated, by_date in per_runner:
            last = allocated
            for d in sorted_dates:
                if d in by_date:
                    last = by_date[d]
                summed[d] += last

        baseline = summed[sorted_dates[0]]  # actual start equity, not allocations
        curve = []
        peak = baseline
        for d in sorted_dates:
            eq = summed[d]
            if eq > peak:
                peak = eq
            dd_pct = round((eq - peak) / peak * 100.0, 2) if peak > 0 else 0.0
            curve.append({
                "date": d,
                "equity": round(eq, 2),
                "pnl": round(eq - baseline, 2),
                "drawdown_pct": dd_pct,
            })
        return curve

    def _build_runner_equity_curve(
        self, runner, period_trades: List[Dict[str, Any]], capital: float
    ) -> List[Dict[str, Any]]:
        curve = []
        if runner.equity_curve and len(runner.equity_curve) > 1:
            peak = capital
            for pt in runner.equity_curve:
                eq = float(pt.get("equity", capital))
                ts = pt.get("ts", "")
                if eq > peak:
                    peak = eq
                dd = round((eq - peak) / peak * 100.0, 2) if peak > 0 else 0.0
                curve.append({
                    "timestamp": ts,
                    "date": ts[:10] if ts else "",
                    "equity": round(eq, 2),
                    "pnl": round(eq - capital, 2),
                    "drawdown_pct": dd,
                })
            return curve

        # Fallback step curve from period trades
        cum = 0.0
        peak = capital
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        curve.append({
            "timestamp": today,
            "date": today,
            "equity": capital,
            "pnl": 0.0,
            "drawdown_pct": 0.0,
        })

        for i, t in enumerate(period_trades):
            pnl = float(t.get("pnl", 0.0))
            cum += pnl
            eq = capital + cum
            if eq > peak:
                peak = eq
            dd = round((eq - peak) / peak * 100.0, 2) if peak > 0 else 0.0
            ts = t.get("exit_ts") or today
            curve.append({
                "timestamp": ts,
                "date": ts[:10] if ts else today,
                "equity": round(eq, 2),
                "pnl": round(cum, 2),
                "drawdown_pct": dd,
                "trade_num": i + 1,
            })
        return curve

    def _build_monthly_breakdown(
        self, trades: List[Dict[str, Any]], capital: float
    ) -> List[Dict[str, Any]]:
        if not trades:
            return []

        buckets: Dict[str, List[tuple]] = {}
        for t in trades:
            ts = _parse_ts(t.get("exit_ts") or t.get("timestamp") or t.get("entry_ts"))
            month_key = ts.strftime("%Y-%m") if ts else "Current"
            pnl = float(t.get("pnl", 0.0))
            buckets.setdefault(month_key, []).append((ts.date() if ts else None, pnl))

        monthly = []
        for m_key in sorted(buckets.keys(), reverse=True):
            dated = buckets[m_key]
            pnls = [p for _, p in dated]
            total_trades = len(pnls)
            wins = [p for p in pnls if p > 0]
            m_pnl = sum(pnls)
            win_rate = round(len(wins) / total_trades * 100.0, 1) if total_trades else 0.0
            ret_pct = round((m_pnl / capital) * 100.0, 2)

            cum_pnl = np.cumsum([0.0] + pnls)
            running_max = np.maximum.accumulate(cum_pnl)
            dd_amt = cum_pnl - running_max
            m_dd = abs(round(float(np.min(dd_amt)) / capital * 100.0, 2)) if len(dd_amt) else 0.0

            # Real monthly Sharpe (fix #5): annualized from DAILY-grouped
            # returns within the month. The old mean/std*sqrt(n_trades) was
            # not a Sharpe under any convention (its scale grew with trade
            # count). Needs ≥2 distinct trading days — otherwise None ("—"),
            # never a fake number.
            day_pnl: Dict[Any, float] = {}
            for d, p in dated:
                if d is not None:
                    day_pnl[d] = day_pnl.get(d, 0.0) + p
            sh: Optional[float] = None
            if len(day_pnl) >= 2:
                rets = pd.Series(list(day_pnl.values())) / capital
                std = rets.std(ddof=0)
                sh = round(float(rets.mean() / std) * math.sqrt(252.0), 2) if std > 0 else None

            monthly.append({
                "month": m_key,
                "trades": total_trades,
                "win_rate": win_rate,
                "pnl": round(m_pnl, 2),
                "return_pct": ret_pct,
                "max_drawdown_pct": m_dd,
                "sharpe_ratio": sh,
            })
        return monthly

    @staticmethod
    def _histogram(pnls: List[float]) -> List[Dict[str, Any]]:
        if not pnls:
            return []
        min_p = min(pnls)
        max_p = max(pnls)
        bins = 6
        step = (max_p - min_p) / bins if max_p > min_p else 100.0
        histogram = []
        for b in range(bins):
            b_start = min_p + b * step
            b_end = b_start + step
            count = sum(1 for p in pnls if b_start <= p <= b_end)
            histogram.append({
                "range": f"₹{int(b_start):,} to ₹{int(b_end):,}",
                "count": count,
                "type": "win" if b_start >= 0 else "loss",
            })
        return histogram

    def _build_trade_distribution(self, trades: List[Dict[str, Any]]) -> Dict[str, Any]:
        """P&L histogram + insights, SPLIT by instrument class (fix #10).

        Equity rows (share qty × price) and option rows (lots × net premium)
        live in different units — one combined histogram mixes them into a
        meaningless shape. The combined view stays (back-compat + the
        single-class common case is unaffected) and ``by_class`` carries the
        honest per-class split whenever the book is mixed.
        """
        if not trades:
            return {"histogram": [], "insights": [], "by_class": {}}

        pnls = [float(t.get("pnl", 0.0)) for t in trades]
        by_class_pnls: Dict[str, List[float]] = {}
        for t in trades:
            by_class_pnls.setdefault(_instrument_class(t), []).append(
                float(t.get("pnl", 0.0))
            )

        histogram = self._histogram(pnls)
        by_class = {
            cls: {"histogram": self._histogram(cls_pnls), "trades": len(cls_pnls)}
            for cls, cls_pnls in sorted(by_class_pnls.items())
        }

        insights = []
        if len(by_class_pnls) > 1:
            parts = ", ".join(f"{len(v)} {k}" for k, v in sorted(by_class_pnls.items()))
            insights.append(
                f"Mixed book ({parts}) — units differ; see the per-class split."
            )
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]
        if wins:
            insights.append(f"Average win is ₹{int(sum(wins)/len(wins)):,}")
        if losses:
            insights.append(f"Average loss is ₹{int(abs(sum(losses))/len(losses)):,}")
        fat_tail = [
            p for p in losses if abs(p) > 2 * (abs(sum(losses) / len(losses)) if losses else 1)
        ]
        if fat_tail:
            insights.append(f"⚠️ {len(fat_tail)} outlier losses exceeded 2x average loss.")

        return {
            "histogram": histogram,
            "insights": insights,
            "by_class": by_class,
        }

    def _calculate_rolling_metrics(
        self, trades: List[Dict[str, Any]], capital: float, window_trades: int = 10
    ) -> List[Dict[str, Any]]:
        if len(trades) < window_trades:
            return []

        rolling = []
        for i in range(window_trades, len(trades) + 1):
            window_slice = trades[i - window_trades:i]
            pnls = [float(t.get("pnl", 0.0)) for t in window_slice]
            wins = sum(1 for p in pnls if p > 0)
            wr = round(wins / window_trades * 100.0, 1)

            p_series = pd.Series(pnls)
            std = p_series.std(ddof=0)
            mean = p_series.mean()
            sh = float(mean / std * math.sqrt(252)) if std > 0 else 0.0

            ts = window_slice[-1].get("exit_ts") or f"Trade {i}"
            rolling.append({
                "trade_index": i,
                "timestamp": ts,
                "date": ts[:10] if isinstance(ts, str) else "",
                "rolling_sharpe": round(sh, 2),
                "rolling_win_rate": wr,
            })
        return rolling

    def _detect_edge_degradation(
        self, rolling: List[Dict[str, Any]], current_metrics: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        if len(rolling) < 4:
            return None

        recent_sharpes = [r["rolling_sharpe"] for r in rolling[-3:]]
        earlier_sharpes = [r["rolling_sharpe"] for r in rolling[:-3]]
        if not earlier_sharpes:
            return None

        recent_avg = np.mean(recent_sharpes)
        earlier_avg = np.mean(earlier_sharpes)

        if earlier_avg > 0 and recent_avg < earlier_avg:
            drop_pct = round(((earlier_avg - recent_avg) / earlier_avg) * 100.0, 1)
            if drop_pct >= 20.0 or recent_avg < 1.0:
                return {
                    "alert_type": "sharpe_decline",
                    "severity": "warning" if recent_avg >= 1.0 else "critical",
                    "message": (
                        f"Rolling Sharpe dropped by {drop_pct}% "
                        f"(from {earlier_avg:.2f} to {recent_avg:.2f})"
                    ),
                    "recent_sharpe": round(recent_avg, 2),
                    "baseline_sharpe": round(earlier_avg, 2),
                    "drop_pct": drop_pct,
                }

        # Absolute floor (§2.3): a strategy that was ALWAYS bad has nothing
        # to "degrade" from, so the relative check above never fires. A
        # negative recent rolling Sharpe is an alert on its own.
        if recent_avg < 0.0:
            return {
                "alert_type": "sharpe_floor",
                "severity": "critical",
                "message": (
                    f"Rolling Sharpe is negative ({recent_avg:.2f}) — "
                    "the strategy is losing on a risk-adjusted basis."
                ),
                "recent_sharpe": round(recent_avg, 2),
                "baseline_sharpe": round(earlier_avg, 2),
                "drop_pct": 0.0,
            }
        return None

    def _generate_overview_alerts(
        self, strategy_cards: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        alerts = []
        for c in strategy_cards:
            m = c["metrics"]
            if m["sharpe_ratio"] < 1.0 and m["total_trades"] >= 5:
                alerts.append({
                    "strategy": c["name"],
                    "instance_id": c["instance_id"],
                    "severity": "warning",
                    "message": (
                        f"{c['name']}: Low Sharpe ratio ({m['sharpe_ratio']}) "
                        f"across {m['total_trades']} trades."
                    ),
                })
            if m["max_drawdown_pct"] > 15.0:
                alerts.append({
                    "strategy": c["name"],
                    "instance_id": c["instance_id"],
                    "severity": "critical",
                    "message": f"{c['name']}: High drawdown observed (-{m['max_drawdown_pct']}%).",
                })
            streaks = m.get("streaks", {})
            if streaks.get("max_loss_streak", 0) >= 4:
                alerts.append({
                    "strategy": c["name"],
                    "instance_id": c["instance_id"],
                    "severity": "info",
                    "message": (
                        f"{c['name']}: Longest losing streak reached "
                        f"{streaks['max_loss_streak']} trades."
                    ),
                })
        return alerts
