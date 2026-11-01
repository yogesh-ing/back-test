"""Slice 6 — Backtest-vs-Paper Reality Matrix & Constrained Live Roster Selector.

Solves the multi-runner portfolio construction problem when many Paper instances
simultaneously pass individual 6-gate graduation after 30–40 sessions, under
real prop-desk constraints:

1. **Composite Capital-Efficiency Ranking (`0..100`)**: ranks passing instances
   by Drawdown-Adjusted Return (`Net P&L / Max DD`), Net Profit Factor cushion,
   Commission-Drag efficiency (`1 - Drag`), and Backtest-to-Paper Sim-to-Real
   retention — never by raw rupee profit alone.
2. **Backtest-vs-Paper Sim-to-Real Matcher**: matches each instance against
   persisted ``BacktestRun`` records (`(strategy_id, underlying, timeframe)`),
   flagging curve-fit haircuts (`CURVE_FIT_HAIRCUT`) vs true retained edge
   (`RETENTION_STRONG`).
3. **Same-Strategy Multi-Timeframe Pruning**: prevents secretly doubling risk
   by running e.g. `3m`, `5m`, and `15m` of the same `(strategy, underlying)`
   together unless intraday-vs-swing horizons are genuinely uncorrelated.
4. **Daily IST Net P&L De-Correlation**: computes pairwise Pearson correlation
   across IST session dates (`min_overlap_days = 5`) and skips hidden clones
   whose correlation with an already-selected Live runner exceeds `max_correlation`.
5. **Hard Capital Reserve & Per-Underlying Concentration Caps**: enforces a
   cash/margin reserve buffer (`reserve_pct`), a per-underlying exposure cap
   (`max_underlying_pct`), and a maximum concurrent Live runner slot count
   (`max_live_runners`), slotting runners into **Wave 1 / Wave 2 Live Roster**
   vs **Warm Paper Bench**.

Read-only analytical module: never mutates runner state or places orders.
"""

from __future__ import annotations

import math
import re
from datetime import date, timedelta
from typing import Any, Mapping, Optional, Sequence

DEFAULT_LIVE_CAPITAL_INR = 300_000.0
DEFAULT_RUNNER_CAPITAL_FALLBACK_INR = 50_000.0
DEFAULT_RESERVE_PCT = 20.0
DEFAULT_MAX_UNDERLYING_PCT = 40.0
DEFAULT_MAX_CORRELATION = 0.65
DEFAULT_MAX_LIVE_RUNNERS = 5
MIN_CORRELATION_OVERLAP_DAYS = 5

_INDEX_ROOTS: tuple[tuple[str, str], ...] = (
    ("MIDCPNIFTY", "MIDCPNIFTY"),
    ("BANKNIFTY", "BANKNIFTY"),
    ("NIFTY BANK", "BANKNIFTY"),
    ("FINNIFTY", "FINNIFTY"),
    ("NIFTY FIN", "FINNIFTY"),
    ("SENSEX", "SENSEX"),
    ("BANKEX", "BANKEX"),
    ("NIFTY", "NIFTY"),
)

_OPTION_SUFFIX_RE = re.compile(
    r"^([A-Z&]+?)(?:\d{2}[A-Z]{3}\d+|\d{5,})(?:CE|PE|FUT)?$",
    re.IGNORECASE,
)

_INTRADAY_FAST_TFS = frozenset({"1m", "1min", "3m", "3min", "5m", "5min"})
_SWING_SLOW_TFS = frozenset({"1h", "1hour", "60m", "60min", "4h", "1d", "1day", "day"})
_TF_IN_LABEL_RE = re.compile(
    r"\b(1min|3min|5min|10min|15min|30min|60min|1hour|1day|1m|3m|5m|10m|15m|30m|1h|4h|1d)\b",
    re.IGNORECASE,
)


def canonical_underlying(raw_symbol: Any) -> str:
    """Extract canonical root underlying (`NIFTY`, `BANKNIFTY`, `RELIANCE`, etc.)."""
    s = str(raw_symbol or "").strip().upper()
    if not s:
        return "UNSPECIFIED"
    if "|" in s:
        s = s.split("|", 1)[-1].strip()
    if ":" in s:
        s = s.split(":", 1)[-1].strip()
    for suf in ("-EQ", ".NS", ".BO", " SPOT", " INDEX"):
        if s.endswith(suf):
            s = s[: -len(suf)].strip()

    for token, canonical in _INDEX_ROOTS:
        if s == token or s.startswith(token):
            return canonical

    m = _OPTION_SUFFIX_RE.match(s.replace(" ", ""))
    if m:
        root = m.group(1).upper()
        for token, canonical in _INDEX_ROOTS:
            if root.startswith(token):
                return canonical
        return root

    cleaned = s.split()[0].strip()
    return cleaned or "UNSPECIFIED"


def resolve_instance_underlyings(
    symbols: Sequence[Any],
    trades: Sequence[Mapping[str, Any]] = (),
) -> list[str]:
    """Return ordered unique canonical underlyings touched by an instance."""
    seen: list[str] = []
    for raw in symbols or ():
        u = canonical_underlying(raw)
        if u != "UNSPECIFIED" and u not in seen:
            seen.append(u)
    for t in trades or ():
        u = canonical_underlying(t.get("symbol"))
        if u != "UNSPECIFIED" and u not in seen:
            seen.append(u)
    return seen or ["UNSPECIFIED"]


def normalize_timeframe_token(raw_tf: Any) -> str:
    """Normalize timeframe strings (`'5min'` / `'5m'` -> `'5min'`, `'1day'` -> `'1day'`)."""
    t = str(raw_tf or "").strip().lower()
    aliases = {
        "1m": "1min",
        "3m": "3min",
        "5m": "5min",
        "10m": "10min",
        "15m": "15min",
        "30m": "30min",
        "60m": "1hour",
        "60min": "1hour",
        "1h": "1hour",
        "1d": "1day",
        "d": "1day",
        "day": "1day",
    }
    return aliases.get(t, t)


def resolve_instance_timeframe(raw_tf: Any, instance_label: Any = None) -> str:
    """Resolve normalized timeframe from explicit field or instance label hint."""
    norm = normalize_timeframe_token(raw_tf)
    if norm:
        return norm
    m = _TF_IN_LABEL_RE.search(str(instance_label or ""))
    return normalize_timeframe_token(m.group(1)) if m else ""


def are_timeframes_orthogonal(tf_a: Any, tf_b: Any) -> bool:
    """Return True only when one timeframe is fast scalp (`<=5m`) and one is swing (`>=1h`)."""
    a = normalize_timeframe_token(tf_a)
    b = normalize_timeframe_token(tf_b)
    if not a or not b or a == b:
        return False
    return (a in _INTRADAY_FAST_TFS and b in _SWING_SLOW_TFS) or (
        b in _INTRADAY_FAST_TFS and a in _SWING_SLOW_TFS
    )


def match_backtest_benchmark(
    *,
    strategy_name: str,
    primary_underlying: str,
    timeframe: Any,
    backtest_runs: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Match a Paper/Live instance against stored BacktestRun benchmarks."""
    strat = str(strategy_name or "").strip().lower()
    und = canonical_underlying(primary_underlying)
    tf_norm = normalize_timeframe_token(timeframe)

    same_strat = [
        r for r in backtest_runs
        if str(r.get("strategy_id") or "").strip().lower() == strat
    ]
    if not same_strat:
        return {"matched": False, "match_tier": None}

    def _engine_rank(row: Mapping[str, Any]) -> tuple[int, str]:
        eng = str(row.get("engine") or "")
        return (1 if eng == "backtest_driver" else 0, str(row.get("created_at") or ""))

    exact = [
        r for r in same_strat
        if canonical_underlying(r.get("symbol")) == und
        and normalize_timeframe_token(r.get("timeframe")) == tf_norm
    ]
    if exact:
        best = max(exact, key=_engine_rank)
        tier = "exact"
    else:
        same_sym = [
            r for r in same_strat
            if canonical_underlying(r.get("symbol")) == und
        ]
        if same_sym:
            best = max(same_sym, key=_engine_rank)
            tier = "same_symbol_diff_tf"
        else:
            best = max(same_strat, key=_engine_rank)
            tier = "strategy_only"

    return {
        "matched": True,
        "match_tier": tier,
        "run_id": best.get("run_id"),
        "symbol": best.get("symbol"),
        "timeframe": best.get("timeframe"),
        "engine": best.get("engine"),
        "date_from": best.get("date_from"),
        "date_to": best.get("date_to"),
        "profit_factor": best.get("profit_factor"),
        "win_rate_pct": best.get("win_rate_pct"),
        "max_drawdown_pct": best.get("max_drawdown_pct"),
        "sharpe": best.get("sharpe"),
        "total_return_pct": best.get("total_return_pct"),
        "total_trades": best.get("total_trades"),
        "monte_carlo_profit_prob_pct": best.get("monte_carlo_profit_prob_pct"),
    }


def evaluate_sim_to_real(
    *,
    paper_pf: Optional[float],
    paper_unbounded_pf: bool,
    paper_win_rate_pct: float,
    paper_max_dd_pct: Optional[float],
    benchmark: Mapping[str, Any],
) -> dict[str, Any]:
    """Compare Paper realization against matched Backtest benchmark."""
    if not benchmark.get("matched"):
        return {
            "has_backtest": False,
            "match_tier": None,
            "status": "NO_BACKTEST",
            "status_label": "Paper-Only (No Saved BT)",
            "status_tone": "neutral",
            "pf_retention_pct": None,
            "win_rate_drift_pp": None,
            "dd_inflation_ratio": None,
            "retention_unit_score": 0.75,
            "benchmark": None,
        }

    bt_pf = benchmark.get("profit_factor")
    bt_wr = benchmark.get("win_rate_pct")
    bt_dd = benchmark.get("max_drawdown_pct")
    bt_trades = int(benchmark.get("total_trades") or 0)
    bt_mc = benchmark.get("monte_carlo_profit_prob_pct")

    eff_paper_pf = 3.0 if paper_unbounded_pf else (float(paper_pf) if paper_pf is not None else 0.0)
    pf_retention_pct: Optional[float] = None
    if bt_pf is not None and float(bt_pf) > 0.05:
        pf_retention_pct = round((eff_paper_pf / float(bt_pf)) * 100.0, 1)

    wr_drift: Optional[float] = None
    if bt_wr is not None:
        wr_drift = round(float(paper_win_rate_pct) - float(bt_wr), 1)

    dd_ratio: Optional[float] = None
    if paper_max_dd_pct is not None and bt_dd is not None and float(bt_dd) > 0.1:
        dd_ratio = round(float(paper_max_dd_pct) / float(bt_dd), 2)

    # Classify Sim-to-Real regime
    if (
        bt_pf is not None
        and float(bt_pf) >= 1.50
        and pf_retention_pct is not None
        and pf_retention_pct < 55.0
    ):
        status = "CURVE_FIT_HAIRCUT"
        label = f"Curve-Fit Haircut ({pf_retention_pct:.0f}% PF)"
        tone = "negative"
        unit_score = max(0.10, pf_retention_pct / 100.0)
    elif (
        pf_retention_pct is not None
        and pf_retention_pct > 150.0
        and bt_trades >= 20
    ):
        status = "REGIME_TAILWIND"
        label = f"Regime Tailwind ({pf_retention_pct:.0f}% PF)"
        tone = "caution"
        unit_score = 0.85
    elif (
        pf_retention_pct is not None
        and pf_retention_pct >= 65.0
        and (wr_drift is None or wr_drift >= -10.0)
    ):
        status = "RETENTION_STRONG"
        label = f"Retained ({pf_retention_pct:.0f}% PF)"
        tone = "positive"
        unit_score = min(1.0, 0.75 + 0.25 * min(1.0, pf_retention_pct / 90.0))
    else:
        status = "MODERATE_DRIFT"
        label = (
            f"Moderate Haircut ({pf_retention_pct:.0f}% PF)"
            if pf_retention_pct is not None
            else "Matched BT"
        )
        tone = "caution"
        unit_score = 0.60

    # Modulate slightly by Monte Carlo P(Profit) when available
    if bt_mc is not None:
        if float(bt_mc) < 55.0:
            unit_score = max(0.10, unit_score - 0.15)
        elif float(bt_mc) >= 75.0:
            unit_score = min(1.0, unit_score + 0.05)

    return {
        "has_backtest": True,
        "match_tier": benchmark.get("match_tier"),
        "status": status,
        "status_label": label,
        "status_tone": tone,
        "pf_retention_pct": pf_retention_pct,
        "win_rate_drift_pp": wr_drift,
        "dd_inflation_ratio": dd_ratio,
        "retention_unit_score": round(unit_score, 4),
        "benchmark": {
            "run_id": benchmark.get("run_id"),
            "symbol": benchmark.get("symbol"),
            "timeframe": benchmark.get("timeframe"),
            "engine": benchmark.get("engine"),
            "date_from": benchmark.get("date_from"),
            "date_to": benchmark.get("date_to"),
            "profit_factor": round(float(bt_pf), 2) if bt_pf is not None else None,
            "win_rate_pct": round(float(bt_wr), 1) if bt_wr is not None else None,
            "max_drawdown_pct": round(float(bt_dd), 2) if bt_dd is not None else None,
            "sharpe": (
                round(float(benchmark["sharpe"]), 2)
                if benchmark.get("sharpe") is not None
                else None
            ),
            "total_return_pct": (
                round(float(benchmark["total_return_pct"]), 2)
                if benchmark.get("total_return_pct") is not None
                else None
            ),
            "total_trades": bt_trades,
            "monte_carlo_profit_prob_pct": (
                round(float(bt_mc), 1) if bt_mc is not None else None
            ),
        },
    }


def build_daily_net_pnl_map(trades: Sequence[Mapping[str, Any]]) -> dict[str, float]:
    """Aggregate an instance's closed trades into `{session_date_ist: net_pnl}`."""
    daily: dict[str, float] = {}
    for t in trades or ():
        raw_day = t.get("session_date") or t.get("session_date_ist") or ""
        day = str(raw_day).strip()[:10]
        if not day:
            continue
        daily[day] = round(daily.get(day, 0.0) + float(t.get("net_pnl") or 0.0), 4)
    return daily


def compute_daily_pnl_correlation(
    daily_a: Mapping[str, float],
    daily_b: Mapping[str, float],
    *,
    all_session_days: Sequence[str],
    min_overlap_days: int = MIN_CORRELATION_OVERLAP_DAYS,
) -> Optional[float]:
    """Compute Pearson correlation across active IST session days (`None` if sparse)."""
    active_union = [
        d for d in all_session_days
        if abs(daily_a.get(d, 0.0)) > 1e-9 or abs(daily_b.get(d, 0.0)) > 1e-9
    ]
    if len(active_union) < min_overlap_days:
        return None
    if len(daily_a) < 2 or len(daily_b) < 2:
        return None

    xs = [float(daily_a.get(d, 0.0)) for d in active_union]
    ys = [float(daily_b.get(d, 0.0)) for d in active_union]
    n = len(xs)
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    cov = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    var_x = sum((x - mean_x) ** 2 for x in xs)
    var_y = sum((y - mean_y) ** 2 for y in ys)
    denom = math.sqrt(var_x * var_y)
    if denom <= 1e-9:
        return None
    corr = max(-1.0, min(1.0, cov / denom))
    return round(corr, 3)


def compute_composite_rank_score(
    *,
    net_pnl: float,
    max_drawdown_inr: float,
    required_capital: float,
    profit_factor: Optional[float],
    unbounded_pf: bool,
    commission_drag_pct: Optional[float],
    trade_count: int,
    retention_unit_score: float,
) -> dict[str, float]:
    """Compute 0..100 institutional capital-efficiency & edge score."""
    # 1. Return-over-Drawdown Efficiency (0..40 pts)
    dd_floor = max(float(max_drawdown_inr or 0.0), max(500.0, 0.02 * float(required_capital)))
    calmar_like = max(0.0, float(net_pnl) / dd_floor) if net_pnl > 0 else 0.0
    eff_pts = min(40.0, (calmar_like / 5.0) * 40.0)

    # 2. Net Profit Factor Cushion (0..25 pts)
    eff_pf = 3.0 if (unbounded_pf and net_pnl > 0) else float(profit_factor or 0.0)
    if eff_pf <= 1.0:
        pf_pts = 0.0
    else:
        pf_pts = min(25.0, ((eff_pf - 1.0) / 1.5) * 25.0)

    # 3. Friction Safety (0..15 pts; 0% drag -> 15, 60%+ drag -> 0)
    drag = float(commission_drag_pct) if commission_drag_pct is not None else 100.0
    if net_pnl <= 0:
        drag_pts = 0.0
    else:
        drag_pts = max(0.0, min(15.0, (1.0 - min(drag, 60.0) / 60.0) * 15.0))

    # 4. Sim-to-Real Honesty & Sample Confidence (0..20 pts)
    sample_factor = min(1.0, max(0.0, float(trade_count) / 30.0))
    honesty_pts = 20.0 * (0.65 * float(retention_unit_score) + 0.35 * sample_factor)

    total = round(eff_pts + pf_pts + drag_pts + honesty_pts, 1)
    return {
        "composite_score": total,
        "efficiency_pts": round(eff_pts, 1),
        "pf_pts": round(pf_pts, 1),
        "friction_pts": round(drag_pts, 1),
        "honesty_pts": round(honesty_pts, 1),
        "return_over_dd": round(calmar_like, 2),
    }


def _enumerate_weekdays(start_d: date, end_d: date) -> list[str]:
    days: list[str] = []
    cur = start_d
    while cur <= end_d:
        if cur.weekday() < 5:
            days.append(cur.isoformat())
        cur += timedelta(days=1)
    return days


def validate_roster_constraints(
    *,
    live_capital: Any = None,
    reserve_pct: Any = None,
    max_underlying_pct: Any = None,
    max_correlation: Any = None,
    max_live_runners: Any = None,
    dedupe_timeframes: Any = True,
    default_runner_capital: Any = None,
) -> dict[str, Any]:
    """Validate and clamp operator portfolio-construction parameters."""
    cap = float(live_capital) if live_capital not in (None, "") else DEFAULT_LIVE_CAPITAL_INR
    if cap <= 0:
        raise ValueError("live_capital must be > 0")

    res_pct = float(reserve_pct) if reserve_pct not in (None, "") else DEFAULT_RESERVE_PCT
    if not (0.0 <= res_pct <= 80.0):
        raise ValueError("reserve_pct must be between 0 and 80")

    und_pct = (
        float(max_underlying_pct)
        if max_underlying_pct not in (None, "")
        else DEFAULT_MAX_UNDERLYING_PCT
    )
    if not (10.0 <= und_pct <= 100.0):
        raise ValueError("max_underlying_pct must be between 10 and 100")

    corr_cap = (
        float(max_correlation)
        if max_correlation not in (None, "")
        else DEFAULT_MAX_CORRELATION
    )
    if not (0.10 <= corr_cap <= 1.0):
        raise ValueError("max_correlation must be between 0.10 and 1.00")

    slots = (
        int(max_live_runners)
        if max_live_runners not in (None, "")
        else DEFAULT_MAX_LIVE_RUNNERS
    )
    if not (1 <= slots <= 25):
        raise ValueError("max_live_runners must be between 1 and 25")

    fallback_cap = (
        float(default_runner_capital)
        if default_runner_capital not in (None, "")
        else min(DEFAULT_RUNNER_CAPITAL_FALLBACK_INR, round(cap * 0.20, 2))
    )
    fallback_cap = max(5_000.0, fallback_cap)

    if isinstance(dedupe_timeframes, str):
        dedupe_tf = dedupe_timeframes.strip().lower() not in ("0", "false", "no", "off")
    else:
        dedupe_tf = bool(dedupe_timeframes)

    deployable_budget = round(cap * (1.0 - res_pct / 100.0), 2)
    reserve_inr = round(cap - deployable_budget, 2)
    max_underlying_inr = round(cap * (und_pct / 100.0), 2)

    return {
        "live_capital": round(cap, 2),
        "reserve_pct": round(res_pct, 1),
        "reserve_inr": reserve_inr,
        "deployable_budget": deployable_budget,
        "max_underlying_pct": round(und_pct, 1),
        "max_underlying_inr": max_underlying_inr,
        "max_correlation": round(corr_cap, 2),
        "max_live_runners": slots,
        "dedupe_timeframes": dedupe_tf,
        "default_runner_capital": round(fallback_cap, 2),
    }


def build_live_roster_plan(
    *,
    book_mode: str,
    start_d: date,
    end_d: date,
    graduation_scorecard: Mapping[str, Any],
    attribution_tree: Mapping[str, Any],
    backtest_runs: Sequence[Mapping[str, Any]] = (),
    live_capital: Any = None,
    reserve_pct: Any = None,
    max_underlying_pct: Any = None,
    max_correlation: Any = None,
    max_live_runners: Any = None,
    dedupe_timeframes: Any = True,
    default_runner_capital: Any = None,
) -> dict[str, Any]:
    """Run the 5-stage constrained Live Roster Selection Tournament."""
    cfg = validate_roster_constraints(
        live_capital=live_capital,
        reserve_pct=reserve_pct,
        max_underlying_pct=max_underlying_pct,
        max_correlation=max_correlation,
        max_live_runners=max_live_runners,
        dedupe_timeframes=dedupe_timeframes,
        default_runner_capital=default_runner_capital,
    )

    # Index full per-instance trade lists from attribution_tree for daily correlation
    trades_by_key: dict[str, list[Mapping[str, Any]]] = {}
    for strat_node in attribution_tree.get("strategies") or ():
        for inst in strat_node.get("instances") or ():
            ikey = str(inst.get("instance_key") or "")
            if ikey:
                trades_by_key[ikey] = list(inst.get("trades") or ())

    session_days = _enumerate_weekdays(start_d, end_d)
    grad_rows = list(graduation_scorecard.get("instances") or ())

    enriched: list[dict[str, Any]] = []
    daily_maps: dict[str, dict[str, float]] = {}

    for row in grad_rows:
        ikey = str(row.get("instance_key") or "")
        inst_trades = trades_by_key.get(ikey, [])
        daily_map = build_daily_net_pnl_map(inst_trades)
        daily_maps[ikey] = daily_map

        underlyings = resolve_instance_underlyings(row.get("symbols") or (), inst_trades)
        primary_underlying = underlyings[0]
        resolved_tf = resolve_instance_timeframe(
            row.get("timeframe"), row.get("instance_label")
        )

        alloc = row.get("allocated_capital")
        req_cap = (
            round(float(alloc), 2)
            if (alloc is not None and float(alloc) > 0)
            else cfg["default_runner_capital"]
        )
        capital_is_estimated = not (alloc is not None and float(alloc) > 0)

        bt_match = match_backtest_benchmark(
            strategy_name=str(row.get("strategy") or ""),
            primary_underlying=primary_underlying,
            timeframe=resolved_tf,
            backtest_runs=backtest_runs,
        )
        sim_to_real = evaluate_sim_to_real(
            paper_pf=row.get("profit_factor"),
            paper_unbounded_pf=bool(row.get("unbounded_pf")),
            paper_win_rate_pct=float(row.get("win_rate_pct") or 0.0),
            paper_max_dd_pct=row.get("max_drawdown_pct"),
            benchmark=bt_match,
        )

        score_parts = compute_composite_rank_score(
            net_pnl=float(row.get("net_pnl") or 0.0),
            max_drawdown_inr=float(row.get("max_drawdown_inr") or 0.0),
            required_capital=req_cap,
            profit_factor=row.get("profit_factor"),
            unbounded_pf=bool(row.get("unbounded_pf")),
            commission_drag_pct=row.get("commission_drag_pct"),
            trade_count=int(row.get("trade_count") or 0),
            retention_unit_score=float(sim_to_real["retention_unit_score"]),
        )

        enriched.append(
            {
                **row,
                "timeframe": row.get("timeframe") or (resolved_tf or None),
                "primary_underlying": primary_underlying,
                "underlyings": underlyings,
                "required_capital": req_cap,
                "capital_is_estimated": capital_is_estimated,
                "sim_to_real": sim_to_real,
                "score": score_parts,
                "composite_score": score_parts["composite_score"],
            }
        )

    # Sort eligible candidates ahead of disqualified, then by Composite Score DESC
    enriched.sort(
        key=lambda r: (
            0 if r.get("verdict") == "PROMOTE_CANDIDATE" else 1,
            -float(r["composite_score"]),
            -float(r.get("net_pnl") or 0.0),
            str(r.get("instance_label") or ""),
        )
    )

    selected: list[dict[str, Any]] = []
    underlying_allocated: dict[str, float] = {}
    total_allocated = 0.0

    candidates_out: list[dict[str, Any]] = []

    for rank_idx, cand in enumerate(enriched, start=1):
        ikey = str(cand.get("instance_key") or "")
        und = cand["primary_underlying"]
        strat_low = str(cand.get("strategy") or "").strip().lower()
        tf_norm = normalize_timeframe_token(cand.get("timeframe"))
        req_cap = float(cand["required_capital"])
        adv = cand.get("entitlement_advisory") or {}
        sim = cand["sim_to_real"]

        # Compute max correlation against currently selected roster
        max_corr_val: Optional[float] = None
        max_corr_peer: Optional[str] = None
        for sel in selected:
            sel_key = str(sel.get("instance_key") or "")
            corr = compute_daily_pnl_correlation(
                daily_maps.get(ikey, {}),
                daily_maps.get(sel_key, {}),
                all_session_days=session_days,
            )
            if corr is not None and (max_corr_val is None or corr > max_corr_val):
                max_corr_val = corr
                max_corr_peer = f"#{sel['roster_slot']} {sel.get('instance_label')}"

        # Stage 1: Gate + Sim-to-Real Haircut + Live Entitlement Qualification
        if cand.get("verdict") != "PROMOTE_CANDIDATE":
            decision = "DISQUALIFIED_GATES"
            decision_label = "Disqualified · Gate Fail"
            decision_tone = "negative"
            decision_reason = str(cand.get("rationale") or "Failed 6-gate graduation check.")
            roster_slot = None
            rollout_wave = None
        elif book_mode == "paper" and bool(adv.get("requires_live_entitlement_review")):
            decision = "HOLD_ENTITLEMENT_REVIEW"
            decision_label = "Hold · Entitlement Review"
            decision_tone = "caution"
            decision_reason = str(
                adv.get("message")
                or "Option structure requires explicit Live risk entitlement sign-off."
            )
            roster_slot = None
            rollout_wave = None
        elif (
            sim.get("status") == "CURVE_FIT_HAIRCUT"
            and sim.get("pf_retention_pct") is not None
            and float(sim["pf_retention_pct"]) < 45.0
        ):
            decision = "DISQUALIFIED_HAIRCUT"
            decision_label = "Reject · Severe BT Haircut"
            decision_tone = "negative"
            decision_reason = (
                f"Paper retained only {sim['pf_retention_pct']:.0f}% of Backtest PF "
                f"({sim['benchmark']['profit_factor']:.2f}× → "
                f"{cand.get('profit_factor') or 0:.2f}×)."
            )
            roster_slot = None
            rollout_wave = None
        else:
            # Stage 2: Same-Strategy + Same-Underlying Multi-Timeframe De-Duplication
            tf_conflict: Optional[dict[str, Any]] = None
            if cfg["dedupe_timeframes"]:
                for sel in selected:
                    same_pair = (
                        str(sel.get("strategy") or "").strip().lower() == strat_low
                        and sel["primary_underlying"] == und
                    )
                    if not same_pair:
                        continue
                    pair_corr = compute_daily_pnl_correlation(
                        daily_maps.get(ikey, {}),
                        daily_maps.get(str(sel.get("instance_key") or ""), {}),
                        all_session_days=session_days,
                    )
                    # Allow keeping both ONLY if fast-intraday vs slow-swing AND corr < 0.50
                    if are_timeframes_orthogonal(tf_norm, sel.get("timeframe")) and (
                        pair_corr is not None and pair_corr < 0.50
                    ):
                        continue
                    tf_conflict = sel
                    break

            if tf_conflict is not None:
                decision = "PRUNED_TIMEFRAME_DUPLICATE"
                decision_label = "Pruned · Same Strat+Symbol TF"
                decision_tone = "caution"
                decision_reason = (
                    f"Same ({cand.get('strategy')}, {und}) family already represented by "
                    f"#{tf_conflict['roster_slot']} {tf_conflict.get('instance_label')} "
                    f"({tf_conflict.get('timeframe') or 'default'}, "
                    f"score {tf_conflict['composite_score']:.1f})."
                )
                roster_slot = None
                rollout_wave = None
            # Stage 3: Daily Net P&L Correlation Pruning
            elif max_corr_val is not None and max_corr_val > cfg["max_correlation"]:
                decision = "PRUNED_CORRELATED_CLONE"
                decision_label = f"Pruned · Corr ρ={max_corr_val:+.2f}"
                decision_tone = "caution"
                decision_reason = (
                    f"Daily IST Net P&L correlation ρ={max_corr_val:+.2f} exceeds "
                    f"{cfg['max_correlation']:.2f} cap against {max_corr_peer}."
                )
                roster_slot = None
                rollout_wave = None
            # Stage 4: Underlying Concentration Cap
            elif (
                und != "UNSPECIFIED"
                and (underlying_allocated.get(und, 0.0) + req_cap)
                > cfg["max_underlying_inr"] + 0.01
            ):
                projected_pct = (
                    (underlying_allocated.get(und, 0.0) + req_cap) / cfg["live_capital"]
                ) * 100.0
                decision = "DEFERRED_UNDERLYING_CAP"
                decision_label = f"Warm Bench · {und} Cap"
                decision_tone = "neutral"
                decision_reason = (
                    f"Adding ₹{req_cap:,.0f} pushes {und} exposure to {projected_pct:.1f}% "
                    f"(cap {cfg['max_underlying_pct']:.0f}% = ₹{cfg['max_underlying_inr']:,.0f})."
                )
                roster_slot = None
                rollout_wave = None
            # Stage 5: Live Runner Slot & Deployable Capital Knapsack
            elif len(selected) >= cfg["max_live_runners"]:
                decision = "DEFERRED_SLOT_CAP"
                decision_label = "Warm Bench · Max Slots Full"
                decision_tone = "neutral"
                decision_reason = (
                    f"Top {cfg['max_live_runners']} Live Roster slots filled; "
                    f"keep warm in Paper as #1 rotation backup."
                )
                roster_slot = None
                rollout_wave = None
            elif (total_allocated + req_cap) > cfg["deployable_budget"] + 0.01:
                rem = max(0.0, cfg["deployable_budget"] - total_allocated)
                decision = "DEFERRED_CAPITAL_CAP"
                decision_label = "Warm Bench · Budget Reserve"
                decision_tone = "neutral"
                decision_reason = (
                    f"Requires ₹{req_cap:,.0f}, exceeding ₹{rem:,.0f} remaining deployable "
                    f"budget (₹{cfg['reserve_inr']:,.0f} reserve held)."
                )
                roster_slot = None
                rollout_wave = None
            else:
                roster_slot = len(selected) + 1
                rollout_wave = (
                    "Wave 1 · Deploy Week 1 (1× min lot)"
                    if roster_slot <= 2
                    else "Wave 2 · Add Week 3 after Live slippage check"
                )
                decision = "SELECTED_LIVE_ROSTER"
                decision_label = f"#{roster_slot} Live Roster"
                decision_tone = "positive"
                decision_reason = (
                    f"{rollout_wave} · Score {cand['composite_score']:.1f}/100 · "
                    f"Alloc ₹{req_cap:,.0f} "
                    f"({(req_cap / cfg['live_capital']) * 100.0:.1f}% of Live pool)."
                )
                total_allocated = round(total_allocated + req_cap, 2)
                underlying_allocated[und] = round(underlying_allocated.get(und, 0.0) + req_cap, 2)

        entry = {
            "rank": rank_idx,
            "roster_slot": roster_slot,
            "rollout_wave": rollout_wave,
            "decision": decision,
            "decision_label": decision_label,
            "decision_tone": decision_tone,
            "decision_reason": decision_reason,
            "instance_key": cand.get("instance_key"),
            "instance_id": cand.get("instance_id"),
            "portfolio_id": cand.get("portfolio_id"),
            "instance_label": cand.get("instance_label"),
            "strategy": cand.get("strategy"),
            "timeframe": cand.get("timeframe"),
            "primary_underlying": und,
            "underlyings": cand["underlyings"],
            "lifecycle_status": cand.get("lifecycle_status"),
            "portfolio_deep_link": cand.get("portfolio_deep_link"),
            "required_capital": req_cap,
            "capital_is_estimated": cand["capital_is_estimated"],
            "composite_score": cand["composite_score"],
            "score_breakdown": cand["score"],
            "sim_to_real": sim,
            "max_corr_with_selected": max_corr_val,
            "max_corr_peer": max_corr_peer,
            "gates_passed": cand.get("gates_passed"),
            "gates_total": cand.get("gates_total"),
            "verdict": cand.get("verdict"),
            "trade_count": cand.get("trade_count"),
            "win_rate_pct": cand.get("win_rate_pct"),
            "profit_factor": cand.get("profit_factor"),
            "unbounded_pf": cand.get("unbounded_pf"),
            "expectancy_net": cand.get("expectancy_net"),
            "gross_pnl": cand.get("gross_pnl"),
            "commission": cand.get("commission"),
            "net_pnl": cand.get("net_pnl"),
            "commission_drag_pct": cand.get("commission_drag_pct"),
            "max_drawdown_inr": cand.get("max_drawdown_inr"),
            "max_drawdown_pct": cand.get("max_drawdown_pct"),
        }
        if decision == "SELECTED_LIVE_ROSTER":
            selected.append(entry)
        candidates_out.append(entry)

    underlying_mix = [
        {
            "underlying": u,
            "allocated_inr": amt,
            "share_of_live_pct": round((amt / cfg["live_capital"]) * 100.0, 1),
        }
        for u, amt in sorted(underlying_allocated.items(), key=lambda kv: (-kv[1], kv[0]))
    ]

    gate_passers = sum(1 for c in candidates_out if c["verdict"] == "PROMOTE_CANDIDATE")
    pruned_count = sum(
        1
        for c in candidates_out
        if c["decision"] in ("PRUNED_TIMEFRAME_DUPLICATE", "PRUNED_CORRELATED_CLONE")
    )
    bench_count = sum(
        1
        for c in candidates_out
        if c["decision"] in ("DEFERRED_UNDERLYING_CAP", "DEFERRED_SLOT_CAP", "DEFERRED_CAPITAL_CAP")
    )
    disqualified_count = sum(
        1
        for c in candidates_out
        if c["decision"]
        in ("DISQUALIFIED_GATES", "DISQUALIFIED_HAIRCUT", "HOLD_ENTITLEMENT_REVIEW")
    )

    return {
        "mode": book_mode,
        "from_date": start_d.isoformat(),
        "to_date": end_d.isoformat(),
        "constraints": cfg,
        "summary": {
            "total_evaluated": len(candidates_out),
            "gate_passers": gate_passers,
            "selected_count": len(selected),
            "warm_bench_count": bench_count,
            "pruned_duplicate_or_corr_count": pruned_count,
            "disqualified_count": disqualified_count,
            "allocated_capital_inr": round(total_allocated, 2),
            "unallocated_deployable_inr": round(
                max(0.0, cfg["deployable_budget"] - total_allocated), 2
            ),
            "reserve_inr": cfg["reserve_inr"],
            "utilization_of_total_pct": round((total_allocated / cfg["live_capital"]) * 100.0, 1),
            "underlying_mix": underlying_mix,
            "backtest_runs_indexed": len(backtest_runs),
        },
        "selected_roster": selected,
        "candidates": candidates_out,
    }
