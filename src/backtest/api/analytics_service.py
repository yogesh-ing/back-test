"""Analytics service for live & paper trading performance evaluation.

Provides quantitative performance analytics, risk ratios, equity & drawdown curves,
trade distribution, monthly breakdowns, and edge degradation detection for
individual running strategies as well as portfolio-level aggregation.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from backtest.forward.portfolio_manager import get_portfolio_manager
from backtest.logging_config import get_logger

log = get_logger(__name__)


def _parse_ts(ts_val: Any) -> Optional[datetime]:
    if not ts_val:
        return None
    if isinstance(ts_val, datetime):
        return ts_val if ts_val.tzinfo else ts_val.replace(tzinfo=timezone.utc)
    try:
        dt = pd.to_datetime(ts_val)
        if dt.tzinfo is None:
            dt = dt.tz_localize("UTC")
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

    last_p = pnls[-1]
    current_is_win = last_p >= 0
    current_len = 0
    for p in reversed(pnls):
        if (p >= 0) == current_is_win:
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
    risk_free_rate: float = 0.06,
    equity_history: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Calculate Sharpe, Sortino, Calmar, Win Rate, Drawdown, Expectancy, and Streaks."""
    capital = max(1.0, float(allocated_capital))
    pnls = [float(t.get("pnl", 0.0)) for t in trades]
    total_trades = len(pnls)

    if total_trades == 0:
        return {
            "total_trades": 0,
            "total_pnl": 0.0,
            "total_return_pct": 0.0,
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
        profit_factor = round(gross_profit / gross_loss, 2)
    elif gross_profit > 0:
        profit_factor = 99.99
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
        trade_dates.append((ts.date() if ts else datetime.now(timezone.utc).date(), float(t.get("pnl", 0.0))))

    df_pnl = pd.DataFrame(trade_dates, columns=["date", "pnl"])
    if not df_pnl.empty:
        daily_pnl = df_pnl.groupby("date")["pnl"].sum()
        daily_ret = daily_pnl / capital
        daily_returns_list = daily_ret.tolist()

    if len(daily_returns_list) >= 2:
        ret_series = pd.Series(daily_returns_list)
        std_ret = ret_series.std(ddof=0)
        excess_daily_rf = risk_free_rate / 252.0
        excess_mean = ret_series.mean() - excess_daily_rf
        sharpe = math.sqrt(252.0) * (excess_mean / std_ret) if std_ret > 0 else 0.0

        downside = ret_series[ret_series < 0]
        downside_std = downside.std(ddof=0)
        sortino = math.sqrt(252.0) * (excess_mean / downside_std) if downside_std > 0 else (sharpe if sharpe > 0 else 0.0)
    elif len(pnls) >= 2:
        # Approximate per-trade Sharpe annualized assuming ~250 trading periods
        pnl_series = pd.Series(pnls) / capital
        std_pnl = pnl_series.std(ddof=0)
        mean_pnl = pnl_series.mean()
        sharpe = math.sqrt(min(252, max(12, len(pnls)))) * (mean_pnl / std_pnl) if std_pnl > 0 else 0.0
        downside = pnl_series[pnl_series < 0]
        downside_std = downside.std(ddof=0)
        sortino = math.sqrt(min(252, max(12, len(pnls)))) * (mean_pnl / downside_std) if downside_std > 0 else sharpe
    else:
        sharpe = 0.0
        sortino = 0.0

    # Calmar: Annualized Return / Max Drawdown
    if max_dd_pct > 0:
        calmar = round(total_return_pct / max_dd_pct, 2)
    else:
        calmar = round(total_return_pct, 2) if total_return_pct > 0 else 0.0

    return {
        "total_trades": total_trades,
        "total_pnl": round(total_pnl, 2),
        "total_return_pct": total_return_pct,
        "sharpe_ratio": round(float(sharpe), 2),
        "sortino_ratio": round(float(sortino), 2),
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


def get_health_rating(sharpe: float, max_dd_pct: float, win_rate: float) -> Dict[str, str]:
    """Return health status indicator 🟢 / 🟡 / 🔴 and descriptive label."""
    if sharpe >= 1.5 and max_dd_pct <= 10.0 and win_rate >= 50.0:
        return {"status": "green", "badge": "🟢 Healthy", "label": "Excellent / Healthy"}
    if sharpe >= 1.0 and max_dd_pct <= 15.0:
        return {"status": "yellow", "badge": "🟡 Warning", "label": "Moderate / Watch closely"}
    return {"status": "red", "badge": "🔴 Alert", "label": "Underperforming / Critical"}


class AnalyticsService:
    """Core analytics engine interfacing with PortfolioManager and runners."""

    def __init__(self):
        self.mgr = get_portfolio_manager()

    def get_portfolio_overview(self, period: str = "30d", mode: Optional[str] = None) -> Dict[str, Any]:
        """Aggregate performance overview across all runners."""
        runners_summary = self.mgr.list_instances(mode=mode)
        all_closed_trades: List[Dict[str, Any]] = []
        strategy_cards: List[Dict[str, Any]] = []

        total_allocated_capital = 0.0
        active_count = 0

        for r_meta in runners_summary:
            inst_id = r_meta.get("instance_id")
            runner = self.mgr.get_runner(inst_id)
            if not runner:
                continue

            allocated = float(r_meta.get("allocated_capital", 100_000.0))
            total_allocated_capital += allocated
            status = r_meta.get("status", "STOPPED").upper()
            if status in ("RUNNING", "ACTIVE"):
                active_count += 1

            closed = runner.closed_trades
            period_trades = _filter_by_period(closed, period)
            metrics = compute_metrics_from_trades(
                period_trades,
                allocated_capital=allocated,
                equity_history=runner.equity_curve,
            )

            # Mini equity curve (last 10-20 points)
            mini_curve = []
            if runner.equity_curve:
                sample_pts = runner.equity_curve[-20:]
                mini_curve = [round(float(p.get("equity", allocated)), 1) for p in sample_pts]
            elif period_trades:
                cum = np.cumsum([float(t.get("pnl", 0)) for t in period_trades[-20:]])
                mini_curve = [round(allocated + float(c), 1) for c in cum]
            else:
                mini_curve = [allocated]

            health = get_health_rating(
                metrics["sharpe_ratio"],
                metrics["max_drawdown_pct"],
                metrics["win_rate"],
            )

            for t in period_trades:
                et = dict(t)
                et["runner_id"] = inst_id
                et["strategy_name"] = r_meta.get("strategy_name")
                all_closed_trades.append(et)

            strategy_cards.append({
                "instance_id": inst_id,
                "name": r_meta.get("name"),
                "strategy_name": r_meta.get("strategy_name"),
                "mode": r_meta.get("mode"),
                "status": status,
                "symbols": r_meta.get("symbols", []),
                "allocated_capital": allocated,
                "metrics": metrics,
                "mini_curve": mini_curve,
                "health": health,
                "last_trade_ts": closed[-1].get("exit_ts") if closed else None,
            })

        portfolio_metrics = compute_metrics_from_trades(
            all_closed_trades,
            allocated_capital=total_allocated_capital or 100_000.0,
        )

        # Portfolio aggregate equity curve
        portfolio_equity_curve = self._build_portfolio_equity_curve(runners_summary, period)

        # Recent alerts (e.g. degrading Sharpe, excessive DD)
        alerts = self._generate_overview_alerts(strategy_cards)

        return {
            "period": period,
            "mode": mode or "all",
            "portfolio_metrics": portfolio_metrics,
            "active_runners": active_count,
            "total_runners": len(runners_summary),
            "total_allocated_capital": total_allocated_capital,
            "strategy_cards": strategy_cards,
            "portfolio_equity_curve": portfolio_equity_curve,
            "alerts": alerts,
        }

    def get_strategy_detail(self, instance_id: str, period: str = "90d") -> Optional[Dict[str, Any]]:
        """Deep dive analytics for a single strategy runner."""
        runner = self.mgr.get_runner(instance_id)
        if not runner:
            return None

        state = runner.get_state()
        detail = runner.get_detail()
        allocated = float(state.get("allocated_capital", 100_000.0))
        closed = runner.closed_trades
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
        )

        equity_curve_data = self._build_runner_equity_curve(runner, period_trades, allocated)
        monthly_breakdown = self._build_monthly_breakdown(period_trades, allocated)
        trade_distribution = self._build_trade_distribution(period_trades)
        rolling_metrics = self._calculate_rolling_metrics(period_trades, allocated, window_trades=10)
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
            "period": period,
            "metrics": metrics,
            "health": health,
            "equity_curve": equity_curve_data,
            "monthly_breakdown": monthly_breakdown,
            "trade_distribution": trade_distribution,
            "rolling_metrics": rolling_metrics,
            "edge_degradation": edge_degradation,
            "recent_trades": period_trades[-50:],
        }

    def _build_portfolio_equity_curve(self, runners_summary: List[Dict[str, Any]], period: str) -> List[Dict[str, Any]]:
        points_map: Dict[str, float] = {}
        total_initial = sum(float(r.get("allocated_capital", 100_000.0)) for r in runners_summary) or 100_000.0

        for r_meta in runners_summary:
            runner = self.mgr.get_runner(r_meta.get("instance_id"))
            if not runner:
                continue
            for pt in runner.equity_curve:
                ts = pt.get("ts")
                if not ts:
                    continue
                d_str = ts[:10]  # group by date
                points_map[d_str] = points_map.get(d_str, 0.0) + float(pt.get("equity", 0.0))

        if not points_map:
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            return [{"date": today, "equity": total_initial, "pnl": 0.0, "drawdown_pct": 0.0}]

        sorted_dates = sorted(points_map.keys())
        curve = []
        peak = total_initial
        for d in sorted_dates:
            eq = points_map[d]
            if eq > peak:
                peak = eq
            dd_pct = round((eq - peak) / peak * 100.0, 2) if peak > 0 else 0.0
            pnl = round(eq - total_initial, 2)
            curve.append({
                "date": d,
                "equity": round(eq, 2),
                "pnl": pnl,
                "drawdown_pct": dd_pct,
            })
        return curve

    def _build_runner_equity_curve(self, runner, period_trades: List[Dict[str, Any]], capital: float) -> List[Dict[str, Any]]:
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

    def _build_monthly_breakdown(self, trades: List[Dict[str, Any]], capital: float) -> List[Dict[str, Any]]:
        if not trades:
            return []

        buckets: Dict[str, List[float]] = {}
        for t in trades:
            ts = _parse_ts(t.get("exit_ts") or t.get("timestamp") or t.get("entry_ts"))
            month_key = ts.strftime("%Y-%m") if ts else "Current"
            pnl = float(t.get("pnl", 0.0))
            buckets.setdefault(month_key, []).append(pnl)

        monthly = []
        for m_key in sorted(buckets.keys(), reverse=True):
            pnls = buckets[m_key]
            total_trades = len(pnls)
            wins = [p for p in pnls if p > 0]
            m_pnl = sum(pnls)
            win_rate = round(len(wins) / total_trades * 100.0, 1) if total_trades else 0.0
            ret_pct = round((m_pnl / capital) * 100.0, 2)

            cum_pnl = np.cumsum([0.0] + pnls)
            running_max = np.maximum.accumulate(cum_pnl)
            dd_amt = cum_pnl - running_max
            m_dd = abs(round(float(np.min(dd_amt)) / capital * 100.0, 2)) if len(dd_amt) else 0.0

            # Approximate monthly Sharpe
            if len(pnls) >= 2:
                s = pd.Series(pnls)
                sh = float(s.mean() / s.std(ddof=0)) * math.sqrt(len(pnls)) if s.std(ddof=0) > 0 else 0.0
            else:
                sh = 0.0

            monthly.append({
                "month": m_key,
                "trades": total_trades,
                "win_rate": win_rate,
                "pnl": round(m_pnl, 2),
                "return_pct": ret_pct,
                "max_drawdown_pct": m_dd,
                "sharpe_ratio": round(sh, 2),
            })
        return monthly

    def _build_trade_distribution(self, trades: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not trades:
            return {"histogram": [], "insights": []}

        pnls = [float(t.get("pnl", 0.0)) for t in trades]
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

        insights = []
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]
        if wins:
            insights.append(f"Average win is ₹{int(sum(wins)/len(wins)):,}")
        if losses:
            insights.append(f"Average loss is ₹{int(abs(sum(losses))/len(losses)):,}")
        fat_tail = [p for p in losses if abs(p) > 2 * (abs(sum(losses)/len(losses)) if losses else 1)]
        if fat_tail:
            insights.append(f"⚠️ {len(fat_tail)} outlier losses exceeded 2x average loss.")

        return {
            "histogram": histogram,
            "insights": insights,
        }

    def _calculate_rolling_metrics(self, trades: List[Dict[str, Any]], capital: float, window_trades: int = 10) -> List[Dict[str, Any]]:
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

    def _detect_edge_degradation(self, rolling: List[Dict[str, Any]], current_metrics: Dict[str, Any]) -> Optional[Dict[str, Any]]:
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
                    "message": f"Rolling Sharpe dropped by {drop_pct}% (from {earlier_avg:.2f} to {recent_avg:.2f})",
                    "recent_sharpe": round(recent_avg, 2),
                    "baseline_sharpe": round(earlier_avg, 2),
                    "drop_pct": drop_pct,
                }
        return None

    def _generate_overview_alerts(self, strategy_cards: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        alerts = []
        for c in strategy_cards:
            m = c["metrics"]
            if m["sharpe_ratio"] < 1.0 and m["total_trades"] >= 5:
                alerts.append({
                    "strategy": c["name"],
                    "instance_id": c["instance_id"],
                    "severity": "warning",
                    "message": f"{c['name']}: Low Sharpe ratio ({m['sharpe_ratio']}) across {m['total_trades']} trades.",
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
                    "message": f"{c['name']}: Longest losing streak reached {streaks['max_loss_streak']} trades.",
                })
        return alerts
