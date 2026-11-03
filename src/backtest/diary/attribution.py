"""Slice 2 — 3-Level Strategy → Spawned Instance → Trade/Structure Attribution.

Builds the deterministic attribution hierarchy for a single book (``paper`` or
``live``) and IST window ``[start_date, end_date]``:

* **Level 1 (Strategy):** aggregates across all spawned instances (active +
  retired) belonging to ``strategy_name``.
* **Level 2 (Spawned Instance):** merges persisted ``portfolios`` history
  (``"<name> [<id8>]"``) with live ``PortfolioManager`` runner metadata when
  active (``RUNNING | PAUSED | BLOCKED_BY_RISK | STOPPED | RETIRED``), symbols,
  timeframe, allocated capital, config badges, and today-only live
  ``running_mtm``.
* **Level 3 (Closed Trades & Open Structures):** deduplicated closed round-trips
  plus today's live open equity positions / option structures, with exact 2dp
  ``Gross - Commission = Net`` verification and **single combined Commission**
  (zero sub-component fee split per E2).

Hierarchical invariants guaranteed to the paisa:
1. ``sum(inst.net_pnl for inst in strat.instances) == strat.net_pnl``
2. ``sum(strat.net_pnl for strat in strategies) == totals.net_pnl``
3. ``sum(strat.gross_pnl for strat in strategies) == totals.gross_pnl``
4. ``sum(strat.commission for strat in strategies) == totals.commission``
5. On windows including IST today:
   ``sum(strat.running_mtm for strat in strategies) == totals.running_mtm``
"""

from __future__ import annotations

from datetime import date
from typing import Any, Mapping, Optional

from backtest.timekeeping import from_stored_utc, to_exchange

DEFAULT_INLINE_TRADE_LIMIT = 250

_CONFIG_PARAM_KEYS = (
    ("lots", "lots"),
    ("lot_size_mult", "lots"),
    ("quantity", "qty"),
    ("stop_loss_pct", "SL%"),
    ("sl_pct", "SL%"),
    ("stop_loss", "SL"),
    ("take_profit_pct", "TP%"),
    ("tp_pct", "TP%"),
    ("target_pct", "TP%"),
    ("trailing_stop_pct", "TSL%"),
    ("carry_policy", "carry"),
    ("product_type", "prod"),
)


def make_instance_key(
    strategy_name: str,
    *,
    instance_prefix: Optional[str] = None,
    portfolio_id: Optional[str] = None,
    runner_id: Optional[str] = None,
    runner_name: Optional[str] = None,
) -> str:
    """Deterministic key binding a spawned instance under its strategy."""
    strat = (strategy_name or "unassigned").strip() or "unassigned"
    if instance_prefix:
        return f"{strat}::{str(instance_prefix).strip().lower()}"
    if runner_id and len(str(runner_id).strip()) >= 8:
        return f"{strat}::{str(runner_id).strip()[:8].lower()}"
    if portfolio_id:
        return f"{strat}::pid:{str(portfolio_id).strip().lower()}"
    fallback = (runner_name or runner_id or "default").strip().lower()
    return f"{strat}::mem:{fallback}"


def compute_rollup_metrics(
    trades: list[dict[str, Any]],
    *,
    includes_today: bool,
    running_mtm: float = 0.0,
    open_count: int = 0,
) -> dict[str, Any]:
    """Compute exact 2dp rollup statistics shared by Strategy & Instance nodes."""
    gross = round(sum(float(t["gross_pnl"]) for t in trades), 2)
    comm = round(sum(float(t["commission_total"]) for t in trades), 2)
    net = round(sum(float(t["net_pnl"]) for t in trades), 2)
    t_count = len(trades)

    win_nets = [float(t["net_pnl"]) for t in trades if float(t["net_pnl"]) > 0]
    loss_nets = [float(t["net_pnl"]) for t in trades if float(t["net_pnl"]) < 0]
    all_nets = [float(t["net_pnl"]) for t in trades]

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
        round((comm / abs(gross)) * 100.0, 2) if abs(gross) > 0.005 else None
    )
    comm_drag_ratio: Optional[float] = (
        round(comm / abs(gross), 4) if abs(gross) > 0.005 else None
    )

    mtm_val: Optional[float] = round(float(running_mtm), 2) if includes_today else None
    open_val = int(open_count) if includes_today else 0
    combined = round(net + (mtm_val or 0.0), 2) if includes_today else net

    return {
        "trade_count": t_count,
        "win_count": wins,
        "loss_count": losses,
        "flat_count": flats,
        "net_win_rate": round(wins / t_count, 4) if t_count else 0.0,
        "win_rate_pct": round((wins / t_count) * 100.0, 2) if t_count else 0.0,
        "gross_pnl": gross,
        "commission": comm,
        "net_pnl": net,
        "realized_net_pnl": net,
        "commission_drag_pct": comm_drag_pct,
        "commission_drag_ratio": comm_drag_ratio,
        "running_mtm": mtm_val,
        "open_count": open_val,
        "total_pnl_with_mtm": combined,
        "profit_factor": profit_factor,
        "unbounded_pf": bool(loss_abs_sum == 0 and win_sum > 0),
        "expectancy_net": round(net / t_count, 2) if t_count else 0.0,
        "avg_win_net": round(win_sum / wins, 2) if wins else 0.0,
        "avg_loss_net": round(sum(loss_nets) / losses, 2) if losses else 0.0,
        "max_win_net": round(max(all_nets), 2) if all_nets else 0.0,
        "max_loss_net": round(min(all_nets), 2) if all_nets else 0.0,
        "reconcile_anomalies": mismatches,
        "mismatch_count": mismatches,
    }


def _resolve_lifecycle_status(state_row: Optional[Mapping[str, Any]]) -> str:
    if state_row is None:
        return "RETIRED"
    if state_row.get("block_reason") or str(state_row.get("trade_health") or "OK") != "OK":
        return "BLOCKED_BY_RISK"
    raw = str(state_row.get("status") or "RUNNING").strip().upper()
    return raw or "RUNNING"


def extract_runner_config_badges(
    runner: Any,
    state_row: Optional[Mapping[str, Any]],
) -> list[str]:
    """Build compact human-readable configuration pills for a spawned instance."""
    badges: list[str] = []
    cfg = getattr(runner, "config", None) if runner is not None else None

    tf = (state_row or {}).get("timeframe") or getattr(cfg, "timeframe", None)
    if tf:
        badges.append(str(tf))

    instrument = (
        (state_row or {}).get("instrument")
        or getattr(cfg, "instrument", None)
        or {}
    )
    if isinstance(instrument, Mapping):
        expr = instrument.get("expression")
        if isinstance(expr, Mapping):
            struct_type = expr.get("structure") or expr.get("structure_type")
            if struct_type:
                badges.append(str(struct_type))
            carry = expr.get("carry_policy") or expr.get("horizon")
            if carry:
                badges.append(str(carry))
        elif instrument.get("type") == "option":
            badges.append("options")

    params = getattr(cfg, "strategy_params", None)
    if isinstance(params, Mapping):
        seen_labels: set[str] = set()
        for raw_key, short_label in _CONFIG_PARAM_KEYS:
            if (
                raw_key in params
                and params[raw_key] not in (None, "")
                and short_label not in seen_labels
            ):
                badges.append(f"{short_label}={params[raw_key]}")
                seen_labels.add(short_label)
                if len(badges) >= 6:
                    break
    return badges


def _format_entry_ist(raw_ts: Any) -> Optional[str]:
    if not raw_ts:
        return None
    try:
        dt_u = from_stored_utc(raw_ts)
        if dt_u is None:
            return None
        ist_dt = to_exchange(dt_u)
        return f"{ist_dt:%d-%b %H:%M:%S} IST" if ist_dt is not None else None
    except Exception:  # noqa: BLE001
        return None


def extract_open_position_rows(
    runner: Any,
    state_row: Optional[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Extract live open equity positions & option structures on IST today."""
    rows: list[dict[str, Any]] = []
    if runner is None and state_row is None:
        return rows

    # 1. Equity positions dict on real PaperRunner (``runner.positions``)
    positions_map = getattr(runner, "positions", None)
    last_price_map = getattr(runner, "last_price", {}) or {}
    if isinstance(positions_map, Mapping):
        for sym, pos in positions_map.items():
            if not isinstance(pos, Mapping):
                continue
            qty = float(pos.get("qty") or 0.0)
            entry_p = float(pos.get("entry_price") or 0.0)
            cur_p = float(last_price_map.get(sym, entry_p))
            side = str(pos.get("side") or "LONG").upper()
            mult = -1.0 if side.startswith("SHORT") else 1.0
            upnl = round((cur_p - entry_p) * abs(qty) * mult, 2)
            rows.append(
                {
                    "symbol": str(sym),
                    "display_symbol": str(sym),
                    "kind": "equity",
                    "side": side,
                    "qty": abs(qty),
                    "entry_price": round(entry_p, 2),
                    "current_price": round(cur_p, 2),
                    "unrealized_pnl": upnl,
                    "entry_ist_label": _format_entry_ist(pos.get("entry_ts")),
                    "mark_stale": False,
                }
            )

    # 2. Option structures from ``state_row["options"]["structures"]``
    opts = (state_row or {}).get("options")
    if isinstance(opts, Mapping):
        structures = opts.get("structures")
        if isinstance(structures, list):
            for st in structures:
                if not isinstance(st, Mapping):
                    continue
                label = (
                    st.get("label")
                    or st.get("display_label")
                    or f"{st.get('underlying', '')} {st.get('structure_type', 'option')}".strip()
                )
                entry_prem = float(st.get("entry_premium") or st.get("entry_price") or 0.0)
                cur_prem = float(st.get("current_premium") or st.get("current_price") or 0.0)
                upnl_val = float(st.get("unrealized_pnl") or st.get("pnl") or 0.0)
                rows.append(
                    {
                        "symbol": str(st.get("symbol") or label),
                        "display_symbol": str(label),
                        "kind": "option",
                        "side": str(st.get("side") or "LONG").upper(),
                        "qty": float(st.get("lots") or st.get("qty") or 1),
                        "entry_price": round(entry_prem, 2),
                        "current_price": round(cur_prem, 2),
                        "unrealized_pnl": round(upnl_val, 2),
                        "entry_ist_label": _format_entry_ist(
                            st.get("opened_at") or st.get("entry_ts")
                        ),
                        "mark_stale": bool(st.get("mark_stale") or opts.get("mark_stale")),
                    }
                )

    # 3. Fallback for lightweight test stubs exposing ``_open_positions`` list
    stub_open = getattr(runner, "_open_positions", None)
    if not rows and isinstance(stub_open, list):
        fallback_upnl = float((state_row or {}).get("open_pnl") or 0.0)
        per_row_upnl = round(fallback_upnl / len(stub_open), 2) if stub_open else 0.0
        for item in stub_open:
            if not isinstance(item, Mapping):
                continue
            sym = str(item.get("symbol") or "?")
            rows.append(
                {
                    "symbol": sym,
                    "display_symbol": str(item.get("label") or sym),
                    "kind": str(item.get("kind") or "option"),
                    "side": str(item.get("side") or "LONG").upper(),
                    "qty": float(item.get("qty") or 1),
                    "entry_price": round(float(item.get("entry_price") or 0.0), 2),
                    "current_price": round(float(item.get("current_price") or 0.0), 2),
                    "unrealized_pnl": round(
                        float(item.get("unrealized_pnl", per_row_upnl)), 2
                    ),
                    "entry_ist_label": _format_entry_ist(item.get("entry_ts")),
                    "mark_stale": bool(item.get("mark_stale", False)),
                }
            )
    return rows


def format_public_trade_row(trade: Mapping[str, Any]) -> dict[str, Any]:
    """Emit a clean Level-3 closed-trade record (single Commission sum per E2)."""
    comm = round(float(trade.get("commission_total", trade.get("commission", 0.0)) or 0.0), 2)
    symbol = str(trade.get("symbol") or "?")
    display_sym = str(trade.get("label") or symbol)
    return {
        "trade_id": trade.get("trade_id"),
        "symbol": symbol,
        "display_symbol": display_sym,
        "kind": str(trade.get("kind") or "equity"),
        "side": str(trade.get("side") or "LONG").upper(),
        "qty": float(trade.get("qty") or 0.0),
        "units": trade.get("units"),
        "entry_price": round(float(trade.get("entry_price") or 0.0), 2),
        "exit_price": round(float(trade.get("exit_price") or 0.0), 2),
        "entry_ts": trade.get("entry_ts"),
        "exit_ts": trade.get("exit_ts"),
        "entry_ist_label": trade.get("entry_ist_label"),
        "exit_ist_label": trade.get("exit_ist_label"),
        "session_date": trade.get("session_date"),
        "carried_overnight": bool(trade.get("carried_overnight", False)),
        "holding_seconds": trade.get("holding_seconds"),
        "holding_display": trade.get("holding_display") or "—",
        "exit_reason": trade.get("exit_reason") or "unspecified",
        "gross_pnl": round(float(trade.get("gross_pnl") or 0.0), 2),
        "commission": comm,
        "slippage_total": round(float(trade.get("slippage_total") or 0.0), 2),
        "net_pnl": round(float(trade.get("net_pnl") or 0.0), 2),
        "is_winner": bool(trade.get("is_winner", False)),
        "reconcile_ok": bool(trade.get("reconcile_ok", True)),
        "reconcile_diff": round(float(trade.get("reconcile_diff") or 0.0), 2),
        "persisted": bool(trade.get("persisted", False)),
    }


def build_attribution_tree(
    *,
    book_mode: str,
    start_d: date,
    end_d: date,
    today_ist: date,
    selected_strategy: Optional[str],
    available_strategies: list[str],
    trades: list[dict[str, Any]],
    active_instances: list[dict[str, Any]],
    runner_lookup: Any,
    duplicates_collapsed: int = 0,
    inline_trade_limit: int = DEFAULT_INLINE_TRADE_LIMIT,
) -> dict[str, Any]:
    """Assemble the 3-level Strategy → Spawned Instance → Trade tree."""
    includes_today = start_d <= today_ist <= end_d

    # Index active runners by 8-char prefix and full instance_id
    active_by_prefix: dict[str, dict[str, Any]] = {}
    active_by_iid: dict[str, dict[str, Any]] = {}
    for rs in active_instances:
        iid = str(rs.get("instance_id") or "").strip()
        if not iid:
            continue
        active_by_iid[iid] = rs
        active_by_prefix[iid[:8].lower()] = rs

    # Bucket closed trades into instance groups
    inst_buckets: dict[str, dict[str, Any]] = {}
    for t in trades:
        strat = str(t.get("strategy") or "unassigned").strip() or "unassigned"
        prefix = t.get("instance_prefix")
        pid = t.get("portfolio_id")
        rid = t.get("runner_id")
        rname = t.get("runner_name")
        ikey = make_instance_key(
            strat,
            instance_prefix=prefix,
            portfolio_id=pid,
            runner_id=rid,
            runner_name=rname,
        )
        grp = inst_buckets.get(ikey)
        if grp is None:
            matched_active = (
                (active_by_iid.get(str(rid)) if rid else None)
                or (active_by_prefix.get(str(prefix).lower()) if prefix else None)
            )
            grp = {
                "instance_key": ikey,
                "strategy_name": strat,
                "instance_prefix": prefix or (
                    str(matched_active.get("instance_id"))[:8].lower()
                    if matched_active and matched_active.get("instance_id")
                    else None
                ),
                "portfolio_id": pid,
                "runner_name": rname or (matched_active or {}).get("name") or strat,
                "allocated_capital": t.get("allocated_capital"),
                "active_state": matched_active,
                "trades": [],
            }
            inst_buckets[ikey] = grp
        else:
            if not grp["portfolio_id"] and pid:
                grp["portfolio_id"] = pid
            if grp["allocated_capital"] is None and t.get("allocated_capital") is not None:
                grp["allocated_capital"] = t.get("allocated_capital")
        grp["trades"].append(t)

    # On IST today, ensure active runners with open positions / running MTM appear
    # even when they have 0 closed trades so far today!
    if includes_today:
        for rs in active_instances:
            strat = str(rs.get("strategy_name") or "unassigned").strip() or "unassigned"
            if selected_strategy and strat != selected_strategy:
                continue
            iid = str(rs.get("instance_id") or "").strip()
            prefix = iid[:8].lower() if iid else None
            ikey = make_instance_key(
                strat,
                instance_prefix=prefix,
                runner_id=iid,
                runner_name=rs.get("name"),
            )
            open_pnl = float(rs.get("open_pnl") or 0.0)
            raw_open = rs.get("open_positions")
            open_cnt = (
                raw_open
                if isinstance(raw_open, int)
                else len(raw_open) if isinstance(raw_open, (list, tuple)) else 0
            )
            if ikey not in inst_buckets and (open_cnt > 0 or abs(open_pnl) > 0.005):
                inst_buckets[ikey] = {
                    "instance_key": ikey,
                    "strategy_name": strat,
                    "instance_prefix": prefix,
                    "portfolio_id": None,
                    "runner_name": rs.get("name") or strat,
                    "allocated_capital": rs.get("allocated_capital"),
                    "active_state": rs,
                    "trades": [],
                }
            elif ikey in inst_buckets and inst_buckets[ikey]["active_state"] is None:
                inst_buckets[ikey]["active_state"] = rs

    # Materialize Level-2 Instance nodes grouped by Strategy
    by_strategy: dict[str, list[dict[str, Any]]] = {}
    for ikey, grp in inst_buckets.items():
        strat = grp["strategy_name"]
        active_state = grp["active_state"]
        active_iid = (
            str(active_state.get("instance_id"))
            if active_state and active_state.get("instance_id")
            else None
        )
        runner_obj = runner_lookup(active_iid) if (active_iid and callable(runner_lookup)) else None
        is_active = active_state is not None

        inst_mtm = 0.0
        inst_open_cnt = 0
        open_rows: list[dict[str, Any]] = []
        if includes_today and active_state is not None:
            inst_mtm = float(active_state.get("open_pnl") or 0.0)
            raw_open = active_state.get("open_positions")
            inst_open_cnt = (
                raw_open
                if isinstance(raw_open, int)
                else len(raw_open) if isinstance(raw_open, (list, tuple)) else 0
            )
            open_rows = extract_open_position_rows(runner_obj, active_state)
            if not inst_open_cnt and open_rows:
                inst_open_cnt = len(open_rows)

        # Sort closed trades newest exit first for operator drilldown
        sorted_trades = sorted(
            grp["trades"],
            key=lambda r: (r.get("exit_ts") or "", r.get("symbol") or ""),
            reverse=True,
        )
        metrics = compute_rollup_metrics(
            sorted_trades,
            includes_today=includes_today,
            running_mtm=inst_mtm,
            open_count=inst_open_cnt,
        )

        prefix = grp["instance_prefix"]
        clean_name = str(grp["runner_name"] or strat).strip()
        display_name = f"{clean_name} [{prefix}]" if prefix else clean_name

        symbols = (
            list(active_state.get("symbols") or [])
            if active_state and active_state.get("symbols")
            else sorted({str(t.get("symbol")) for t in sorted_trades if t.get("symbol")})
        )
        alloc_cap = (
            round(float(active_state["allocated_capital"]), 2)
            if active_state and active_state.get("allocated_capital") is not None
            else grp["allocated_capital"]
        )

        truncated = len(sorted_trades) > inline_trade_limit
        inline_rows = [
            format_public_trade_row(t)
            for t in (sorted_trades[:inline_trade_limit] if truncated else sorted_trades)
        ]

        inst_node = {
            "instance_key": ikey,
            "instance_id": active_iid,
            "instance_prefix": prefix,
            "portfolio_id": grp["portfolio_id"],
            "name": clean_name,
            "display_name": display_name,
            "strategy_name": strat,
            "is_active": is_active,
            "lifecycle_status": _resolve_lifecycle_status(active_state),
            "symbols": symbols[:8],
            "symbol_count": len(symbols),
            "timeframe": (active_state or {}).get("timeframe"),
            "allocated_capital": alloc_cap,
            "config_badges": extract_runner_config_badges(runner_obj, active_state),
            **metrics,
            "open_positions": open_rows,
            "trades": inline_rows,
            "trades_truncated": truncated,
        }
        by_strategy.setdefault(strat, []).append(inst_node)

    # Materialize Level-1 Strategy nodes
    strategies_out: list[dict[str, Any]] = []
    all_closed_trades: list[dict[str, Any]] = []
    total_mtm = 0.0
    total_open_cnt = 0
    total_active_inst = 0
    total_retired_inst = 0

    for strat_name, inst_list in by_strategy.items():
        # Sort instances by net_pnl ascending (worst bleeders surfaced immediately)
        inst_list.sort(key=lambda x: (x["net_pnl"], -(x["trade_count"])))
        strat_trades: list[dict[str, Any]] = []
        for inst in inst_list:
            strat_trades.extend(inst_buckets[inst["instance_key"]]["trades"])

        all_closed_trades.extend(strat_trades)
        strat_mtm = round(sum(float(i["running_mtm"] or 0.0) for i in inst_list), 2)
        strat_open = sum(int(i["open_count"] or 0) for i in inst_list)
        active_cnt = sum(1 for i in inst_list if i["is_active"])
        retired_cnt = len(inst_list) - active_cnt

        total_mtm = round(total_mtm + strat_mtm, 2)
        total_open_cnt += strat_open
        total_active_inst += active_cnt
        total_retired_inst += retired_cnt

        s_metrics = compute_rollup_metrics(
            strat_trades,
            includes_today=includes_today,
            running_mtm=strat_mtm,
            open_count=strat_open,
        )
        strategies_out.append(
            {
                "strategy_name": strat_name,
                "instance_count": len(inst_list),
                "active_instance_count": active_cnt,
                "retired_instance_count": retired_cnt,
                **s_metrics,
                "instances": inst_list,
            }
        )

    # Default sort: worst net_pnl first so losses/bleeders jump out immediately
    strategies_out.sort(key=lambda s: (s["net_pnl"], -(s["trade_count"])))

    max_abs_strat_net = max(
        (abs(float(s["net_pnl"])) for s in strategies_out),
        default=0.0,
    )
    for s in strategies_out:
        s["pnl_bar_ratio"] = (
            round(abs(float(s["net_pnl"])) / max_abs_strat_net, 4)
            if max_abs_strat_net > 0
            else 0.0
        )

    totals = compute_rollup_metrics(
        all_closed_trades,
        includes_today=includes_today,
        running_mtm=total_mtm,
        open_count=total_open_cnt,
    )
    totals.update(
        {
            "strategy_count": len(strategies_out),
            "instance_count": total_active_inst + total_retired_inst,
            "active_instance_count": total_active_inst,
            "retired_instance_count": total_retired_inst,
        }
    )

    return {
        "mode": book_mode,
        "from_date": start_d.isoformat(),
        "to_date": end_d.isoformat(),
        "today_ist": today_ist.isoformat(),
        "includes_today": includes_today,
        "selected_strategy": selected_strategy or "all",
        "available_strategies": available_strategies,
        "max_abs_strategy_net": round(max_abs_strat_net, 2),
        "totals": totals,
        "strategies": strategies_out,
        "duplicates_collapsed": int(duplicates_collapsed),
    }
