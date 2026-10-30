"""DiaryService — IST-scoped PnL calendar, day/range summary & book loader.

Implements Slice 1 of ``docs/PNL-DIARY-AND-EDGE-SUITE-PLAN.md``:

* Strict ``mode in ('paper', 'live')`` book isolation — Paper and Live are
  never blended in SQL queries, in-memory runner scans, calendar cells, or
  summary rollups.
* Dual-store idempotent trade loading: persisted ``portfolios JOIN trades``
  (retaining retired/restarted instances) merged deterministically with active
  ``PortfolioManager`` runners via :func:`~backtest.diary.ledger.dedupe_normalized_trades`.
* E1/E3 ``running_mtm`` rule: open unrealized MTM is read live from active
  runners ONLY for today's IST session date (gross mark; exit fees booked on
  close) and is ``None`` on closed historical dates.
* E2 single-commission rule: only one combined ``commission`` figure
  (brokerage + statutory taxes) is emitted; ``Gross - Commission = Net``
  mismatches are surfaced in ``reconcile_anomalies`` / ``ledger_health``,
  never smoothed over.
"""

from __future__ import annotations

import calendar
import logging
from datetime import date, timedelta
from typing import Any, Optional

from backtest.diary.attribution import (
    DEFAULT_INLINE_TRADE_LIMIT,
    build_attribution_tree,
)
from backtest.diary.ledger import (
    dedupe_normalized_trades,
    extract_instance_prefix,
    normalize_db_trade_row,
    normalize_memory_trade_dict,
)
from backtest.diary.regimes import (
    build_regime_matrices,
    validate_regime_clock,
    validate_regime_group_by,
)
from backtest.forward.portfolio_manager import get_portfolio_manager
from backtest.live.market_status import NSE_HOLIDAYS_2026
from backtest.timekeeping import (
    current_session_date,
    format_session_scope,
    ist_session_bounds_utc,
)

logger = logging.getLogger(__name__)

VALID_DIARY_MODES = ("paper", "live")
MAX_CALENDAR_SPAN_DAYS = 400


def validate_diary_mode(raw_mode: Optional[str]) -> str:
    """Validate and normalize ``mode`` strictly to ``'paper'`` or ``'live'``."""
    if raw_mode is None:
        raise ValueError(f"mode must be one of {VALID_DIARY_MODES}, got None")
    cleaned = str(raw_mode).strip().lower()
    if cleaned not in VALID_DIARY_MODES:
        raise ValueError(f"mode must be one of {VALID_DIARY_MODES}, got {raw_mode!r}")
    return cleaned


def _parse_iso_day(raw: str, label: str) -> date:
    try:
        return date.fromisoformat(str(raw).strip()[:10])
    except ValueError as exc:
        raise ValueError(f"Invalid {label} date {raw!r}; expected YYYY-MM-DD") from exc


def parse_ist_date_range(
    from_arg: Optional[str],
    to_arg: Optional[str],
    *,
    default_span: str = "month",
    today_ist: Optional[date] = None,
) -> tuple[date, date]:
    """Resolve inclusive ``(start_date, end_date)`` in IST with span guard."""
    today = today_ist or current_session_date()
    if from_arg:
        start_d = _parse_iso_day(from_arg, "from")
    elif default_span == "today":
        start_d = today
    else:
        start_d = today.replace(day=1)

    if to_arg:
        end_d = _parse_iso_day(to_arg, "to")
    elif from_arg and default_span == "today":
        end_d = start_d
    elif default_span == "today":
        end_d = today
    else:
        _, last_day = calendar.monthrange(start_d.year, start_d.month)
        end_d = start_d.replace(day=last_day)

    if end_d < start_d:
        raise ValueError(
            f"to date {end_d.isoformat()} precedes from date {start_d.isoformat()}"
        )
    if (end_d - start_d).days > MAX_CALENDAR_SPAN_DAYS:
        raise ValueError(
            f"date range exceeds maximum {MAX_CALENDAR_SPAN_DAYS} days"
        )
    return start_d, end_d


def _normalize_strategy_filter(strategy: Optional[str]) -> Optional[str]:
    cleaned = str(strategy or "").strip()
    if not cleaned or cleaned.lower() == "all":
        return None
    return cleaned


class DiaryService:
    """Read-only analytical service backing ``/diary`` and ``/api/diary/*``."""

    def __init__(self, manager: Any = None, db_manager: Any = None) -> None:
        self.mgr = manager if manager is not None else get_portfolio_manager()
        if db_manager is not None:
            self.db = db_manager
        else:
            persister = getattr(self.mgr, "_trade_persister", None)
            self.db = getattr(persister, "db", None)

    # ------------------------------------------------------------------
    # Shared book & calendar loaders
    # ------------------------------------------------------------------

    def load_book_trades(
        self,
        *,
        mode: str,
        start_date: date,
        end_date: date,
        strategy: Optional[str] = None,
    ) -> dict[str, Any]:
        """Load deduped closed trades for ``mode`` inside ``[start_date, end_date]`` IST."""
        book_mode = validate_diary_mode(mode)
        strat_filter = _normalize_strategy_filter(strategy)
        start_utc, end_utc = ist_session_bounds_utc(start_date, end_date)
        start_iso = start_date.isoformat()
        end_iso = end_date.isoformat()

        instances = self.mgr.list_instances(mode=book_mode)
        prefix_to_iid = {
            str(rs.get("instance_id") or "")[:8].lower(): str(rs.get("instance_id") or "")
            for rs in instances
            if rs.get("instance_id")
        }

        strategies_seen: set[str] = {
            str(rs.get("strategy_name")).strip()
            for rs in instances
            if rs.get("strategy_name")
        }
        raw_records: list[dict[str, Any]] = []

        # 1. Persisted DB rows (includes retired/deleted instances).
        if self.db is not None:
            try:
                from backtest.db.models import Portfolio as PortfolioRow
                from backtest.db.models import Trade as TradeRow

                with self.db.session() as session:
                    strat_rows = (
                        session.query(TradeRow.strategy_name)
                        .join(
                            PortfolioRow,
                            TradeRow.portfolio_id == PortfolioRow.portfolio_id,
                        )
                        .filter(
                            PortfolioRow.mode == book_mode,
                            TradeRow.strategy_name.isnot(None),
                        )
                        .distinct()
                        .all()
                    )
                    for (s_name,) in strat_rows:
                        if s_name and str(s_name).strip():
                            strategies_seen.add(str(s_name).strip())

                    q = (
                        session.query(TradeRow, PortfolioRow)
                        .join(
                            PortfolioRow,
                            TradeRow.portfolio_id == PortfolioRow.portfolio_id,
                        )
                        .filter(
                            PortfolioRow.mode == book_mode,
                            TradeRow.exit_time >= start_utc,
                            TradeRow.exit_time < end_utc,
                        )
                    )
                    if strat_filter is not None:
                        q = q.filter(TradeRow.strategy_name == strat_filter)

                    for trade_row, portfolio_row in q.order_by(TradeRow.exit_time.asc()).all():
                        _, prefix = extract_instance_prefix(
                            portfolio_row.name,
                            fallback_id=portfolio_row.portfolio_id,
                        )
                        active_iid = prefix_to_iid.get(prefix or "")
                        raw_records.append(
                            normalize_db_trade_row(
                                trade_row,
                                portfolio_row,
                                active_runner_id=active_iid,
                            )
                        )
            except Exception as exc:  # noqa: BLE001 — fail-soft to in-memory book
                logger.debug("DiaryService DB read skipped: %s", exc)

        # 2. Active in-memory runner closed trades (un-flushed tail + non-DB runs).
        for rs in instances:
            s_name = str(rs.get("strategy_name") or "").strip()
            if strat_filter is not None and s_name != strat_filter:
                continue
            iid = str(rs.get("instance_id") or "")
            runner = self.mgr.get_runner(iid)
            if runner is None:
                continue
            for t in runner.closed_trades:
                norm = normalize_memory_trade_dict(
                    t,
                    instance_id=iid,
                    runner_name=rs.get("name"),
                    strategy_name=s_name,
                    mode=book_mode,
                    portfolio_id=getattr(runner, "_db_portfolio_id", None),
                )
                sdate = norm.get("session_date")
                if not sdate or not (start_iso <= sdate <= end_iso):
                    continue
                raw_records.append(norm)

        trades, duplicates_collapsed = dedupe_normalized_trades(raw_records)
        trades.sort(key=lambda r: (r.get("exit_ts") or "", r.get("symbol") or ""))

        return {
            "mode": book_mode,
            "trades": trades,
            "duplicates_collapsed": duplicates_collapsed,
            "available_strategies": sorted(strategies_seen),
            "active_instances": instances,
        }

    @staticmethod
    def _today_open_snapshot(
        *,
        instances: list[dict[str, Any]],
        strategy: Optional[str] = None,
    ) -> tuple[float, int]:
        """Return ``(running_mtm, open_count)`` across active runners in this book."""
        strat_filter = _normalize_strategy_filter(strategy)
        total_mtm = 0.0
        open_count = 0
        for rs in instances:
            if strat_filter is not None and rs.get("strategy_name") != strat_filter:
                continue
            total_mtm += float(rs.get("open_pnl") or 0.0)
            raw_open = rs.get("open_positions")
            if isinstance(raw_open, int):
                open_count += raw_open
            elif isinstance(raw_open, (list, tuple)):
                open_count += len(raw_open)
        return round(total_mtm, 2), open_count

    def _load_holidays(self, start_date: date, end_date: date) -> dict[str, str]:
        """Return ``{iso_date: holiday_name}`` combining seeded 2026 + DB holidays."""
        out: dict[str, str] = {}
        start_iso = start_date.isoformat()
        end_iso = end_date.isoformat()
        for h_iso, seg, desc in NSE_HOLIDAYS_2026:
            if seg == "equity" and start_iso <= h_iso <= end_iso:
                out[h_iso] = desc

        if self.db is not None:
            try:
                from backtest.db.models import MarketHoliday

                with self.db.session() as session:
                    rows = (
                        session.query(MarketHoliday)
                        .filter(
                            MarketHoliday.holiday_date >= start_date,
                            MarketHoliday.holiday_date <= end_date,
                            MarketHoliday.is_trading_holiday.is_(True),
                        )
                        .all()
                    )
                    for row in rows:
                        out[row.holiday_date.isoformat()] = str(row.description or "Holiday")
            except Exception:  # noqa: BLE001 — optional table on unmigrated SQLite
                pass
        return out

    @staticmethod
    def _ledger_health(duplicates_collapsed: int, mismatch_count: int) -> dict[str, Any]:
        if mismatch_count > 0:
            status = "mismatch_warning"
        elif duplicates_collapsed > 0:
            status = "deduped_legacy"
        else:
            status = "verified"
        return {
            "status": status,
            "duplicates_collapsed": int(duplicates_collapsed),
            "mismatch_count": int(mismatch_count),
        }

    # ------------------------------------------------------------------
    # Public Slice 1 Views: Calendar Heatmap + Executive Summary
    # ------------------------------------------------------------------

    def get_calendar(
        self,
        *,
        mode: str = "paper",
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        strategy: Optional[str] = None,
        today_ist: Optional[date] = None,
    ) -> dict[str, Any]:
        """Per-IST-session calendar heatmap + weekly rollups for ``[from, to]``."""
        book_mode = validate_diary_mode(mode)
        resolved_today = today_ist or current_session_date()
        start_d, end_d = parse_ist_date_range(
            from_date, to_date, default_span="month", today_ist=resolved_today
        )
        strat_filter = _normalize_strategy_filter(strategy)

        loaded = self.load_book_trades(
            mode=book_mode,
            start_date=start_d,
            end_date=end_d,
            strategy=strat_filter,
        )
        trades = loaded["trades"]
        holidays = self._load_holidays(start_d, end_d)
        today_iso = resolved_today.isoformat()
        running_today, open_today = self._today_open_snapshot(
            instances=loaded["active_instances"],
            strategy=strat_filter,
        )

        by_day: dict[str, list[dict[str, Any]]] = {}
        for t in trades:
            sdate = t.get("session_date")
            if sdate:
                by_day.setdefault(sdate, []).append(t)

        days: list[dict[str, Any]] = []
        cursor = start_d
        total_mismatches = 0
        max_abs_net = 0.0

        while cursor <= end_d:
            d_iso = cursor.isoformat()
            d_trades = by_day.get(d_iso, [])
            gross = round(sum(float(t["gross_pnl"]) for t in d_trades), 2)
            comm = round(sum(float(t["commission_total"]) for t in d_trades), 2)
            net = round(sum(float(t["net_pnl"]) for t in d_trades), 2)
            t_count = len(d_trades)
            wins = sum(1 for t in d_trades if float(t["net_pnl"]) > 0)
            losses = sum(1 for t in d_trades if float(t["net_pnl"]) < 0)
            flats = t_count - wins - losses
            mismatches = sum(1 for t in d_trades if not t.get("reconcile_ok", True))
            total_mismatches += mismatches

            strats_on_day = {
                str(t.get("strategy")) for t in d_trades if t.get("strategy")
            }
            inst_on_day = {
                str(t.get("instance_prefix") or t.get("portfolio_id") or t.get("runner_id"))
                for t in d_trades
            }

            if t_count > 0 and abs(net) > max_abs_net:
                max_abs_net = abs(net)

            weekday = cursor.weekday()  # 0=Mon .. 6=Sun
            h_name = holidays.get(d_iso)
            is_today = cursor == resolved_today

            days.append(
                {
                    "date": d_iso,
                    "weekday": weekday,
                    "is_weekend": weekday >= 5,
                    "is_holiday": h_name is not None,
                    "holiday_name": h_name,
                    "is_today": is_today,
                    "is_future": cursor > resolved_today,
                    "trade_count": t_count,
                    "win_count": wins,
                    "loss_count": losses,
                    "flat_count": flats,
                    "net_win_rate": round(wins / t_count, 4) if t_count else None,
                    "win_rate_pct": round((wins / t_count) * 100.0, 2) if t_count else 0.0,
                    "gross_pnl": gross,
                    "commission": comm,
                    "net_pnl": net,
                    "running_mtm": running_today if is_today else None,
                    "open_count": open_today if is_today else 0,
                    "active_strategies": len(strats_on_day),
                    "active_instances": len(inst_on_day),
                    "mismatch_count": mismatches,
                    "reconcile_anomalies": mismatches,
                }
            )
            cursor += timedelta(days=1)

        weeks = self._rollup_weeks(days)

        return {
            "mode": book_mode,
            "from_date": start_d.isoformat(),
            "to_date": end_d.isoformat(),
            "today_ist": today_iso,
            "selected_strategy": strat_filter or "all",
            "available_strategies": loaded["available_strategies"],
            "max_abs_daily_net": round(max_abs_net, 2),
            "days": days,
            "weeks": weeks,
            "reconcile_anomalies": total_mismatches,
            "ledger_health": self._ledger_health(
                loaded["duplicates_collapsed"], total_mismatches
            ),
        }

    def get_summary(
        self,
        *,
        mode: str = "paper",
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        strategy: Optional[str] = None,
        today_ist: Optional[date] = None,
    ) -> dict[str, Any]:
        """Executive summary strip for a single IST day or inclusive date range."""
        book_mode = validate_diary_mode(mode)
        resolved_today = today_ist or current_session_date()
        start_d, end_d = parse_ist_date_range(
            from_date, to_date, default_span="today", today_ist=resolved_today
        )
        strat_filter = _normalize_strategy_filter(strategy)

        loaded = self.load_book_trades(
            mode=book_mode,
            start_date=start_d,
            end_date=end_d,
            strategy=strat_filter,
        )
        trades = loaded["trades"]

        gross = round(sum(float(t["gross_pnl"]) for t in trades), 2)
        comm = round(sum(float(t["commission_total"]) for t in trades), 2)
        realized_net = round(sum(float(t["net_pnl"]) for t in trades), 2)
        t_count = len(trades)
        win_nets = [float(t["net_pnl"]) for t in trades if float(t["net_pnl"]) > 0]
        loss_nets = [float(t["net_pnl"]) for t in trades if float(t["net_pnl"]) < 0]
        wins = len(win_nets)
        losses = len(loss_nets)
        flats = t_count - wins - losses
        mismatches = sum(1 for t in trades if not t.get("reconcile_ok", True))

        win_sum = sum(win_nets)
        loss_abs_sum = abs(sum(loss_nets))
        profit_factor: Optional[float] = None
        if loss_abs_sum > 0:
            profit_factor = round(win_sum / loss_abs_sum, 2)

        comm_drag_pct: Optional[float] = (
            round((comm / gross) * 100.0, 2) if gross > 0.005 else None
        )
        avg_win_net = round(win_sum / wins, 2) if wins else 0.0
        avg_loss_net = round(sum(loss_nets) / losses, 2) if losses else 0.0
        all_nets = [float(t["net_pnl"]) for t in trades]
        best_trade_net = round(max(all_nets), 2) if all_nets else 0.0
        worst_trade_net = round(min(all_nets), 2) if all_nets else 0.0

        includes_today = start_d <= resolved_today <= end_d
        is_single_day = start_d == end_d
        running_mtm: Optional[float] = None
        open_count = 0
        day_start_anchor: Optional[float] = None

        if includes_today:
            running_mtm, open_count = self._today_open_snapshot(
                instances=loaded["active_instances"],
                strategy=strat_filter,
            )
            if is_single_day and strat_filter is None and hasattr(
                self.mgr, "get_bucket_aggregates"
            ):
                buckets = self.mgr.get_bucket_aggregates()
                b = buckets.get(book_mode) or {}
                anchor = float(
                    getattr(self.mgr, "_bucket_day_start", {}).get(book_mode, 0.0)
                    or b.get("capital")
                    or 0.0
                )
                day_start_anchor = round(anchor, 2) if anchor > 0 else None

        combined_pnl = (
            round(realized_net + running_mtm, 2)
            if running_mtm is not None
            else realized_net
        )

        # Daily extremes & active trading-day streak inside [start_d, end_d]
        by_day_net: dict[str, float] = {}
        for t in trades:
            sd = t.get("session_date")
            if sd:
                by_day_net[sd] = round(by_day_net.get(sd, 0.0) + float(t["net_pnl"]), 2)

        active_days_sorted = sorted(by_day_net.items(), key=lambda kv: kv[0])
        best_day = (
            {
                "date": max(active_days_sorted, key=lambda kv: kv[1])[0],
                "net_pnl": max(active_days_sorted, key=lambda kv: kv[1])[1],
            }
            if active_days_sorted
            else None
        )
        worst_day = (
            {
                "date": min(active_days_sorted, key=lambda kv: kv[1])[0],
                "net_pnl": min(active_days_sorted, key=lambda kv: kv[1])[1],
            }
            if active_days_sorted
            else None
        )
        streak = self._compute_streak(active_days_sorted)

        if is_single_day:
            scope_label = format_session_scope(start_d, reference_now=None)
            if today_ist is not None:
                scope_label = (
                    f"Today · {start_d:%d-%b} IST"
                    if start_d == resolved_today
                    else f"{start_d:%d-%b-%Y} IST"
                )
        else:
            scope_label = (
                f"{start_d:%d-%b} → {end_d:%d-%b-%Y} IST "
                f"({len(active_days_sorted)} active)"
            )

        return {
            "mode": book_mode,
            "from_date": start_d.isoformat(),
            "to_date": end_d.isoformat(),
            "today_ist": resolved_today.isoformat(),
            "selected_strategy": strat_filter or "all",
            "available_strategies": loaded["available_strategies"],
            "is_single_day": is_single_day,
            "includes_today": includes_today,
            "scope_label": scope_label,
            "active_sessions": len(active_days_sorted),
            "active_days": len(active_days_sorted),
            "day_start_anchor": day_start_anchor,
            "gross_pnl": gross,
            "commission": comm,
            "net_pnl": realized_net,
            "realized_net_pnl": realized_net,
            "commission_drag_pct": comm_drag_pct,
            "running_mtm": running_mtm,
            "open_count": open_count,
            "total_pnl_with_mtm": combined_pnl,
            "combined_session_pnl": combined_pnl,
            "trade_count": t_count,
            "win_count": wins,
            "loss_count": losses,
            "flat_count": flats,
            "unbounded_pf": bool(loss_abs_sum == 0 and win_sum > 0),
            "profit_factor": profit_factor,
            "net_win_rate": round(wins / t_count, 4) if t_count else 0.0,
            "win_rate_pct": round((wins / t_count) * 100.0, 2) if t_count else 0.0,
            "expectancy_net": round(realized_net / t_count, 2) if t_count else 0.0,
            "avg_win_net": avg_win_net,
            "avg_loss_net": avg_loss_net,
            "best_trade_net": best_trade_net,
            "worst_trade_net": worst_trade_net,
            "best_day": best_day,
            "worst_day": worst_day,
            "current_streak": streak,
            "streak": {"type": streak["kind"], "count": streak["days"]},
            "reconcile_anomalies": mismatches,
            "ledger_health": self._ledger_health(
                loaded["duplicates_collapsed"], mismatches
            ),
        }

    # ------------------------------------------------------------------
    # Public Slice 2 Views: 3-Level Strategy → Instance → Trade Tree
    # ------------------------------------------------------------------

    def get_attribution(
        self,
        *,
        mode: str = "paper",
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        strategy: Optional[str] = None,
        today_ist: Optional[date] = None,
        inline_trade_limit: int = DEFAULT_INLINE_TRADE_LIMIT,
    ) -> dict[str, Any]:
        """Return 3-level Strategy → Spawned Instance → Trade/Structure tree."""
        book_mode = validate_diary_mode(mode)
        resolved_today = today_ist or current_session_date()
        start_d, end_d = parse_ist_date_range(
            from_date, to_date, default_span="today", today_ist=resolved_today
        )
        strat_filter = _normalize_strategy_filter(strategy)

        loaded = self.load_book_trades(
            mode=book_mode,
            start_date=start_d,
            end_date=end_d,
            strategy=strat_filter,
        )
        tree = build_attribution_tree(
            book_mode=book_mode,
            start_d=start_d,
            end_d=end_d,
            today_ist=resolved_today,
            selected_strategy=strat_filter,
            available_strategies=loaded["available_strategies"],
            trades=loaded["trades"],
            active_instances=loaded["active_instances"],
            runner_lookup=getattr(self.mgr, "get_runner", None),
            duplicates_collapsed=loaded["duplicates_collapsed"],
            inline_trade_limit=inline_trade_limit,
        )
        tree["ledger_health"] = self._ledger_health(
            loaded["duplicates_collapsed"],
            tree["totals"]["reconcile_anomalies"],
        )
        return tree

    def get_instance_trades(
        self,
        instance_key: str,
        *,
        mode: str = "paper",
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        today_ist: Optional[date] = None,
    ) -> dict[str, Any]:
        """Return full un-truncated Level-3 trades + open structures for one instance."""
        target = str(instance_key or "").strip()
        if not target:
            raise ValueError("instance_key is required")

        tree = self.get_attribution(
            mode=mode,
            from_date=from_date,
            to_date=to_date,
            today_ist=today_ist,
            inline_trade_limit=100_000,
        )
        target_low = target.lower()
        for strat in tree["strategies"]:
            for inst in strat["instances"]:
                candidates = {
                    str(inst.get("instance_key") or "").lower(),
                    str(inst.get("instance_id") or "").lower(),
                    str(inst.get("instance_prefix") or "").lower(),
                    str(inst.get("portfolio_id") or "").lower(),
                }
                candidates.discard("")
                if target_low in candidates:
                    return {
                        "mode": tree["mode"],
                        "from_date": tree["from_date"],
                        "to_date": tree["to_date"],
                        "today_ist": tree["today_ist"],
                        "includes_today": tree["includes_today"],
                        "instance": inst,
                    }
        raise KeyError(f"Spawned instance {instance_key!r} not found in {tree['mode']} window")

    # ------------------------------------------------------------------
    # Public Slice 3 Views: Exit-Reason & NSE IST Session Regimes
    # ------------------------------------------------------------------

    def get_edge_regimes(
        self,
        *,
        mode: str = "paper",
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        strategy: Optional[str] = None,
        clock: str = "entry",
        group_by: str = "strategy",
        today_ist: Optional[date] = None,
    ) -> dict[str, Any]:
        """Return Exit-Reason & NSE Intraday Session Regime 2D heat matrices."""
        book_mode = validate_diary_mode(mode)
        norm_clock = validate_regime_clock(clock)
        norm_group = validate_regime_group_by(group_by)
        resolved_today = today_ist or current_session_date()
        start_d, end_d = parse_ist_date_range(
            from_date, to_date, default_span="today", today_ist=resolved_today
        )
        strat_filter = _normalize_strategy_filter(strategy)

        loaded = self.load_book_trades(
            mode=book_mode,
            start_date=start_d,
            end_date=end_d,
            strategy=strat_filter,
        )
        trades = loaded["trades"]
        mismatches = sum(1 for t in trades if not t.get("reconcile_ok", True))
        payload = build_regime_matrices(
            book_mode=book_mode,
            start_d=start_d,
            end_d=end_d,
            today_ist=resolved_today,
            selected_strategy=strat_filter,
            available_strategies=loaded["available_strategies"],
            trades=trades,
            clock=norm_clock,
            group_by=norm_group,
            duplicates_collapsed=loaded["duplicates_collapsed"],
        )
        payload["ledger_health"] = self._ledger_health(
            loaded["duplicates_collapsed"], mismatches
        )
        return payload

    @staticmethod
    def _rollup_weeks(days: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Group daily cells into Monday-anchored calendar weeks."""
        buckets: dict[str, list[dict[str, Any]]] = {}
        order: list[str] = []
        for d in days:
            dt = date.fromisoformat(d["date"])
            monday = (dt - timedelta(days=dt.weekday())).isoformat()
            if monday not in buckets:
                buckets[monday] = []
                order.append(monday)
            buckets[monday].append(d)

        weeks: list[dict[str, Any]] = []
        for monday in order:
            w_days = buckets[monday]
            t_count = sum(x["trade_count"] for x in w_days)
            wins = sum(x["win_count"] for x in w_days)
            gross = round(sum(x["gross_pnl"] for x in w_days), 2)
            comm = round(sum(x["commission"] for x in w_days), 2)
            net = round(sum(x["net_pnl"] for x in w_days), 2)
            active = sum(1 for x in w_days if x["trade_count"] > 0)
            start_iso = w_days[0]["date"]
            end_iso = w_days[-1]["date"]
            weeks.append(
                {
                    "week_monday": monday,
                    "week_start": start_iso,
                    "week_end": end_iso,
                    "start_date": start_iso,
                    "end_date": end_iso,
                    "active_days": active,
                    "trade_count": t_count,
                    "gross_pnl": gross,
                    "commission": comm,
                    "net_pnl": net,
                    "net_win_rate": round(wins / t_count, 4) if t_count else None,
                    "win_rate_pct": round((wins / t_count) * 100.0, 2) if t_count else 0.0,
                }
            )
        return weeks

    @staticmethod
    def _compute_streak(active_days_sorted: list[tuple[str, float]]) -> dict[str, Any]:
        if not active_days_sorted:
            return {"kind": "flat", "days": 0}
        last_pnl = active_days_sorted[-1][1]
        if last_pnl > 0:
            kind = "win"
        elif last_pnl < 0:
            kind = "loss"
        else:
            return {"kind": "flat", "days": 1}

        count = 0
        for _, net_val in reversed(active_days_sorted):
            if (kind == "win" and net_val > 0) or (kind == "loss" and net_val < 0):
                count += 1
            else:
                break
        return {"kind": kind, "days": count}
