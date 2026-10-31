"""Slice 5 — Paper → Live Graduation Scorecard Engine.

Evaluates every spawned strategy instance (active + retired) in a book
(``paper`` or ``live``) across 6 institutional risk & edge gates:

1. ``sample_size``      — Closed round-trip sample ``n >= 20``.
2. ``net_expectancy``   — Net expectancy ``E[net] > ₹0.00`` after combined fees.
3. ``profit_factor``    — Net profit factor ``PF >= 1.35`` (or unbounded > 0 wins).
4. ``commission_drag``  — ``Commission / |Gross| <= 35%``.
5. ``max_drawdown``     — Peak-to-trough realized drawdown within cap
                          (``<= 10%`` of allocated capital, or ``<= ₹25,000``
                          when capital is unrecorded) AND not ``BLOCKED_BY_RISK``.
6. ``ledger_integrity`` — ``reconcile_anomalies == 0`` (exact 2dp E2 truth).

Also inspects option structures against ``config/risk_limits.yaml``
``entitlement.live`` (read-only) so Paper short/multi-leg option runners carry
an explicit ``entitlement_advisory`` badge before any Live promotion review.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any, Mapping, Optional, Sequence

from backtest.diary.attribution import (
    _resolve_lifecycle_status,
    extract_runner_config_badges,
    make_instance_key,
)

MIN_GRADUATION_TRADES = 20
MIN_SIGNIFICANT_KILL_TRADES = 10
MIN_PROFIT_FACTOR = 1.35
MAX_COMMISSION_DRAG_PCT = 35.0
SEVERE_COMMISSION_DRAG_PCT = 75.0
DEFAULT_MAX_DRAWDOWN_PCT_CAP = 10.0
DEFAULT_MAX_DRAWDOWN_INR_CAP = 25_000.0

KNOWN_OPTION_STRUCTURES = (
    "long_call",
    "long_put",
    "bull_call_spread",
    "bear_put_spread",
    "long_straddle",
    "strangle",
    "short_straddle",
    "short_call",
    "short_put",
    "iron_condor",
)

_CE_RE = re.compile(r"(?:\bCE\b|\d+CE\b)", re.IGNORECASE)
_PE_RE = re.compile(r"(?:\bPE\b|\d+PE\b)", re.IGNORECASE)

VERDICT_META: dict[str, dict[str, str]] = {
    "PROMOTE_CANDIDATE": {
        "paper_label": "Promote Candidate",
        "live_label": "Healthy Live Runner",
        "tone": "positive",
    },
    "TUNE_EDGE": {
        "paper_label": "Tune Edge / Sample",
        "live_label": "Monitor / Tune",
        "tone": "caution",
    },
    "RETIRE_OR_KILL": {
        "paper_label": "Retire / Kill",
        "live_label": "Halt / Retire",
        "tone": "negative",
    },
}


def _load_live_option_entitlements() -> tuple[str, ...]:
    """Read ``entitlement.live`` from ``config/risk_limits.yaml`` fail-soft."""
    try:
        from backtest.risk_limits import load_risk_limits

        limits = load_risk_limits()
        live_list = getattr(limits.entitlement, "live", None)
        if live_list:
            return tuple(str(x).strip().lower() for x in live_list if str(x).strip())
    except Exception:  # noqa: BLE001 — fallback to canonical live baseline
        pass
    return ("long_call", "long_put")


def compute_realized_drawdown(
    trades: Sequence[Mapping[str, Any]],
    *,
    allocated_capital: Optional[float] = None,
) -> dict[str, Any]:
    """Compute peak-to-trough realized net P&L drawdown in INR and %."""
    cap = float(allocated_capital) if allocated_capital and allocated_capital > 0 else 0.0
    ordered = sorted(
        trades,
        key=lambda t: (
            str(t.get("exit_ts") or ""),
            str(t.get("entry_ts") or ""),
            str(t.get("symbol") or ""),
        ),
    )

    if cap > 0.0:
        equity = cap
        peak = cap
        max_dd_inr = 0.0
        max_dd_pct = 0.0
        for t in ordered:
            equity += float(t.get("net_pnl") or 0.0)
            if equity > peak:
                peak = equity
            dd_inr = max(0.0, peak - equity)
            if dd_inr > max_dd_inr:
                max_dd_inr = dd_inr
            if peak > 0:
                dd_pct = (dd_inr / peak) * 100.0
                if dd_pct > max_dd_pct:
                    max_dd_pct = dd_pct
        return {
            "allocated_capital": round(cap, 2),
            "max_drawdown_inr": round(max_dd_inr, 2),
            "max_drawdown_pct": round(max_dd_pct, 2),
        }

    cum_pnl = 0.0
    peak_pnl = 0.0
    max_dd_inr = 0.0
    for t in ordered:
        cum_pnl += float(t.get("net_pnl") or 0.0)
        if cum_pnl > peak_pnl:
            peak_pnl = cum_pnl
        dd_inr = max(0.0, peak_pnl - cum_pnl)
        if dd_inr > max_dd_inr:
            max_dd_inr = dd_inr
    return {
        "allocated_capital": None,
        "max_drawdown_inr": round(max_dd_inr, 2),
        "max_drawdown_pct": None,
    }


def _infer_option_leg_structure(item: Mapping[str, Any]) -> Optional[str]:
    """Infer canonical option structure token from a trade or open-position row."""
    explicit = str(
        item.get("structure_type") or item.get("structure") or ""
    ).strip().lower()
    if explicit in KNOWN_OPTION_STRUCTURES:
        return explicit

    sym = f"{item.get('symbol') or ''} {item.get('display_symbol') or ''}".strip()
    sym_low = sym.lower()
    for struct in KNOWN_OPTION_STRUCTURES:
        if struct in sym_low:
            return struct

    kind = str(
        item.get("kind") or item.get("instrument_type") or ""
    ).strip().lower()
    opt_type = str(item.get("option_type") or "").strip().upper()
    if not opt_type:
        if _CE_RE.search(sym):
            opt_type = "CE"
        elif _PE_RE.search(sym):
            opt_type = "PE"

    if kind == "option" or opt_type in ("CE", "PE"):
        side = str(item.get("side") or "LONG").strip().upper()
        if side.startswith("SHORT") or side == "SELL":
            if opt_type == "PE":
                return "short_put"
            return "short_call"
        if opt_type == "PE":
            return "long_put"
        if opt_type == "CE":
            return "long_call"
    return None


def detect_option_entitlement_advisory(
    instance: Mapping[str, Any],
    *,
    book_mode: str,
    live_entitlements: Sequence[str],
) -> dict[str, Any]:
    """Inspect badges, open structures, and closed legs for non-live option structures."""
    allowed = {str(x).strip().lower() for x in live_entitlements}
    detected: list[str] = []

    def _record(tag: Optional[str]) -> None:
        if not tag:
            return
        norm = tag.strip().lower()
        if norm and norm not in detected:
            detected.append(norm)

    for badge in instance.get("config_badges") or ():
        b_low = str(badge).strip().lower()
        if b_low in KNOWN_OPTION_STRUCTURES:
            _record(b_low)

    for op in instance.get("open_positions") or instance.get("open_structures") or ():
        if isinstance(op, Mapping):
            _record(_infer_option_leg_structure(op))

    for t in instance.get("trades") or ():
        if isinstance(t, Mapping):
            _record(_infer_option_leg_structure(t))

    restricted = [s for s in detected if s not in allowed]
    requires_review = bool(restricted)

    if requires_review and book_mode == "paper":
        allowed_csv = ", ".join(sorted(allowed)) or "none"
        restricted_csv = ", ".join(restricted)
        message = (
            f"Uses [{restricted_csv}]; config/risk_limits.yaml live entitlement "
            f"currently permits [{allowed_csv}] only."
        )
    elif requires_review:
        restricted_csv = ", ".join(restricted)
        message = (
            f"Non-standard live structure [{restricted_csv}] detected vs "
            f"risk_limits.yaml live entitlement."
        )
    else:
        message = None

    return {
        "requires_live_entitlement_review": requires_review,
        "detected_structures": detected,
        "restricted_structures": restricted,
        "permitted_live_structures": sorted(allowed),
        "message": message,
    }


def evaluate_instance_graduation(
    instance: Mapping[str, Any],
    *,
    book_mode: str,
    trades_override: Optional[Sequence[Mapping[str, Any]]] = None,
    max_drawdown_pct_cap: float = DEFAULT_MAX_DRAWDOWN_PCT_CAP,
    max_drawdown_inr_cap: float = DEFAULT_MAX_DRAWDOWN_INR_CAP,
    live_entitlements: Optional[Sequence[str]] = None,
) -> dict[str, Any]:
    """Score a single spawned instance across the 6 institutional gates."""
    trades = (
        list(trades_override)
        if trades_override is not None
        else list(instance.get("trades") or ())
    )
    trade_count = int(instance.get("trade_count") or len(trades))
    expectancy_net = float(instance.get("expectancy_net") or 0.0)
    net_pnl = round(float(instance.get("net_pnl") or 0.0), 2)
    gross_pnl = round(float(instance.get("gross_pnl") or 0.0), 2)
    commission = round(float(instance.get("commission") or 0.0), 2)
    pf = instance.get("profit_factor")
    unbounded_pf = bool(instance.get("unbounded_pf", False))
    comm_drag_pct = instance.get("commission_drag_pct")
    anomalies = int(instance.get("reconcile_anomalies") or 0)
    lifecycle = str(instance.get("lifecycle_status") or "RETIRED").upper()
    risk_blocked = lifecycle == "BLOCKED_BY_RISK"

    dd = compute_realized_drawdown(
        trades,
        allocated_capital=instance.get("allocated_capital"),
    )
    dd_pct = dd["max_drawdown_pct"]
    dd_inr = dd["max_drawdown_inr"]

    g_sample = trade_count >= MIN_GRADUATION_TRADES
    g_expectancy = trade_count > 0 and expectancy_net > 0.0
    g_pf = bool(
        unbounded_pf or (pf is not None and float(pf) >= MIN_PROFIT_FACTOR)
    )
    if trade_count == 0:
        g_drag = False
    elif comm_drag_pct is not None:
        g_drag = float(comm_drag_pct) <= MAX_COMMISSION_DRAG_PCT
    else:
        g_drag = commission <= 0.005

    if risk_blocked:
        g_dd = False
    elif dd_pct is not None:
        g_dd = float(dd_pct) <= max_drawdown_pct_cap
    else:
        g_dd = float(dd_inr) <= max_drawdown_inr_cap

    g_integrity = anomalies == 0

    gates = {
        "sample_size": {
            "passed": g_sample,
            "actual": trade_count,
            "target": f">= {MIN_GRADUATION_TRADES} trades",
        },
        "net_expectancy": {
            "passed": g_expectancy,
            "actual": round(expectancy_net, 2),
            "target": "> ₹0.00 / trade",
        },
        "profit_factor": {
            "passed": g_pf,
            "actual": "INF" if unbounded_pf else pf,
            "target": f">= {MIN_PROFIT_FACTOR:.2f}",
        },
        "commission_drag": {
            "passed": g_drag,
            "actual": comm_drag_pct,
            "target": f"<= {MAX_COMMISSION_DRAG_PCT:.0f}%",
        },
        "max_drawdown": {
            "passed": g_dd,
            "actual_pct": dd_pct,
            "actual_inr": dd_inr,
            "risk_blocked": risk_blocked,
            "target": (
                f"<= {max_drawdown_pct_cap:.1f}% cap & OK"
                if dd_pct is not None
                else f"<= ₹{max_drawdown_inr_cap:,.0f} & OK"
            ),
        },
        "ledger_integrity": {
            "passed": g_integrity,
            "actual": anomalies,
            "target": "0 mismatches",
        },
    }

    gates_passed = sum(1 for g in gates.values() if g["passed"])
    all_passed = gates_passed == len(gates)

    severe_negative_edge = (
        trade_count >= MIN_SIGNIFICANT_KILL_TRADES
        and net_pnl < 0.0
        and not unbounded_pf
        and (pf is None or float(pf) < 1.0)
    )
    severe_churn_bleed = (
        trade_count >= 5
        and net_pnl < 0.0
        and comm_drag_pct is not None
        and float(comm_drag_pct) >= SEVERE_COMMISSION_DRAG_PCT
    )

    if all_passed:
        verdict = "PROMOTE_CANDIDATE"
        rationale = (
            "All 6 institutional gates passed; net expectancy, friction drag, "
            "and drawdown are within promotion limits."
        )
    elif not g_integrity:
        verdict = "RETIRE_OR_KILL"
        rationale = (
            f"Ledger integrity gate failed ({anomalies} Gross - Commission != Net "
            "anomalies); halt promotion until reconciled."
        )
    elif risk_blocked or not g_dd:
        verdict = "RETIRE_OR_KILL"
        if risk_blocked:
            rationale = "Runner is BLOCKED_BY_RISK; clear risk block or retire."
        elif dd_pct is not None:
            rationale = (
                f"Realized drawdown {dd_pct:.2f}% breached {max_drawdown_pct_cap:.1f}% cap."
            )
        else:
            rationale = (
                f"Realized drawdown ₹{dd_inr:,.2f} breached ₹{max_drawdown_inr_cap:,.0f} cap."
            )
    elif severe_negative_edge or severe_churn_bleed:
        verdict = "RETIRE_OR_KILL"
        if severe_churn_bleed and comm_drag_pct is not None:
            rationale = (
                f"Negative net P&L with {comm_drag_pct:.1f}% commission drag; "
                "structural churn exceeds gross alpha."
            )
        else:
            rationale = (
                f"Negative net expectancy (₹{expectancy_net:,.2f}/trade) across "
                f"{trade_count} trades with PF < 1.00."
            )
    else:
        verdict = "TUNE_EDGE"
        missing: list[str] = []
        if not g_sample:
            missing.append(f"sample {trade_count}/{MIN_GRADUATION_TRADES}")
        if not g_expectancy:
            missing.append("net expectancy <= ₹0")
        if not g_pf:
            missing.append(f"PF < {MIN_PROFIT_FACTOR:.2f}")
        if not g_drag:
            missing.append(f"fee drag > {MAX_COMMISSION_DRAG_PCT:.0f}%")
        rationale = "Pending gates: " + ", ".join(missing) + "."

    meta = VERDICT_META[verdict]
    label_key = "live_label" if book_mode == "live" else "paper_label"

    resolved_live_ent = (
        live_entitlements
        if live_entitlements is not None
        else _load_live_option_entitlements()
    )
    advisory = detect_option_entitlement_advisory(
        {**instance, "trades": trades},
        book_mode=book_mode,
        live_entitlements=resolved_live_ent,
    )

    strat_name = (
        instance.get("strategy_name") or instance.get("strategy") or "unassigned"
    )
    inst_label = (
        instance.get("display_name")
        or instance.get("instance_label")
        or instance.get("name")
        or strat_name
    )
    is_active = bool(instance.get("is_active", False))
    iid = instance.get("instance_id")
    deep_link = (
        instance.get("portfolio_deep_link")
        or (f"/portfolio/{book_mode}?instance={iid}" if (is_active and iid) else None)
    )

    return {
        "instance_key": instance.get("instance_key"),
        "strategy": strat_name,
        "strategy_name": strat_name,
        "instance_label": inst_label,
        "display_name": inst_label,
        "instance_id": iid,
        "instance_prefix": instance.get("instance_prefix"),
        "portfolio_id": instance.get("portfolio_id"),
        "lifecycle_status": lifecycle,
        "is_active": is_active,
        "portfolio_deep_link": deep_link,
        "config_badges": list(instance.get("config_badges") or ()),
        "symbols": list(instance.get("symbols") or ()),
        "timeframe": instance.get("timeframe"),
        "allocated_capital": dd["allocated_capital"],
        "trade_count": trade_count,
        "win_rate_pct": round(float(instance.get("win_rate_pct") or 0.0), 2),
        "profit_factor": pf,
        "unbounded_pf": unbounded_pf,
        "expectancy_net": round(expectancy_net, 2),
        "gross_pnl": gross_pnl,
        "commission": commission,
        "net_pnl": net_pnl,
        "commission_drag_pct": comm_drag_pct,
        "running_mtm": instance.get("running_mtm"),
        "max_drawdown_inr": dd_inr,
        "max_drawdown_pct": dd_pct,
        "reconcile_anomalies": anomalies,
        "gates": gates,
        "gates_passed": gates_passed,
        "gates_total": len(gates),
        "verdict": verdict,
        "verdict_label": meta[label_key],
        "verdict_tone": meta["tone"],
        "rationale": rationale,
        "entitlement_advisory": advisory,
    }


def _idle_active_instance_stub(
    state_row: Mapping[str, Any],
    *,
    runner_obj: Any,
    includes_today: bool,
) -> dict[str, Any]:
    """Materialize a zero-trade active runner so risk-blocked/idle runners appear."""
    strat = str(state_row.get("strategy_name") or "unassigned").strip() or "unassigned"
    iid = str(state_row.get("instance_id") or "").strip() or None
    prefix = iid[:8].lower() if iid else None
    clean_name = str(state_row.get("name") or strat).strip()
    display_name = f"{clean_name} [{prefix}]" if prefix else clean_name
    ikey = make_instance_key(
        strat,
        instance_prefix=prefix,
        runner_id=iid,
        runner_name=clean_name,
    )
    alloc_raw = state_row.get("allocated_capital")
    alloc_cap = round(float(alloc_raw), 2) if alloc_raw is not None else None
    mtm = round(float(state_row.get("open_pnl") or 0.0), 2) if includes_today else None
    return {
        "instance_key": ikey,
        "instance_id": iid,
        "instance_prefix": prefix,
        "portfolio_id": None,
        "name": clean_name,
        "display_name": display_name,
        "strategy_name": strat,
        "is_active": True,
        "lifecycle_status": _resolve_lifecycle_status(state_row),
        "symbols": list(state_row.get("symbols") or ())[:8],
        "timeframe": state_row.get("timeframe"),
        "allocated_capital": alloc_cap,
        "config_badges": extract_runner_config_badges(runner_obj, state_row),
        "trade_count": 0,
        "win_rate_pct": 0.0,
        "profit_factor": None,
        "unbounded_pf": False,
        "expectancy_net": 0.0,
        "gross_pnl": 0.0,
        "commission": 0.0,
        "net_pnl": 0.0,
        "commission_drag_pct": None,
        "running_mtm": mtm,
        "reconcile_anomalies": 0,
        "open_positions": [],
        "trades": [],
    }


def build_graduation_scorecard(
    *,
    book_mode: str,
    start_d: date,
    end_d: date,
    today_ist: date,
    selected_strategy: Optional[str],
    available_strategies: list[str],
    attribution_tree: Mapping[str, Any],
    active_instances: Sequence[Mapping[str, Any]] = (),
    runner_lookup: Any = None,
    max_drawdown_pct_cap: float = DEFAULT_MAX_DRAWDOWN_PCT_CAP,
    max_drawdown_inr_cap: float = DEFAULT_MAX_DRAWDOWN_INR_CAP,
) -> dict[str, Any]:
    """Build the full Paper → Live Graduation (or Live Health) Scorecard."""
    live_entitlements = _load_live_option_entitlements()
    includes_today = bool(attribution_tree.get("includes_today", False))
    rows: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    seen_iids: set[str] = set()

    for strat_node in attribution_tree.get("strategies") or ():
        for inst in strat_node.get("instances") or ():
            if inst.get("instance_key"):
                seen_keys.add(str(inst["instance_key"]))
            if inst.get("instance_id"):
                seen_iids.add(str(inst["instance_id"]))
            rows.append(
                evaluate_instance_graduation(
                    inst,
                    book_mode=book_mode,
                    max_drawdown_pct_cap=max_drawdown_pct_cap,
                    max_drawdown_inr_cap=max_drawdown_inr_cap,
                    live_entitlements=live_entitlements,
                )
            )

    # Also surface active zero-trade runners (e.g. BLOCKED_BY_RISK or newly spawned)
    if includes_today:
        for rs in active_instances:
            strat = str(rs.get("strategy_name") or "unassigned").strip() or "unassigned"
            if selected_strategy and strat != selected_strategy:
                continue
            iid = str(rs.get("instance_id") or "").strip()
            if iid and iid in seen_iids:
                continue
            runner_obj = (
                runner_lookup(iid) if (iid and callable(runner_lookup)) else None
            )
            stub = _idle_active_instance_stub(
                rs,
                runner_obj=runner_obj,
                includes_today=includes_today,
            )
            if stub["instance_key"] in seen_keys:
                continue
            seen_keys.add(stub["instance_key"])
            rows.append(
                evaluate_instance_graduation(
                    stub,
                    book_mode=book_mode,
                    max_drawdown_pct_cap=max_drawdown_pct_cap,
                    max_drawdown_inr_cap=max_drawdown_inr_cap,
                    live_entitlements=live_entitlements,
                )
            )

    verdict_order = {"PROMOTE_CANDIDATE": 0, "TUNE_EDGE": 1, "RETIRE_OR_KILL": 2}
    rows.sort(
        key=lambda r: (
            verdict_order.get(r["verdict"], 9),
            -int(r["gates_passed"]),
            -float(r["net_pnl"]),
            str(r["instance_label"] or ""),
        )
    )

    promote_count = sum(1 for r in rows if r["verdict"] == "PROMOTE_CANDIDATE")
    tune_count = sum(1 for r in rows if r["verdict"] == "TUNE_EDGE")
    retire_count = sum(1 for r in rows if r["verdict"] == "RETIRE_OR_KILL")
    advisory_count = sum(
        1
        for r in rows
        if r["entitlement_advisory"]["requires_live_entitlement_review"]
    )

    totals = attribution_tree.get("totals") or {}
    return {
        "mode": book_mode,
        "scorecard_kind": (
            "paper_to_live_graduation"
            if book_mode == "paper"
            else "live_runner_health"
        ),
        "from_date": start_d.isoformat(),
        "to_date": end_d.isoformat(),
        "today_ist": today_ist.isoformat(),
        "includes_today": includes_today,
        "selected_strategy": selected_strategy or "all",
        "available_strategies": list(available_strategies),
        "thresholds": {
            "min_trades": MIN_GRADUATION_TRADES,
            "min_net_expectancy_inr": 0.0,
            "min_profit_factor": MIN_PROFIT_FACTOR,
            "max_commission_drag_pct": MAX_COMMISSION_DRAG_PCT,
            "max_drawdown_pct_cap": max_drawdown_pct_cap,
            "max_drawdown_inr_cap": max_drawdown_inr_cap,
            "permitted_live_option_structures": list(live_entitlements),
        },
        "summary": {
            "instance_count": len(rows),
            "promote_count": promote_count,
            "tune_count": tune_count,
            "retire_count": retire_count,
            "entitlement_advisory_count": advisory_count,
            "trade_count": int(totals.get("trade_count") or 0),
            "gross_pnl": round(float(totals.get("gross_pnl") or 0.0), 2),
            "commission": round(float(totals.get("commission") or 0.0), 2),
            "net_pnl": round(float(totals.get("net_pnl") or 0.0), 2),
            "reconcile_anomalies": int(totals.get("reconcile_anomalies") or 0),
        },
        "instances": rows,
        "reconcile_anomalies": int(totals.get("reconcile_anomalies") or 0),
    }
