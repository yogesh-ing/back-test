"""Monte Carlo over a result's trade sequence (PRD backTest-enhance §3.3).

Asks: *was this result lucky, or would it have happened in most orderings of the
same trades?*

**The PRD's literal specification is vacuous for half of what it asks for, and
that is worth stating plainly rather than quietly working around.**

Shuffling a list of trade P&Ls changes the path, never the sum. So for the
1,000 reorderings the PRD describes:

* final equity is **identical in every simulation** — mean, median, 5th and
  95th percentile alike;
* "probability of profit" is exactly 100% or exactly 0%, decided entirely by
  the sign of the total, with no reference to any particular ordering.

Reporting those as "median final equity ₹X" and "P(profit) 79%" would put four
identical numbers on the page in a table that looks like a distribution, and a
reader would reasonably conclude the tool had measured something.

So this module runs **two** resamplings and keeps them apart:

``reorder``
    The PRD's shuffle, WITH replacement disabled. This is a genuine test of
    *path dependence*: the same trades in a different order can produce a very
    different drawdown even though the ending is identical. It answers "could
    I have been underwater for twice as long?".

``bootstrap``
    Resample WITH replacement, so the *multiset* of trades changes too. This is
    the only one of the two whose final-equity distribution means anything, and
    it answers the question the PRD's percentiles were reaching for: "would a
    similar sequence of results have produced a different ending?"

A second PRD rule is unreachable for the same structural reason, and is
replaced rather than shipped dead: *"if the actual result is above the 90th
percentile of simulations"* can never fire. The actual sample **defines** the
empirical distribution being resampled, so it sits at roughly the 50th-75th
percentile of its own bootstrap no matter what the trades look like — the
ceiling is about 0.74 for any n (verified in
``test_run_checks.py::test_the_actual_percentile_has_a_mathematical_ceiling``).
A rule that can never fire is a feature that only ever looks correct, so the
"was this one lucky trade?" question is asked directly instead, via
:func:`trade_concentration`.
"""

from __future__ import annotations

import logging
from typing import Any, Sequence

import numpy as np

logger = logging.getLogger("backtest.engine.monte_carlo")

__all__ = [
    "DEFAULT_SIMULATIONS",
    "FAN_PERCENTILES",
    "monte_carlo_trade_order",
]

#: The PRD's 1,000. On even a few hundred trades this is well under a second.
DEFAULT_SIMULATIONS = 1000

#: Below this many closed trades there is nothing to resample. §2's
#: trade-count flag is the authority on sample quality; this only stops the
#: loop from pretending one trade has a distribution.
MIN_TRADES = 2

#: Seeded so the same result always produces the same fan chart. An
#: unseeded Monte Carlo makes a page that changes when you refresh it, which
#: reads as a bug in the numbers.
DEFAULT_SEED = 42

#: Above this, one trade is carrying so much of the profit that the run is an
#: anecdote. 50% is a judgement call, stated here so it can be argued with.
CONCENTRATION_WARN_PCT = 50.0


def _final_and_dd(pnls: np.ndarray, start: float) -> tuple[np.ndarray, np.ndarray]:
    """Final equity and peak-to-trough DEPTH (positive) for many orderings.

    Vectorised across simulations: one ``cumsum`` over an ``(n, k)`` array.
    """
    curves = start + np.cumsum(pnls, axis=1)
    finals = curves[:, -1]
    # Peak includes the starting capital: an account that never goes above its
    # opening balance has a drawdown measured from the opening balance, not
    # from a peak that does not exist.
    opening = np.full((pnls.shape[0], 1), start)
    peaks = np.maximum.accumulate(np.concatenate([opening, curves], axis=1), axis=1)
    depths = (peaks[:, 1:] - curves) / peaks[:, 1:]
    return finals, depths.max(axis=1)


def _pct(values: np.ndarray, q: float) -> float:
    return float(np.percentile(values, q)) if values.size else 0.0


def _summary(finals: np.ndarray, depths: np.ndarray, start: float, actual: float) -> dict[str, Any]:
    return {
        "median_final_equity": round(_pct(finals, 50), 2),
        "p5_final_equity": round(_pct(finals, 5), 2),
        "p95_final_equity": round(_pct(finals, 95), 2),
        "median_max_drawdown_pct": round(_pct(depths, 50), 2),
        "p95_max_drawdown_pct": round(_pct(depths, 95), 2),
        "worst_max_drawdown_pct": round(float(depths.max()) if depths.size else 0.0, 2),
        "profit_probability_pct": round(float((finals > start).mean()) * 100, 1),
    }


def _actual_percentile(finals: np.ndarray, actual: float) -> float:
    """Where the real result sits in the simulated distribution (0-100)."""
    if finals.size == 0:
        return 50.0
    return round(float((finals <= actual).mean()) * 100, 1)


#: Quantile paths returned in ``fan``. 5/25/50/75/95 is enough to see the shape
#: of the spread; more bands just overlap into a grey smear.
FAN_PERCENTILES = (5, 25, 50, 75, 95)

#: Above this many trades the quantile paths are downsampled. A 2,000-trade
#: result would otherwise ship 2,000 points per band to draw a chart a few
#: hundred pixels wide — the resolution is invisible and the payload is not.
FAN_MAX_POINTS = 400


def _fan(pnls: np.ndarray, start: float) -> dict[str, Any]:
    """Quantile paths across the resampled sequences — the fan chart's data.

    ``pnls`` is the ``(n_sims, n_trades)`` P&L matrix; the equity path is
    derived here so a caller cannot pass the wrong one. Only the percentiles
    survive, not the individual paths: a fan chart shows the envelope of the
    distribution, and shipping every simulation to draw five lines would send
    200x the data for the same picture.
    """
    if pnls.size == 0:
        return {"points": 0, "start": round(float(start), 2), "bands": {}}
    curves = float(start) + np.cumsum(pnls, axis=1)  # (n_sims, n_trades)
    # One opening point per simulation, prepended so the fan starts at the
    # account's real starting capital rather than at the first trade boundary.
    opening = np.full((curves.shape[0], 1), float(start))
    with_start = np.concatenate([opening, curves], axis=1)  # (n_sims, n_trades + 1)
    bands = {
        str(p): [round(float(v), 2) for v in np.percentile(with_start, p, axis=0)]
        for p in FAN_PERCENTILES
    }
    total = with_start.shape[1]  # points per path, not the simulation count
    if total > FAN_MAX_POINTS:
        step = int(np.ceil(total / FAN_MAX_POINTS))
        keep = list(range(0, total, step))
        if keep[-1] != total - 1:
            keep.append(total - 1)  # always end on the real final equity
        bands = {k: [v[i] for i in keep] for k, v in bands.items()}
        total = len(keep)
    return {"points": total, "start": round(float(start), 2), "bands": bands}


def trade_concentration(trade_pnls: Sequence[float]) -> dict[str, Any]:
    """How much of the profit rests on the single best trade.

    This is the "was the result lucky" question the PRD's 90th-percentile rule
    was reaching for, and unlike it, it can actually fail. A run whose gross
    profit is 90% one trade is not a run that would have repeated; it is one
    observation, and the bootstrap cannot tell you so because the outlier is
    *in* the sample being resampled.
    """
    pnls = [float(p) for p in trade_pnls]
    gross_profit = sum(p for p in pnls if p > 0)
    best = max(pnls) if pnls else 0.0
    losers = sum(p for p in pnls if p < 0)
    return {
        "gross_profit": round(gross_profit, 2),
        "gross_loss": round(abs(losers), 2),
        "best_trade": round(best, 2),
        "best_trade_share_pct": round((best / gross_profit) * 100, 1) if gross_profit > 0 else 0.0,
        "net": round(gross_profit + losers, 2),
    }


def monte_carlo_trade_order(
    trade_pnls: Sequence[float],
    starting_capital: float,
    *,
    simulations: int = DEFAULT_SIMULATIONS,
    seed: int = DEFAULT_SEED,
) -> dict[str, Any]:
    """Resample one result's closed-trade P&Ls and describe the spread.

    ``trade_pnls`` are **absolute rupee** P&Ls, not fractions — the trade walk
    already produced them that way and they are the only form that sums back to
    the run's real ending equity.
    """
    count = len(trade_pnls)
    base = {
        "available": False,
        "simulations": int(simulations),
        "trades": count,
        "starting_capital": round(float(starting_capital), 2),
    }
    if count < MIN_TRADES:
        return {
            **base,
            "reason": (
                f"only {count} closed trade{'s' if count != 1 else ''} — "
                "there is no sequence to resample"
            ),
        }
    if starting_capital <= 0:
        return {**base, "reason": "starting capital must be positive"}

    pnls = np.asarray([float(p) for p in trade_pnls], dtype="float64")
    n_sims = max(2, int(simulations))
    rng = np.random.default_rng(seed)

    # (a) reorder: the same trades, a different sequence.
    shuffled = np.argsort(rng.random((n_sims, count)), axis=1)
    reorder_pnls = pnls[shuffled]

    # (b) bootstrap: a different draw from the same empirical distribution.
    boot_idx = rng.integers(0, count, size=(n_sims, count))
    boot_pnls = pnls[boot_idx]

    start = float(starting_capital)
    actual_final = start + float(pnls.sum())

    r_finals, r_depths = _final_and_dd(reorder_pnls, start)
    b_finals, b_depths = _final_and_dd(boot_pnls, start)

    reorder = _summary(r_finals, r_depths, start, actual_final)
    bootstrap = _summary(b_finals, b_depths, start, actual_final)

    # Under a pure reorder the final equity is invariant — assert the claim in
    # the payload rather than only in the docstring, so if a future change
    # breaks it, the UI has something to key off.
    reorder["final_equity_is_invariant"] = bool(
        np.allclose(r_finals, actual_final, rtol=0, atol=1e-6)
    )
    bootstrap["final_equity_is_invariant"] = False

    # Under a pure reorder the actual result IS the distribution — reporting
    # the float-noise percentile the comparison happens to produce (99.2% here)
    # would dress a tautology up as a measurement.
    reorder_percentile = (
        50.0 if reorder["final_equity_is_invariant"] else _actual_percentile(r_finals, actual_final)
    )
    bootstrap_percentile = _actual_percentile(b_finals, actual_final)

    warnings: list[dict[str, str]] = []
    if bootstrap["profit_probability_pct"] < 60:
        warnings.append(
            {
                "level": "error",
                "code": "low_profit_probability",
                "message": (
                    f"Only {bootstrap['profit_probability_pct']:.0f}% of resampled "
                    "trade sequences ended profitable. The result leans on a few "
                    "unusually large wins."
                ),
            }
        )
    concentration = trade_concentration(pnls)
    if concentration["best_trade_share_pct"] > CONCENTRATION_WARN_PCT:
        warnings.append(
            {
                "level": "warning",
                "code": "profit_concentrated",
                "message": (
                    f"One trade produced {concentration['best_trade_share_pct']:.0f}% of "
                    "gross profit. Resampling cannot surface this, because the "
                    "outlier is inside the sample being resampled — but a run that "
                    "leans on one trade has not been shown to repeat."
                ),
            }
        )
    if bootstrap["p5_final_equity"] < start and bootstrap["median_final_equity"] > start:
        warnings.append(
            {
                "level": "warning",
                "code": "downside_tail",
                "message": (
                    "The median resampled run is profitable but the 5th percentile "
                    "is not — a real chance of ending underwater on a similar "
                    "sequence of trades."
                ),
            }
        )
    if reorder["p95_max_drawdown_pct"] > 0:
        warnings.append(
            {
                "level": "info",
                "code": "reordering_matters",
                "message": (
                    f"Reordering the same trades changes the worst drawdown from "
                    f"{reorder['median_max_drawdown_pct']:.1f}% to "
                    f"{reorder['p95_max_drawdown_pct']:.1f}%. The ending equity is "
                    "unchanged — the path is not."
                ),
            }
        )

    return {
        **base,
        "available": True,
        "seed": int(seed),
        "actual_final_equity": round(actual_final, 2),
        "drawdown_note": (
            "Drawdowns here are measured between trade boundaries, not on "
            "every bar: the resampled path has one point per trade, so an "
            "excursion inside a trade is invisible to it. Read these as the "
            "drawdown the SEQUENCE imposes, never as the run's max drawdown."
        ),
        "concentration": concentration,
        "reorder": {**reorder, "actual_percentile": reorder_percentile},
        "bootstrap": {**bootstrap, "actual_percentile": bootstrap_percentile},
        # The fan is drawn from the bootstrap band: under a pure reorder every
        # path ends at the same equity, so its "fan" would be a flat wedge and
        # would read as certainty where none exists.
        "fan": _fan(boot_pnls, start),
        "warnings": warnings,
    }
