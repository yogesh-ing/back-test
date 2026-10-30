"""Slice 3 — Institutional Edge Diagnostics I: Exit-Reason & NSE IST Regimes.

Computes two reconciled 2D attribution matrices over deduplicated book trades
plus deterministic quantitative diagnostic callouts:

* **Matrix A (Exit-Reason Attribution):** ``Row (Strategy | Instance) ×
  Canonical Exit Reason`` (``take_profit``, ``stop_loss``, ``trailing_stop``,
  ``eod_flat``, ``time_stop``, ``signal``, ``risk_limit``, ``manual``, plus
  ``unspecified`` only when unlabeled legacy rows exist).
* **Matrix B (NSE Intraday Session Regime):** ``Row (Strategy | Instance) ×
  IST Session Window`` (``09:15–09:45``, ``09:45–11:30``, ``11:30–13:30``,
  ``13:30–15:10``, ``15:10–15:30``, plus ``off_hours`` only when trades occur
  outside regular cash/F&O hours), toggleable between ``clock='entry'`` and
  ``clock='exit'`` in ``Asia/Kolkata`` (``+05:30``).

Every cell carries exact 2dp ``gross_pnl``, single combined ``commission``
(zero sub-component fee split per E2), ``net_pnl``, ``avg_net_pnl``,
``trade_count``, and ``win_rate_pct``, and both matrices reconcile to the
paisa against the window grand total.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Mapping, Optional

from backtest.diary.attribution import make_instance_key
from backtest.timekeeping import from_stored_utc, to_exchange

VALID_REGIME_CLOCKS = ("entry", "exit")
VALID_REGIME_GROUPINGS = ("strategy", "instance")

CANONICAL_EXIT_BUCKETS: tuple[tuple[str, str], ...] = (
    ("take_profit", "Take Profit"),
    ("stop_loss", "Stop Loss"),
    ("trailing_stop", "Trailing Stop"),
    ("eod_flat", "EOD / Square-Off"),
    ("time_stop", "Time Stop"),
    ("signal", "Signal Flip"),
    ("risk_limit", "Risk / Breaker"),
    ("manual", "Manual Close"),
)

_CANONICAL_EXIT_SET = frozenset(k for k, _ in CANONICAL_EXIT_BUCKETS)

# NSE regular session windows expressed in minutes since 00:00 IST:
#   09:15 = 555, 09:45 = 585, 11:30 = 690, 13:30 = 810, 15:10 = 910, 15:30 = 930
NSE_SESSION_REGIMES: tuple[tuple[str, str, str, int, int], ...] = (
    ("opening_drive", "09:15–09:45", "Opening Drive", 9 * 60 + 15, 9 * 60 + 45),
    ("morning_trend", "09:45–11:30", "Morning Trend", 9 * 60 + 45, 11 * 60 + 30),
    ("midday_chop", "11:30–13:30", "Midday Chop", 11 * 60 + 30, 13 * 60 + 30),
    ("afternoon_gamma", "13:30–15:10", "Afternoon / Gamma", 13 * 60 + 30, 15 * 60 + 10),
    ("closing_squareoff", "15:10–15:30", "Square-Off Window", 15 * 60 + 10, 15 * 60 + 31),
)


def validate_regime_clock(raw_clock: Optional[str]) -> str:
    cleaned = str(raw_clock or "entry").strip().lower()
    if cleaned not in VALID_REGIME_CLOCKS:
        raise ValueError(
            f"clock must be one of {VALID_REGIME_CLOCKS}, got {raw_clock!r}"
        )
    return cleaned


def validate_regime_group_by(raw_group_by: Optional[str]) -> str:
    cleaned = str(raw_group_by or "strategy").strip().lower()
    if cleaned not in VALID_REGIME_GROUPINGS:
        raise ValueError(
            f"group_by must be one of {VALID_REGIME_GROUPINGS}, got {raw_group_by!r}"
        )
    return cleaned


def canonical_exit_reason(raw: Any) -> str:
    """Normalize persisted or in-memory ``exit_reason`` into a canonical bucket."""
    r = str(raw or "").strip().lower()
    if not r or r in ("none", "null", "unspecified", "unknown"):
        return "unspecified"
    if r in _CANONICAL_EXIT_SET:
        return r

    # Multi-word & specific tokens checked BEFORE generic substrings like 'stop'
    if (
        "squareoff" in r
        or "square_off" in r
        or "square off" in r
        or "expiry" in r
        or "settle" in r
        or r == "eod"
        or "eod_" in r
        or "_eod" in r
    ):
        return "eod_flat"
    if "trail" in r:
        return "trailing_stop"
    if "time_stop" in r or "max_bars" in r or "time" in r:
        return "time_stop"
    if "take_profit" in r or "target" in r or "profit" in r or r == "tp":
        return "take_profit"
    if (
        "risk" in r
        or "breaker" in r
        or "emergency" in r
        or "max_loss" in r
        or "kill" in r
    ):
        return "risk_limit"
    if "stop" in r or r == "sl" or "sl_" in r or "_sl" in r:
        return "stop_loss"
    if "manual" in r or "flatten" in r:
        return "manual"
    return "signal"


def classify_ist_session_regime(raw_ts: Any, fallback_ts: Any = None) -> str:
    """Map an aware UTC/ISO timestamp onto an NSE IST intraday session bucket."""
    stamp = raw_ts if raw_ts not in (None, "") else fallback_ts
    if stamp in (None, ""):
        return "off_hours"
    try:
        dt_utc = from_stored_utc(stamp)
        if dt_utc is None:
            return "off_hours"
        ist_dt = to_exchange(dt_utc)
        if ist_dt is None:
            return "off_hours"
    except Exception:  # noqa: BLE001
        return "off_hours"

    minute_of_day = ist_dt.hour * 60 + ist_dt.minute
    # Note: closing_squareoff includes 15:30:xx up to 15:30:59 (end_min = 931)
    for key, _span, _title, start_min, end_min in NSE_SESSION_REGIMES:
        if start_min <= minute_of_day < end_min:
            return key
    return "off_hours"


def _cell_metrics(trades: list[dict[str, Any]]) -> dict[str, Any]:
    t_count = len(trades)
    if t_count == 0:
        return {
            "trade_count": 0,
            "win_count": 0,
            "loss_count": 0,
            "net_win_rate": None,
            "win_rate_pct": 0.0,
            "gross_pnl": 0.0,
            "commission": 0.0,
            "net_pnl": 0.0,
            "avg_net_pnl": 0.0,
            "expectancy_net": 0.0,
        }
    gross = round(sum(float(t["gross_pnl"]) for t in trades), 2)
    comm = round(sum(float(t["commission_total"]) for t in trades), 2)
    net = round(sum(float(t["net_pnl"]) for t in trades), 2)
    wins = sum(1 for t in trades if float(t["net_pnl"]) > 0)
    losses = sum(1 for t in trades if float(t["net_pnl"]) < 0)
    avg_net = round(net / t_count, 2)
    return {
        "trade_count": t_count,
        "win_count": wins,
        "loss_count": losses,
        "net_win_rate": round(wins / t_count, 4),
        "win_rate_pct": round((wins / t_count) * 100.0, 2),
        "gross_pnl": gross,
        "commission": comm,
        "net_pnl": net,
        "avg_net_pnl": avg_net,
        "expectancy_net": avg_net,
    }


def _row_identity(trade: Mapping[str, Any], group_by: str) -> tuple[str, str, str]:
    """Return ``(row_key, row_label, strategy_name)`` for a trade."""
    strat = str(trade.get("strategy") or "unassigned").strip() or "unassigned"
    if group_by == "instance":
        prefix = trade.get("instance_prefix")
        pid = trade.get("portfolio_id")
        rid = trade.get("runner_id")
        rname = str(trade.get("runner_name") or strat).strip()
        ikey = make_instance_key(
            strat,
            instance_prefix=prefix,
            portfolio_id=pid,
            runner_id=rid,
            runner_name=rname,
        )
        disp = f"{rname} [{prefix}]" if prefix else rname
        return ikey, disp, strat
    return strat, strat, strat


def _build_2d_matrix(
    *,
    trades: list[dict[str, Any]],
    columns: list[dict[str, str]],
    col_classifier: Any,
    group_by: str,
) -> dict[str, Any]:
    col_keys = [c["key"] for c in columns]
    row_meta: dict[str, tuple[str, str]] = {}
    row_trades: dict[str, list[dict[str, Any]]] = {}
    cell_trades: dict[tuple[str, str], list[dict[str, Any]]] = {}
    col_buckets: dict[str, list[dict[str, Any]]] = {k: [] for k in col_keys}

    for t in trades:
        r_key, r_label, s_name = _row_identity(t, group_by)
        c_key = col_classifier(t)
        row_meta[r_key] = (r_label, s_name)
        row_trades.setdefault(r_key, []).append(t)
        cell_trades.setdefault((r_key, c_key), []).append(t)
        col_buckets.setdefault(c_key, []).append(t)

    max_abs_cell_net = 0.0
    rows_out: list[dict[str, Any]] = []
    for r_key, r_list in row_trades.items():
        r_label, s_name = row_meta[r_key]
        cells: dict[str, dict[str, Any]] = {}
        for c_key in col_keys:
            cm = _cell_metrics(cell_trades.get((r_key, c_key), []))
            if cm["trade_count"] > 0 and abs(cm["net_pnl"]) > max_abs_cell_net:
                max_abs_cell_net = abs(cm["net_pnl"])
            cells[c_key] = cm
        r_tot = _cell_metrics(r_list)
        rows_out.append(
            {
                "row_key": r_key,
                "row_label": r_label,
                "strategy_name": s_name,
                "totals": r_tot,
                "cells": cells,
            }
        )

    # Sort rows worst net_pnl first so primary bleeders surface at the top
    rows_out.sort(key=lambda r: (r["totals"]["net_pnl"], -(r["totals"]["trade_count"])))

    column_totals = {c_key: _cell_metrics(col_buckets.get(c_key, [])) for c_key in col_keys}
    grand_totals = _cell_metrics(trades)

    return {
        "columns": columns,
        "rows": rows_out,
        "column_totals": column_totals,
        "grand_totals": grand_totals,
        "max_abs_cell_net": round(max_abs_cell_net, 2),
    }


def _fmt_inr(val: float) -> str:
    sign = "+" if val > 0.005 else ("−" if val < -0.005 else "")
    return f"{sign}₹{abs(val):,.2f}"


def generate_regime_callouts(
    *,
    exit_matrix: Mapping[str, Any],
    session_matrix: Mapping[str, Any],
    clock: str,
) -> list[dict[str, Any]]:
    """Deterministic quantitative diagnostics (zero LLM fluff)."""
    callouts: list[dict[str, Any]] = []

    # 1. TP Net-Negative Alert (Gross > 0, Net < 0 on take_profit exits)
    for row in exit_matrix.get("rows", []):
        tp = (row.get("cells") or {}).get("take_profit") or {}
        if (
            tp.get("trade_count", 0) >= 2
            and float(tp.get("gross_pnl", 0.0)) > 0.0
            and float(tp.get("net_pnl", 0.0)) < 0.0
        ):
            callouts.append(
                {
                    "code": "TP_NET_NEGATIVE",
                    "severity": "critical",
                    "scope": row["row_label"],
                    "title": f"TP Inside Fee Floor — {row['row_label']}",
                    "detail": (
                        f"{tp['trade_count']} take_profit exits booked "
                        f"{_fmt_inr(tp['gross_pnl'])} gross but {_fmt_inr(tp['net_pnl'])} net "
                        f"after ₹{tp['commission']:,.2f} commission "
                        f"(avg {_fmt_inr(tp['avg_net_pnl'])}/trade). Widen target above cost floor."
                    ),
                }
            )

    # 2. Stop-Loss Dominance (>= 60% of row's trades stopped out with net loss)
    for row in exit_matrix.get("rows", []):
        r_tot = row.get("totals") or {}
        sl = (row.get("cells") or {}).get("stop_loss") or {}
        n_all = int(r_tot.get("trade_count", 0))
        n_sl = int(sl.get("trade_count", 0))
        if (
            n_all >= 3
            and float(r_tot.get("net_pnl", 0.0)) < 0.0
            and n_sl / n_all >= 0.60
            and float(sl.get("net_pnl", 0.0)) < 0.0
        ):
            pct = round((n_sl / n_all) * 100.0)
            callouts.append(
                {
                    "code": "STOP_LOSS_DOMINANCE",
                    "severity": "critical" if pct >= 80 else "warning",
                    "scope": row["row_label"],
                    "title": f"Stop-Loss Cascade ({pct}% of exits) — {row['row_label']}",
                    "detail": (
                        f"{n_sl} of {n_all} closed trades exited via stop_loss for "
                        f"{_fmt_inr(sl['net_pnl'])} net (₹{sl['commission']:,.2f} commission). "
                        f"Evaluate stop width vs bar ATR or entry regime filter."
                    ),
                }
            )

    # 3. EOD / Square-Off Bleed
    eod_tot = (exit_matrix.get("column_totals") or {}).get("eod_flat") or {}
    grand_net = float((exit_matrix.get("grand_totals") or {}).get("net_pnl", 0.0))
    if eod_tot.get("trade_count", 0) >= 2 and float(eod_tot.get("net_pnl", 0.0)) < 0.0:
        eod_net = float(eod_tot["net_pnl"])
        if grand_net < 0 and abs(eod_net) >= 0.35 * abs(grand_net):
            share = round((abs(eod_net) / abs(grand_net)) * 100.0)
            callouts.append(
                {
                    "code": "EOD_SQUAREOFF_BLEED",
                    "severity": "warning",
                    "scope": "Book",
                    "title": f"EOD Square-Off Bleed ({share}% of net drawdown)",
                    "detail": (
                        f"{eod_tot['trade_count']} eod_flat exits lost {_fmt_inr(eod_net)} net. "
                        f"Late entries are running out of session runway before 15:15 IST."
                    ),
                }
            )

    # 4. Midday Chop Bleed (11:30–13:30 IST) & Opening Whipsaw (09:15–09:45 IST)
    clock_word = "entries" if clock == "entry" else "exits"
    for row in session_matrix.get("rows", []):
        cells = row.get("cells") or {}
        mid = cells.get("midday_chop") or {}
        if (
            mid.get("trade_count", 0) >= 3
            and float(mid.get("net_pnl", 0.0)) < 0.0
            and float(mid.get("win_rate_pct", 0.0)) <= 35.0
        ):
            callouts.append(
                {
                    "code": "MIDDAY_CHOP_BLEED",
                    "severity": "warning",
                    "scope": row["row_label"],
                    "title": f"Midday Chop Bleed (11:30–13:30 IST) — {row['row_label']}",
                    "detail": (
                        f"{mid['trade_count']} {clock_word} in 11:30–13:30 IST posted "
                        f"{mid['win_rate_pct']:.1f}% net win rate for {_fmt_inr(mid['net_pnl'])}. "
                        f"Consider a midday lull blackout gate."
                    ),
                }
            )

        opn = cells.get("opening_drive") or {}
        if (
            opn.get("trade_count", 0) >= 3
            and float(opn.get("net_pnl", 0.0)) < 0.0
            and float(opn.get("win_rate_pct", 0.0)) <= 35.0
        ):
            callouts.append(
                {
                    "code": "OPENING_WHIPSAW_BLEED",
                    "severity": "warning",
                    "scope": row["row_label"],
                    "title": f"Opening Drive Whipsaw (09:15–09:45 IST) — {row['row_label']}",
                    "detail": (
                        f"{opn['trade_count']} {clock_word} in 09:15–09:45 IST posted "
                        f"{opn['win_rate_pct']:.1f}% net win rate for {_fmt_inr(opn['net_pnl'])}. "
                        f"Test delaying entries until opening range settles."
                    ),
                }
            )

        # 5. Highest-Edge Session Regime Sweet Spot
        best_col = None
        best_net = 0.0
        for col_def in session_matrix.get("columns", []):
            ck = col_def["key"]
            cell = cells.get(ck) or {}
            if (
                cell.get("trade_count", 0) >= 3
                and float(cell.get("net_pnl", 0.0)) > best_net
                and float(cell.get("win_rate_pct", 0.0)) >= 60.0
            ):
                best_net = float(cell["net_pnl"])
                best_col = (col_def, cell)
        if best_col is not None:
            c_def, c_cell = best_col
            callouts.append(
                {
                    "code": "REGIME_SWEET_SPOT",
                    "severity": "positive",
                    "scope": row["row_label"],
                    "title": f"Regime Sweet Spot ({c_def['label']} IST) — {row['row_label']}",
                    "detail": (
                        f"{c_cell['trade_count']} {clock_word} in {c_def['label']} IST "
                        f"({c_def.get('subtitle', '')}) delivered {c_cell['win_rate_pct']:.1f}% "
                        f"win rate and {_fmt_inr(c_cell['net_pnl'])} net "
                        f"(expectancy {_fmt_inr(c_cell['expectancy_net'])}/trade)."
                    ),
                }
            )

    return callouts


def build_regime_matrices(
    *,
    book_mode: str,
    start_d: date,
    end_d: date,
    today_ist: date,
    selected_strategy: Optional[str],
    available_strategies: list[str],
    trades: list[dict[str, Any]],
    clock: str = "entry",
    group_by: str = "strategy",
    duplicates_collapsed: int = 0,
) -> dict[str, Any]:
    """Assemble Matrix A (Exit Reasons), Matrix B (NSE IST Windows) & Callouts."""
    norm_clock = validate_regime_clock(clock)
    norm_group = validate_regime_group_by(group_by)

    exit_cols = [{"key": k, "label": lbl} for k, lbl in CANONICAL_EXIT_BUCKETS]
    if any(canonical_exit_reason(t.get("exit_reason")) == "unspecified" for t in trades):
        exit_cols.append({"key": "unspecified", "label": "Unspecified"})

    exit_matrix = _build_2d_matrix(
        trades=trades,
        columns=exit_cols,
        col_classifier=lambda t: canonical_exit_reason(t.get("exit_reason")),
        group_by=norm_group,
    )

    session_cols = [
        {"key": k, "label": span, "subtitle": title}
        for k, span, title, _s, _e in NSE_SESSION_REGIMES
    ]

    def _session_bucket_for_trade(t: Mapping[str, Any]) -> str:
        if norm_clock == "entry":
            return classify_ist_session_regime(t.get("entry_ts"), t.get("exit_ts"))
        return classify_ist_session_regime(t.get("exit_ts"), t.get("entry_ts"))

    if any(_session_bucket_for_trade(t) == "off_hours" for t in trades):
        session_cols.append(
            {"key": "off_hours", "label": "Off-Hours", "subtitle": "Pre/Post / Special"}
        )

    session_matrix = _build_2d_matrix(
        trades=trades,
        columns=session_cols,
        col_classifier=_session_bucket_for_trade,
        group_by=norm_group,
    )

    callouts = generate_regime_callouts(
        exit_matrix=exit_matrix,
        session_matrix=session_matrix,
        clock=norm_clock,
    )

    return {
        "mode": book_mode,
        "from_date": start_d.isoformat(),
        "to_date": end_d.isoformat(),
        "today_ist": today_ist.isoformat(),
        "selected_strategy": selected_strategy or "all",
        "available_strategies": available_strategies,
        "clock": norm_clock,
        "group_by": norm_group,
        "exit_matrix": exit_matrix,
        "session_matrix": session_matrix,
        "callouts": callouts,
        "duplicates_collapsed": int(duplicates_collapsed),
    }
