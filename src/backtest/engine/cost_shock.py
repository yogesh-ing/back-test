"""Cost-shock stress test (PRD backTest-enhance §3.2).

Re-runs one fixed configuration at 1x / 2x / 3x a slippage level and asks the
only question that matters about a backtest nobody can control: *does the edge
survive fills that are worse than the ones you modelled?*

The awkward part, and the reason this module exists as more than three loop
iterations:

**The canonical backtest is costless.** ``run_backtest`` defaults to
``free_executor``, which has zero slippage and zero fees. Taking "2x the
configured slippage" literally would therefore be 2 x 0 = 0, and the table
would show three identical green rows for every strategy ever run — a
vacuously reassuring result, which is worse than no result at all. So when the
run is genuinely frictionless we stress from an explicit, labelled default
(:data:`DEFAULT_COST_SHOCK_BASE_BPS`, 5 bps — the same NSE large-cap default
the slippage model itself documents) and say so in the payload, rather than
quietly inventing a number the reader cannot see.

Two other things the table refuses to hide:

* **A drop in trade count means the sizing broke, not just the edge.** An
  all-in order needs buying power for the slipped price; past a certain
  slippage the account cannot fund the round trip at all. That shows up as
  fewer trades, and reading it as "the edge vanished" would be the wrong
  lesson, so each scenario carries ``trades_dropped`` and the UI is told.
* **The 1x row is a re-run, not the result on the cards.** The cards above
  were produced at the run's own slippage level; the table's rows share one
  execution model so that the only thing varying down the column is the
  slippage multiple. The actual result is reported alongside as ``actual``.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable

import pandas as pd

logger = logging.getLogger("backtest.engine.cost_shock")

__all__ = [
    "DEFAULT_COST_SHOCK_BASE_BPS",
    "COST_SHOCK_MULTIPLIERS",
    "resolve_base_bps",
    "run_cost_shock",
]

#: Stress base used when the run itself is frictionless. 5 bps is the default
#: the ``FixedBpsSlippage`` docstring already calls "a sane NSE large-cap
#: default"; matching it keeps the two halves of the app telling one story.
DEFAULT_COST_SHOCK_BASE_BPS = 5.0

#: The PRD's 1x / 2x / 3x column.
COST_SHOCK_MULTIPLIERS = (1, 2, 3)

#: Below this many closed trades the cost-shock verdict is not worth printing.
#: The §2 trade-count warning is the authority on that; this only stops the
#: table from dressing up noise.
MIN_TRADES_FOR_VERDICT = 3


def resolve_base_bps(configured: float | None) -> tuple[float, str]:
    """Return ``(bps, source)`` for the stress base.

    ``source`` is ``"configured"`` when the run already charged slippage, and
    ``"default"`` when it was frictionless and the PRD's multiplier would have
    been a no-op. The caller puts it in the payload so the UI can label the
    column honestly instead of implying the run was costed.
    """
    if configured is None:
        return DEFAULT_COST_SHOCK_BASE_BPS, "default"
    try:
        bps = float(configured)
    except (TypeError, ValueError):
        return DEFAULT_COST_SHOCK_BASE_BPS, "default"
    if bps <= 0:
        return DEFAULT_COST_SHOCK_BASE_BPS, "default"
    return bps, "configured"


def _scenario_row(
    multiple: int, bps: float, metrics: dict[str, Any], base_trades: int
) -> dict[str, Any]:
    total_return = float(metrics.get("total_return", 0.0))
    trades = int(metrics.get("closed_trades", 0))
    return {
        "multiple": multiple,
        "label": ("Base" if multiple == 1 else f"{multiple}x Slippage"),
        "slippage_bps": round(bps, 4),
        "slippage_pct": round(bps / 100.0, 6),
        "total_return_pct": round(total_return * 100, 2),
        "sharpe": round(float(metrics.get("sharpe", 0.0)), 2),
        "max_drawdown_pct": round(float(metrics.get("max_drawdown", 0.0)) * 100, 2),
        "closed_trades": trades,
        "trades_dropped": max(0, base_trades - trades),
        "profitable": total_return > 0,
    }


def _verdict(scenarios: list[dict[str, Any]]) -> tuple[str, dict[str, Any] | None]:
    """Green / yellow / red, exactly as §3.2 words it."""
    by_multiple = {s["multiple"]: s for s in scenarios}
    two, three = by_multiple.get(2), by_multiple.get(3)
    if two is not None and not two["profitable"]:
        return "broken", {
            "level": "error",
            "message": (
                "Edge disappears at 2x slippage. Strategy is not robust to "
                "execution uncertainty."
            ),
        }
    if three is not None and not three["profitable"]:
        return "fragile", {
            "level": "warning",
            "message": (
                "Edge survives 2x slippage but not 3x. Treat the result as "
                "sensitive to execution quality."
            ),
        }
    return "robust", {
        "level": "info",
        "message": "Edge survives 3x slippage.",
    }


def run_cost_shock(
    candles: pd.DataFrame,
    strategy: str,
    params: dict[str, Any] | None,
    symbol: str,
    capital: float,
    timeframe: str | None,
    run_once: Callable[..., Any],
    *,
    actual_metrics: dict[str, Any] | None = None,
    configured_bps: float | None = None,
) -> dict[str, Any]:
    """Build the §3.2 table by re-running ``run_once`` at each multiple.

    ``run_once`` is the caller's engine entry point, injected rather than
    imported so the cost-shock table is identical for the canonical driver and
    (when it supports a slippage argument) any other engine, and so this
    module never has to know which one ran.

    Every failure path returns a well-formed ``available: False`` block with a
    ``reason``. A stress test that throws takes the whole result page down with
    it, which is a terrible trade for a diagnostic.
    """
    base_bps, source = resolve_base_bps(configured_bps)
    actual = dict(actual_metrics or {})

    def unavailable(reason: str) -> dict[str, Any]:
        return {
            "available": False,
            "reason": reason,
            "base_bps": round(base_bps, 4),
            "base_bps_source": source,
            "scenarios": [],
        }

    if candles is None or len(candles) < 2:
        return unavailable("not enough bars to stress")
    if not actual:
        return unavailable("no base result to compare against")

    # At a configured base the 1x column IS the run on the cards — re-running
    # it would burn ~60ms to reproduce a number we already have.
    base_metrics = (
        actual
        if source == "configured"
        else _safe_run(run_once, candles, strategy, params, symbol, capital, timeframe, base_bps)
    )
    if base_metrics is None:
        return unavailable("the 1x slippage re-run did not complete")

    base_trades = int(base_metrics.get("closed_trades", 0))
    scenarios = [_scenario_row(1, base_bps, base_metrics, base_trades)]
    for multiple in COST_SHOCK_MULTIPLIERS:
        if multiple == 1:
            continue
        bps = base_bps * multiple
        metrics = _safe_run(run_once, candles, strategy, params, symbol, capital, timeframe, bps)
        if metrics is None:
            return unavailable(f"the {multiple}x slippage re-run did not complete")
        scenarios.append(_scenario_row(multiple, bps, metrics, base_trades))

    scenarios.sort(key=lambda s: s["multiple"])
    status, warning = _verdict(scenarios)

    # A verdict drawn from a handful of trades is noise wearing a green shirt.
    insufficient = base_trades < MIN_TRADES_FOR_VERDICT
    if insufficient:
        status = "insufficient_trades"
        warning = {
            "level": "warning",
            "message": (
                f"Only {base_trades} closed trades — the cost-shock verdict is "
                "not reliable. Widen the date range first."
            ),
        }

    if any(s["trades_dropped"] for s in scenarios):
        dropped = next(s for s in scenarios if s["trades_dropped"])["multiple"]
        warning = {
            "level": "warning",
            "message": (
                f"At {dropped}x slippage the account can no longer fund an "
                "all-in round trip, so trades drop out. The fall in return is "
                "a sizing limit, not only a thinner edge."
            ),
        }

    return {
        "available": True,
        "base_bps": round(base_bps, 4),
        "base_bps_source": source,
        "base_bps_note": (
            "Run was frictionless, so the stress base is the 5 bps NSE default."
            if source == "default"
            else "Run already charged slippage; the base is that level."
        ),
        "status": status,
        "warning": warning,
        "scenarios": scenarios,
        "actual": {
            "slippage_bps": float(configured_bps or 0.0),
            "total_return_pct": round(float(actual.get("total_return", 0.0)) * 100, 2),
            "sharpe": round(float(actual.get("sharpe", 0.0)), 2),
            "closed_trades": base_trades,
        },
    }


def _safe_run(
    run_once: Callable[..., Any],
    candles: pd.DataFrame,
    strategy: str,
    params: dict[str, Any] | None,
    symbol: str,
    capital: float,
    timeframe: str | None,
    bps: float,
) -> dict[str, Any] | None:
    """One stressed re-run, or ``None`` if it could not complete.

    Timed and logged: three extra engine runs land inside the user's click and
    a silent 200ms stall is indistinguishable from a hung request.
    """
    started = time.perf_counter()
    try:
        result = run_once(
            candles, strategy, params, symbol, capital, timeframe=timeframe, slippage_bps=bps
        )
    except Exception as exc:  # noqa: BLE001 — a diagnostic must not kill the page
        logger.warning("[cost-shock] %s bps re-run failed: %s", bps, exc)
        return None
    metrics = getattr(result, "metrics", None)
    if not isinstance(metrics, dict) or not metrics:
        logger.warning("[cost-shock] %s bps re-run returned no metrics", bps)
        return None
    logger.debug(
        "[cost-shock] %s bps → return %.4f in %.0f ms",
        bps,
        float(metrics.get("total_return", 0.0)),
        (time.perf_counter() - started) * 1000,
    )
    return metrics
