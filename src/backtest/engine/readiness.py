"""Certification readiness — the §5 traffic light.

The panel makes NO decision. It surfaces eight checks and says which passed,
which did not, and — the part that matters most — which could not be
evaluated at all. A user decides what to do with it.

**Advisory only.** Nothing here gates anything. The hard blocks live in
Optimize and in the Paper→Live gate, where they already are. This module has
no side effects, no writes, and no thresholds that can be tuned into an
approval.

Why server-side
---------------
The panel appears at the bottom of every Backtest result *and* per-strategy in
Compare. Computing it once here means the two surfaces cannot disagree, and
the thresholds live in one file instead of in a JS component and a Python
endpoint that drift apart.

The fourth state
----------------
§5 draws a green/yellow/red table. Real runs need a fourth state, and this is
the module's most important decision: **a check that could not be evaluated is
``unknown``, never green.**

The tempting shortcut is to let a missing input fall through to green — a
Quick-Screen run has no cost shock, so "no cost shock" could read as "no cost
problem". That is a green tick the user did not earn, on the single most
promotional panel on the page. Forcing it to red is no better: it double-penalises
Quick-Screen for one fact, once in the Engine row and again in the Cost-Shock
row, and makes the panel look like a verdict rather than a summary.

So ``unknown`` renders neutrally, carries the reason, and is excluded from
"passed". A run is only "all green" when every one of the eight checks was
genuinely evaluated and genuinely passed.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

__all__ = [
    "GREEN",
    "YELLOW",
    "RED",
    "UNKNOWN",
    "PROFIT_FACTOR_GREEN",
    "PROFIT_FACTOR_YELLOW",
    "MAX_DRAWDOWN_GREEN_PCT",
    "MAX_DRAWDOWN_YELLOW_PCT",
    "MONTE_CARLO_GREEN_PCT",
    "MONTE_CARLO_YELLOW_PCT",
    "build_readiness",
]

GREEN = "green"
YELLOW = "yellow"
RED = "red"
UNKNOWN = "unknown"

# §5 row thresholds, restated here so the engine and the panel cannot drift.
# The trade-count pair is imported from §2 rather than re-declared: the same
# 30/20 split already flags the metric block, and two different trade-count
# bands on one page would be a contradiction the user has to notice.
PROFIT_FACTOR_GREEN = 1.5
PROFIT_FACTOR_YELLOW = 1.0
MAX_DRAWDOWN_GREEN_PCT = 15.0
MAX_DRAWDOWN_YELLOW_PCT = 25.0
MONTE_CARLO_GREEN_PCT = 75.0
MONTE_CARLO_YELLOW_PCT = 50.0

#: Below this, the checks that depend on realised trades are reported as
#: unknown rather than given a number nobody should act on. Mirrors §2's
#: insufficient-sample threshold.
NO_EVIDENCE_TRADES = 1


def _num(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out and abs(out) != float("inf") else None


def _check(
    cid: str,
    label: str,
    status: str,
    value: str,
    detail: str = "",
) -> dict[str, Any]:
    return {"id": cid, "label": label, "status": status, "value": value, "detail": detail}


def _band(value: float, green: float, yellow: float) -> str:
    """Higher-is-better banding: >= green, >= yellow, else red."""
    if value >= green:
        return GREEN
    if value >= yellow:
        return YELLOW
    return RED


def _band_dd(value: float) -> str:
    """Drawdown bands, on ABSOLUTE depth.

    §5 writes "Max Drawdown < 15% / 15-25% / > 25%". The metric is reported
    negative, so a naive ``-12 < 15`` test would pass everything and a
    ``-30 < 15`` would also pass. The bands are read as depth.
    """
    depth = abs(value)
    if depth < MAX_DRAWDOWN_GREEN_PCT:
        return GREEN
    if depth <= MAX_DRAWDOWN_YELLOW_PCT:
        return YELLOW
    return RED


# ---------------------------------------------------------------------------
# The eight checks
# ---------------------------------------------------------------------------


def _engine(provenance: Mapping[str, Any]) -> dict[str, Any]:
    canonical = bool(provenance.get("engine_canonical"))
    label = str(provenance.get("engine_label") or "unknown engine")
    if canonical:
        return _check(
            "engine",
            "Engine",
            GREEN,
            label,
            "Fills are modelled exactly, so these numbers are reproducible.",
        )
    return _check(
        "engine",
        "Engine",
        RED,
        label,
        "Quick-Screen uses previous-close fills and an approximate cost "
        "model. Treat this as a screen, not as a result.",
    )


def _data_source(provenance: Mapping[str, Any]) -> dict[str, Any]:
    key = str(provenance.get("data_source") or "").strip().lower()
    label = str(provenance.get("data_source_label") or key or "unknown")
    if provenance.get("data_source_real"):
        return _check(
            "data_source", "Data source", GREEN, label, "Real broker or cached broker prices."
        )
    if key == "csv":
        return _check(
            "data_source",
            "Data source",
            YELLOW,
            label,
            "A supplied file. Confirm it was adjusted for splits and "
            "dividends before trusting the return.",
        )
    return _check(
        "data_source",
        "Data source",
        RED,
        label,
        "Synthetic prices. No result on synthetic data can be certified, "
        "however good the other rows look.",
    )


def _trade_count(metrics: Mapping[str, Any]) -> dict[str, Any]:
    closed = int(_num(metrics.get("closed_trades")) or 0)
    from backtest.engine.metrics_risk import TRADE_COUNT_OK, TRADE_COUNT_WARN

    if closed >= TRADE_COUNT_OK:
        status, detail = GREEN, "Enough closed trades for the estimates to mean something."
    elif closed >= TRADE_COUNT_WARN:
        status = YELLOW
        detail = (
            f"{TRADE_COUNT_OK} is the point where these statistics settle; below it "
            f"the Sharpe error bar is wider than the numbers."
        )
    else:
        status = RED
        detail = (
            "Fewer than "
            f"{TRADE_COUNT_WARN} closed trades. Every percentage in this result is "
            "a handful of outcomes wearing a decimal place."
        )
    return _check("trade_count", f"Trade count ({closed})", status, str(closed), detail)


def _beats_benchmark(benchmark: Mapping[str, Any] | None) -> dict[str, Any]:
    bench = benchmark or {}
    if not bench.get("available"):
        return _check(
            "beats_benchmark",
            "Beats benchmark",
            UNKNOWN,
            "not available",
            str((bench.get("reason") or "no benchmark curve") if bench else "no benchmark block")
            + ".",
        )
    alpha = _num(bench.get("alpha"))
    if alpha is None:
        return _check(
            "beats_benchmark",
            "Beats benchmark",
            UNKNOWN,
            "not available",
            "The benchmark ran but no excess return could be computed.",
        )
    # Alpha is simple excess return (fraction), per §3's definition.
    pct = alpha * 100.0
    if pct > 0:
        return _check(
            "beats_benchmark",
            "Beats benchmark",
            GREEN,
            f"+{pct:.2f}%",
            f"Ahead of {bench.get('label') or 'the benchmark'} by {pct:.2f}%.",
        )
    return _check(
        "beats_benchmark",
        "Beats benchmark",
        RED,
        f"{pct:.2f}%",
        f"{bench.get('label') or 'The benchmark'} matched or beat this by "
        f"{abs(pct):.2f}%. A strategy that only pays when the index does is "
        "not a strategy, it is a delayed index fund.",
    )


def _cost_shock(cost_shock: Mapping[str, Any] | None) -> dict[str, Any]:
    block = cost_shock or {}
    if not block.get("available"):
        return _check(
            "cost_shock_2x",
            "Cost shock (2x)",
            UNKNOWN,
            "not available",
            str(block.get("reason") or "no cost-shock block") + ".",
        )
    scenarios: Sequence[Mapping[str, Any]] = block.get("scenarios") or []
    two = next((s for s in scenarios if _num(s.get("multiple")) == 2.0), None)
    if two is None:
        return _check(
            "cost_shock_2x",
            "Cost shock (2x)",
            UNKNOWN,
            "not available",
            "The 2x slippage scenario did not run.",
        )
    bps = _num(two.get("slippage_bps"))
    where = f"at {bps:g} bps" if bps is not None else "at 2x slippage"
    if block.get("base_bps_source") == "default":
        # Say so, or a green tick implies the strategy survived doubled costs
        # it never actually paid.
        where += " (2x the 5 bps default — this run charged no slippage)"
    if two.get("profitable"):
        return _check(
            "cost_shock_2x",
            "Cost shock (2x)",
            GREEN,
            f"profitable {where}",
            "The edge survives twice the trading costs.",
        )
    return _check(
        "cost_shock_2x",
        "Cost shock (2x)",
        RED,
        f"loss-making {where}",
        "The edge disappears at 2x costs. It is being paid for by the "
        "spread, not by the signal.",
    )


def _profit_factor(metrics: Mapping[str, Any], has_evidence: bool) -> dict[str, Any]:
    pf = _num(metrics.get("profit_factor"))
    if pf is None:
        return _check(
            "profit_factor",
            "Profit factor",
            UNKNOWN,
            "n/a",
            "No closed trades, so there is no profit to divide by a loss.",
        )
    if not has_evidence:
        return _check(
            "profit_factor",
            "Profit factor",
            UNKNOWN,
            f"{pf:.2f}",
            "Too few closed trades for a profit factor to mean anything.",
        )
    status = _band(pf, PROFIT_FACTOR_GREEN, PROFIT_FACTOR_YELLOW)
    detail = {
        GREEN: "Gross profit is comfortably larger than gross loss.",
        YELLOW: "Profitable, but the margin is thin enough to vanish on a bad month.",
        RED: "Gross loss is at least as large as gross profit. The strategy is not "
        "paying for the trades it takes.",
    }[status]
    return _check("profit_factor", "Profit factor", status, f"{pf:.2f}", detail)


def _max_drawdown(metrics: Mapping[str, Any], has_evidence: bool) -> dict[str, Any]:
    dd = _num(metrics.get("max_drawdown_pct"))
    if dd is None:
        return _check("max_drawdown", "Max drawdown", UNKNOWN, "n/a", "No drawdown figure.")
    if not has_evidence:
        # A strategy that never traded has a 0% drawdown. Reporting that as a
        # green tick would be the single most flattering false statement the
        # panel could make.
        return _check(
            "max_drawdown",
            "Max drawdown",
            UNKNOWN,
            f"{dd:.1f}%",
            "No closed trades, so this is a flat curve rather than a " "drawdown anyone survived.",
        )
    status = _band_dd(dd)
    detail = {
        GREEN: "The worst peak-to-trough fall is inside the usual tolerance.",
        YELLOW: "A real drawdown, large enough to test whether the position size suits.",
        RED: "Losing a quarter of the account or more is a level most people do not "
        "stay invested through — which is the point at which the strategy is "
        "abandoned at the bottom.",
    }[status]
    return _check("max_drawdown", "Max drawdown", status, f"{dd:.1f}%", detail)


def _monte_carlo(monte_carlo: Mapping[str, Any] | None) -> dict[str, Any]:
    block = monte_carlo or {}
    if not block.get("available"):
        return _check(
            "monte_carlo_p_profit",
            "Monte Carlo P(profit)",
            UNKNOWN,
            "not available",
            str(block.get("reason") or "no Monte Carlo block") + ".",
        )
    p = _num(((block.get("bootstrap") or {}).get("profit_probability_pct")))
    if p is None:
        return _check(
            "monte_carlo_p_profit",
            "Monte Carlo P(profit)",
            UNKNOWN,
            "not available",
            "The bootstrap did not report a profit probability.",
        )
    status = _band(p, MONTE_CARLO_GREEN_PCT, MONTE_CARLO_YELLOW_PCT)
    detail = {
        GREEN: f"{p:.0f}% of resampled trade sequences ended profitable.",
        YELLOW: f"Only {p:.0f}% of resampled sequences ended profitable. A coin flip "
        "is close to this line.",
        RED: f"Only {p:.0f}% of resampled sequences ended profitable. The result is "
        "more likely than not to be a loss in a different ordering of the same trades.",
    }[status]
    return _check("monte_carlo_p_profit", "Monte Carlo P(profit)", status, f"{p:.0f}%", detail)


# ---------------------------------------------------------------------------
# The panel
# ---------------------------------------------------------------------------


def build_readiness(result: Mapping[str, Any]) -> dict[str, Any]:
    """The whole §5 block for one run.

    Takes the run payload the API already returns — ``metrics``, ``provenance``
    and the three §3 check blocks — so there is nothing new to compute and no
    second engine pass.
    """
    metrics = result.get("metrics") or {}
    provenance = result.get("provenance") or {}
    closed = int(_num(metrics.get("closed_trades")) or 0)
    has_evidence = closed >= NO_EVIDENCE_TRADES

    checks = [
        _engine(provenance),
        _data_source(provenance),
        _trade_count(metrics),
        _beats_benchmark(result.get("benchmark")),
        _cost_shock(result.get("cost_shock")),
        _profit_factor(metrics, has_evidence),
        _max_drawdown(metrics, has_evidence),
        _monte_carlo(result.get("monte_carlo")),
    ]

    counts = {GREEN: 0, YELLOW: 0, RED: 0, UNKNOWN: 0}
    for c in checks:
        counts[c["status"]] = counts.get(c["status"], 0) + 1

    all_green = counts[GREEN] == len(checks)
    has_red = counts[RED] > 0

    if has_red:
        verdict = "fail"
        summary = (
            "One or more checks failed. Review the flagged items before " "proceeding to Optimize."
        )
    elif all_green:
        verdict = "pass"
        summary = (
            "Basic checks passed. Consider running Optimize to validate "
            "parameter robustness before paper trading."
        )
    else:
        # The PRD gives a line for "any red" and for "all green" and nothing
        # for the middle. Saying "basic checks passed" here would be the one
        # thing a reader of this panel is entitled to rely on, so the middle
        # gets its own line that concedes exactly what is missing.
        verdict = "incomplete"
        unproven = counts[UNKNOWN]
        parts = []
        if counts[YELLOW]:
            parts.append(f"{counts[YELLOW]} weak")
        if unproven:
            parts.append(f"{unproven} unproven")
        summary = (
            f"Nothing failed, but {', '.join(parts)}. A check that could not be "
            "evaluated is not a check that passed — widen the date range or switch "
            "to the full engine before reading this as green."
        )

    return {
        "advisory": True,
        "gates_nothing": True,
        "verdict": verdict,
        "all_green": all_green,
        "counts": counts,
        "closed_trades": closed,
        "checks": checks,
        "summary": summary,
        "red_flags": [c["label"] for c in checks if c["status"] == RED],
        "unproven": [c["label"] for c in checks if c["status"] == UNKNOWN],
        # §5 gates the §6 button on this. The button itself is §6; exposing the
        # flag now means the panel does not have to be edited again to use it.
        "tune_this_available": all_green,
    }
