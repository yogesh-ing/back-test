"""Slice 4 — Institutional Edge Diagnostics II: Hold-Duration Drag & Payoff Quadrant.

Computes:
1. **Hold-Duration & Friction Breakdown:** exact ``(exit_time - entry_time)``
   buckets (``<1m`` Churn Watch, ``1–5m`` Scalp, ``5–15m``, ``15–60m``, ``60m+``,
   plus ``unknown_hold`` overflow if any legacy row lacks ``entry_time``),
   single-sum ``commission`` (E2), ``avg_commission_per_trade`` (fee breakeven
   hurdle), and ``commission_drag_pct`` / ``drag_band`` (``healthy | warning |
   critical``).
2. **Expectancy & Payoff Breakeven Quadrant:** per-Spawned-Instance (or
   per-Strategy) ``net_win_rate`` ($W$), ``payoff_ratio``
   ($R = \\overline{W}_{\\text{net}} / |\\overline{L}_{\\text{net}}|$), exact
   hyperbolic breakeven hurdle $R_{\\text{BE}} = (1 - W) / W$, ``expectancy_net``,
   and deterministic **Edge Prescription Tags** (``POSITIVE_EDGE``,
   ``CHURN_FRICTION_DRAG``, ``WIDEN_TP_OR_CUT_SL``, ``TIGHTEN_ENTRY_FILTER``,
   ``LOW_SAMPLE``).
"""

from __future__ import annotations

from datetime import date
from statistics import median
from typing import Any, Mapping, Optional

from backtest.diary.attribution import make_instance_key
from backtest.diary.ledger import format_holding_duration
from backtest.diary.regimes import validate_regime_group_by

MIN_STATISTICAL_TRADES = 10
PLOT_PAYOFF_CAP = 4.0

HOLD_DURATION_BUCKETS: tuple[tuple[str, str, str, Optional[float], Optional[float]], ...] = (
    ("lt_1m", "< 1m", "Churn Watch", 0.0, 60.0),
    ("1m_5m", "1–5m", "Scalp", 60.0, 300.0),
    ("5m_15m", "5–15m", "Intraday Momentum", 300.0, 900.0),
    ("15m_60m", "15–60m", "Intraday Swing", 900.0, 3600.0),
    ("ge_60m", "60m+", "Session Trend / Carry", 3600.0, None),
)


def classify_hold_bucket(holding_seconds: Any) -> str:
    """Map ``holding_seconds`` onto one of the 5 canonical hold buckets."""
    if holding_seconds is None or holding_seconds == "":
        return "unknown_hold"
    try:
        secs = max(0.0, float(holding_seconds))
    except (TypeError, ValueError):
        return "unknown_hold"

    for key, _lbl, _sub, lo, hi in HOLD_DURATION_BUCKETS:
        if lo is not None and secs < lo:
            continue
        if hi is None or secs < hi:
            return key
    return "ge_60m"


def classify_drag_band(gross_pnl: float, commission: float, trade_count: int) -> str:
    """Classify commission drag into ``none | healthy | warning | critical``."""
    if trade_count <= 0 or commission <= 0.005:
        return "none"
    if gross_pnl <= 0.005:
        return "critical"
    ratio = commission / gross_pnl
    if ratio < 0.30:
        return "healthy"
    if ratio <= 0.50:
        return "warning"
    return "critical"


def _fmt_inr(val: float) -> str:
    sign = "+" if val > 0.005 else ("−" if val < -0.005 else "")
    return f"{sign}₹{abs(val):,.2f}"


def evaluate_edge_prescription(
    *,
    trade_count: int,
    gross_pnl: float,
    commission: float,
    net_pnl: float,
    net_win_rate: float,
    payoff_ratio: Optional[float],
    breakeven_payoff_ratio: Optional[float],
    expectancy_net: float,
) -> dict[str, str]:
    """Deterministic prop-desk edge prescription from realized W, R, and drag."""
    avg_fee = round(commission / trade_count, 2) if trade_count > 0 else 0.0

    # 1. Gross-positive flipped to Net <= 0 by transaction costs (objective leak)
    if trade_count >= 1 and gross_pnl > 0.005 and net_pnl <= 0.0:
        return {
            "code": "CHURN_FRICTION_DRAG",
            "label": "Churn / Friction Drag",
            "severity": "critical",
            "detail": (
                f"Positive gross alpha ({_fmt_inr(gross_pnl)}) flipped to "
                f"{_fmt_inr(net_pnl)} net by ₹{commission:,.2f} commission "
                f"(hurdle ₹{avg_fee:,.2f}/trade). Cut trade frequency or widen TP."
            ),
        }

    # 2. Sample size gate (n < 10)
    if trade_count < MIN_STATISTICAL_TRADES:
        return {
            "code": "LOW_SAMPLE",
            "label": f"Low Sample ({trade_count}/{MIN_STATISTICAL_TRADES})",
            "severity": "neutral",
            "detail": (
                f"Only {trade_count} closed trade(s) in window "
                f"(tentative expectancy {_fmt_inr(expectancy_net)}/trade). "
                f"Accumulate ≥{MIN_STATISTICAL_TRADES} trades before resizing."
            ),
        }

    # 3. Statistically meaningful Positive Net Edge (n >= 10, E > 0)
    if expectancy_net > 0.0:
        r_str = "∞" if payoff_ratio is None else f"{payoff_ratio:.2f}×"
        be_str = "0.00×" if breakeven_payoff_ratio is None else f"{breakeven_payoff_ratio:.2f}×"
        return {
            "code": "POSITIVE_EDGE",
            "label": "Positive Net Edge",
            "severity": "positive",
            "detail": (
                f"Net win rate {net_win_rate * 100:.1f}% with payoff {r_str} "
                f"clears breakeven hurdle {be_str} "
                f"({_fmt_inr(expectancy_net)}/trade net after ₹{avg_fee:,.2f} avg cost)."
            ),
        }

    # 4. Negative/flat expectancy with Win % < 40% -> Entry selectivity problem
    if net_win_rate < 0.40:
        be_str = "∞" if breakeven_payoff_ratio is None else f"{breakeven_payoff_ratio:.2f}×"
        r_str = "0.00×" if payoff_ratio is None else f"{payoff_ratio:.2f}×"
        return {
            "code": "TIGHTEN_ENTRY_FILTER",
            "label": "Tighten Entry Filter",
            "severity": "critical",
            "detail": (
                f"Low net win rate ({net_win_rate * 100:.1f}%) requires payoff ≥ {be_str} "
                f"to break even, vs realized {r_str}. Filter chop/whipsaw entries."
            ),
        }

    # 5. Win % >= 40% (incl. >= 50%) yet E <= 0 -> Payoff asymmetry problem (R < (1-W)/W)
    be_str = "0.00×" if breakeven_payoff_ratio is None else f"{breakeven_payoff_ratio:.2f}×"
    r_str = "0.00×" if payoff_ratio is None else f"{payoff_ratio:.2f}×"
    return {
        "code": "WIDEN_TP_OR_CUT_SL",
        "label": "Widen TP / Cut SL",
        "severity": "warning",
        "detail": (
            f"Win rate is {net_win_rate * 100:.1f}%, but payoff {r_str} trails "
            f"breakeven {be_str} (losers outweigh winners). Let winners run or tighten SL."
        ),
    }


def _compute_hold_bucket_row(
    *,
    key: str,
    label: str,
    subtitle: str,
    trades: list[dict[str, Any]],
    window_total_trades: int,
) -> dict[str, Any]:
    t_count = len(trades)
    gross = round(sum(float(t["gross_pnl"]) for t in trades), 2)
    comm = round(sum(float(t["commission_total"]) for t in trades), 2)
    net = round(sum(float(t["net_pnl"]) for t in trades), 2)
    wins = sum(1 for t in trades if float(t["net_pnl"]) > 0)
    losses = sum(1 for t in trades if float(t["net_pnl"]) < 0)
    flats = t_count - wins - losses

    avg_comm = round(comm / t_count, 2) if t_count else 0.0
    drag_ratio = round(comm / max(abs(gross), 1.0), 4) if t_count else 0.0
    drag_pct: Optional[float] = (
        round((comm / abs(gross)) * 100.0, 2) if abs(gross) > 0.005 else None
    )
    profit_drag_pct: Optional[float] = (
        round((comm / gross) * 100.0, 2) if gross > 0.005 else None
    )

    return {
        "key": key,
        "label": label,
        "subtitle": subtitle,
        "trade_count": t_count,
        "trade_share_pct": (
            round((t_count / window_total_trades) * 100.0, 1)
            if window_total_trades > 0
            else 0.0
        ),
        "win_count": wins,
        "loss_count": losses,
        "flat_count": flats,
        "net_win_rate": round(wins / t_count, 4) if t_count else None,
        "win_rate_pct": round((wins / t_count) * 100.0, 2) if t_count else 0.0,
        "gross_pnl": gross,
        "commission": comm,
        "net_pnl": net,
        "expectancy_net": round(net / t_count, 2) if t_count else 0.0,
        "avg_commission_per_trade": avg_comm,
        "commission_drag_ratio": drag_ratio,
        "commission_drag_pct": drag_pct,
        "profit_drag_pct": profit_drag_pct,
        "drag_band": classify_drag_band(gross, comm, t_count),
    }


def _entity_identity(
    trade: Mapping[str, Any],
    group_by: str,
) -> tuple[str, str, str, Optional[str]]:
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
        return ikey, disp, strat, str(prefix) if prefix else None
    return strat, strat, strat, None


def _compute_entity_quadrant_row(
    *,
    entity_key: str,
    entity_label: str,
    strategy_name: str,
    instance_prefix: Optional[str],
    trades: list[dict[str, Any]],
) -> dict[str, Any]:
    t_count = len(trades)
    gross = round(sum(float(t["gross_pnl"]) for t in trades), 2)
    comm = round(sum(float(t["commission_total"]) for t in trades), 2)
    net = round(sum(float(t["net_pnl"]) for t in trades), 2)

    win_nets = [float(t["net_pnl"]) for t in trades if float(t["net_pnl"]) > 0]
    loss_nets = [float(t["net_pnl"]) for t in trades if float(t["net_pnl"]) < 0]
    wins = len(win_nets)
    losses = len(loss_nets)
    flats = t_count - wins - losses

    w_rate = wins / t_count if t_count else 0.0
    avg_win = round(sum(win_nets) / wins, 2) if wins else 0.0
    avg_loss = round(sum(loss_nets) / losses, 2) if losses else 0.0
    avg_loss_abs = abs(avg_loss)

    unbounded_payoff = bool(wins > 0 and losses == 0)
    payoff_ratio: Optional[float]
    if wins > 0 and avg_loss_abs > 0:
        payoff_ratio = round((sum(win_nets) / wins) / (abs(sum(loss_nets)) / losses), 3)
        plot_payoff = round(min(PLOT_PAYOFF_CAP, payoff_ratio), 3)
    elif unbounded_payoff:
        payoff_ratio = None
        plot_payoff = PLOT_PAYOFF_CAP
    else:
        payoff_ratio = 0.0
        plot_payoff = 0.0

    be_ratio: Optional[float]
    if w_rate <= 0.0:
        be_ratio = None
    elif w_rate >= 1.0:
        be_ratio = 0.0
    else:
        be_ratio = round((1.0 - w_rate) / w_rate, 3)

    win_sum = sum(win_nets)
    loss_abs_sum = abs(sum(loss_nets))
    pf: Optional[float] = (
        round(win_sum / loss_abs_sum, 2) if loss_abs_sum > 0 else None
    )
    expectancy = round(net / t_count, 2) if t_count else 0.0
    avg_comm = round(comm / t_count, 2) if t_count else 0.0

    holds = [
        float(t["holding_seconds"])
        for t in trades
        if t.get("holding_seconds") is not None
    ]
    med_hold = round(float(median(holds)), 1) if holds else None

    rx = evaluate_edge_prescription(
        trade_count=t_count,
        gross_pnl=gross,
        commission=comm,
        net_pnl=net,
        net_win_rate=w_rate,
        payoff_ratio=payoff_ratio,
        breakeven_payoff_ratio=be_ratio,
        expectancy_net=expectancy,
    )

    return {
        "entity_key": entity_key,
        "entity_label": entity_label,
        "strategy_name": strategy_name,
        "instance_prefix": instance_prefix,
        "trade_count": t_count,
        "win_count": wins,
        "loss_count": losses,
        "flat_count": flats,
        "net_win_rate": round(w_rate, 4),
        "win_rate_pct": round(w_rate * 100.0, 2),
        "avg_win_net": avg_win,
        "avg_loss_net": avg_loss,
        "payoff_ratio": payoff_ratio,
        "plot_payoff_ratio": plot_payoff,
        "unbounded_payoff": unbounded_payoff,
        "breakeven_payoff_ratio": be_ratio,
        "profit_factor": pf,
        "unbounded_pf": bool(loss_abs_sum == 0 and win_sum > 0),
        "gross_pnl": gross,
        "commission": comm,
        "net_pnl": net,
        "expectancy_net": expectancy,
        "avg_commission_per_trade": avg_comm,
        "commission_drag_pct": (
            round((comm / abs(gross)) * 100.0, 2) if abs(gross) > 0.005 else None
        ),
        "profit_drag_pct": (
            round((comm / gross) * 100.0, 2) if gross > 0.005 else None
        ),
        "drag_band": classify_drag_band(gross, comm, t_count),
        "median_hold_seconds": med_hold,
        "median_hold_display": format_holding_duration(med_hold),
        "prescription_code": rx["code"],
        "prescription_label": rx["label"],
        "prescription_severity": rx["severity"],
        "prescription_detail": rx["detail"],
    }


def build_breakeven_curve_points(max_y: float = PLOT_PAYOFF_CAP) -> list[dict[str, float]]:
    """Sample points along $R = (1 - W) / W$ clipped to ``[0, max_y]`` for plotting."""
    # R <= max_y  <=>  (1 - W)/W <= max_y  <=>  W >= 1 / (1 + max_y)
    min_w = round(1.0 / (1.0 + max_y), 4)
    points: list[dict[str, float]] = [
        {"win_rate_pct": round(min_w * 100.0, 2), "payoff_ratio": round(max_y, 3)}
    ]
    for pct_int in range(int(min_w * 100.0) + 2, 100, 2):
        w = pct_int / 100.0
        r = (1.0 - w) / w
        points.append({"win_rate_pct": float(pct_int), "payoff_ratio": round(r, 3)})
    points.append({"win_rate_pct": 100.0, "payoff_ratio": 0.0})
    return points


def build_payoff_and_drag_report(
    *,
    book_mode: str,
    start_d: date,
    end_d: date,
    today_ist: date,
    selected_strategy: Optional[str],
    available_strategies: list[str],
    trades: list[dict[str, Any]],
    group_by: str = "instance",
    duplicates_collapsed: int = 0,
) -> dict[str, Any]:
    """Assemble Hold-Duration Friction Table + Payoff Breakeven Quadrant."""
    norm_group = validate_regime_group_by(group_by)
    n_total = len(trades)

    # 1. Hold-duration breakdown
    by_hold: dict[str, list[dict[str, Any]]] = {
        k: [] for k, _l, _s, _lo, _hi in HOLD_DURATION_BUCKETS
    }
    for t in trades:
        hk = classify_hold_bucket(t.get("holding_seconds"))
        by_hold.setdefault(hk, []).append(t)

    hold_rows: list[dict[str, Any]] = [
        _compute_hold_bucket_row(
            key=k,
            label=lbl,
            subtitle=sub,
            trades=by_hold.get(k, []),
            window_total_trades=n_total,
        )
        for k, lbl, sub, _lo, _hi in HOLD_DURATION_BUCKETS
    ]
    if by_hold.get("unknown_hold"):
        hold_rows.append(
            _compute_hold_bucket_row(
                key="unknown_hold",
                label="Unspecified",
                subtitle="Legacy Missing Entry Stamp",
                trades=by_hold["unknown_hold"],
                window_total_trades=n_total,
            )
        )

    hold_totals = _compute_hold_bucket_row(
        key="total",
        label="Book Window Total",
        subtitle="All Closed Trades",
        trades=trades,
        window_total_trades=n_total,
    )

    # 2. Per-Instance / Per-Strategy Payoff Quadrant Coordinates & Prescriptions
    entity_meta: dict[str, tuple[str, str, Optional[str]]] = {}
    entity_trades: dict[str, list[dict[str, Any]]] = {}
    for t in trades:
        ekey, elabel, sname, iprefix = _entity_identity(t, norm_group)
        entity_meta[ekey] = (elabel, sname, iprefix)
        entity_trades.setdefault(ekey, []).append(t)

    entities: list[dict[str, Any]] = []
    for ekey, et_list in entity_trades.items():
        elabel, sname, iprefix = entity_meta[ekey]
        entities.append(
            _compute_entity_quadrant_row(
                entity_key=ekey,
                entity_label=elabel,
                strategy_name=sname,
                instance_prefix=iprefix,
                trades=et_list,
            )
        )

    # Sort worst net_pnl first so capital bleeders & churn traps surface immediately
    entities.sort(key=lambda e: (e["net_pnl"], -(e["trade_count"])))

    return {
        "mode": book_mode,
        "from_date": start_d.isoformat(),
        "to_date": end_d.isoformat(),
        "today_ist": today_ist.isoformat(),
        "selected_strategy": selected_strategy or "all",
        "available_strategies": available_strategies,
        "group_by": norm_group,
        "min_statistical_trades": MIN_STATISTICAL_TRADES,
        "plot_payoff_cap": PLOT_PAYOFF_CAP,
        "hold_buckets": hold_rows,
        "hold_totals": hold_totals,
        "entities": entities,
        "breakeven_curve": build_breakeven_curve_points(PLOT_PAYOFF_CAP),
        "duplicates_collapsed": int(duplicates_collapsed),
    }
