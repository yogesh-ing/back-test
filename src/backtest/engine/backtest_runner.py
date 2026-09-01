"""Canonical high-level backtest entry (ticket #6).

This module is **the** single place a backtest is built, run and shaped into
a :class:`~backtest.engine.backtester.BacktestResult`:

* :func:`run_backtest` — the canonical fill-exact engine
  (:class:`~backtest.engine.backtest_driver.BacktestDriver` over the shared
  :func:`~backtest.simulator.engine_loop.run_engine_loop`, next-bar-open
  fills, Decimal-exact accounting).
* :func:`run_quick_screen` — the legacy vectorized
  :class:`~backtest.engine.backtester.Backtester` (prev-close fills, built-in
  costs), kept only as the optional fast rough filter. It is **not** the
  canonical path — its divergent contract lives here explicitly rather than
  being half-baked into callers.
* :func:`trim_to_range`, :func:`resolve_interval`,
  :func:`resolve_warmup_start` — the run-preparation/result-shaping helpers
  that used to be cloned across ``api/backtest.py`` and ``api/forward.py``.

Every caller (``api/backtest.py`` single + run-many pool worker,
``api/forward.py`` replay start) imports these; no module re-implements
bootstrap logic. Layering: this module must not import from
``backtest.forward`` (the forward package is the sibling run, not a
dependency); shared primitives come from :mod:`backtest.data` and
:mod:`backtest.simulator`.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

import pandas as pd

from backtest.data.base import CANONICAL_TIMEFRAMES as SUPPORTED_TIMEFRAMES
from backtest.data.frame_source import FrameSource
from backtest.engine.backtest_driver import BacktestDriver
from backtest.engine.backtester import BacktestConfig, BacktestResult
from backtest.engine.metrics import compute_metrics
from backtest.runner import run_on_candles
from backtest.simulator.bucket_risk import resolve_bucket_risk
from backtest.simulator.execution import free_executor
from backtest.simulator.position_sizing import all_in_size
from backtest.simulator.portfolio import Portfolio
from backtest.strategy.registry import get_strategy

logger = logging.getLogger("backtest.engine.backtest_runner")

__all__ = [
    "run_backtest",
    "run_quick_screen",
    "trim_to_range",
    "resolve_interval",
    "resolve_warmup_start",
]


# ---------------------------------------------------------------------------
# Run preparation helpers
# ---------------------------------------------------------------------------


def resolve_interval(timeframe: str | None, log_prefix: str = "[timeframe]") -> str:
    """Resolve a UI timeframe to its canonical name (unknown → ``1day``).

    Case-insensitive. Kept permissive on purpose: rejecting an unsupported
    timeframe is part of gap G6/G11. Until then, say so loudly in the log
    instead of silently. ``log_prefix`` preserves each caller's log identity.
    """
    if not timeframe:
        return "1day"
    key = str(timeframe).strip().lower()
    if key not in SUPPORTED_TIMEFRAMES:
        logger.warning(
            "%s unsupported timeframe %r — falling back to '1day' (supported: %s)",
            log_prefix, timeframe, ", ".join(SUPPORTED_TIMEFRAMES),
        )
        return "1day"
    return key


def resolve_warmup_start(
    from_date: str,
    warmup_bars: int = 0,
    log_prefix: str = "[backtest]",
    label: str = "from_date",
) -> str:
    """Start date that includes ``warmup_bars`` of extra history.

    Borrowed from the API clones: the run loads ``warmup_bars`` bars before
    ``from_date`` (0 = exactly the requested range). Unparseable dates warn
    and fall back to ``from_date`` unchanged.
    """
    try:
        from_dt = datetime.strptime(from_date, "%Y-%m-%d")
        return (from_dt - timedelta(days=warmup_bars * 2)).strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        logger.warning(
            "%s unparseable %s %r — no warmup applied", log_prefix, label, from_date,
        )
        return from_date


# ---------------------------------------------------------------------------
# Result shaping
# ---------------------------------------------------------------------------


def trim_to_range(result: BacktestResult, from_date: str, to_date: str) -> BacktestResult:
    """Trim backtest result to the requested date range.

    Removes warmup bars from candles, equity, returns, and position series
    so the user only sees the date range they asked for.
    Uses string date comparison to avoid tz-aware/tz-naive issues.
    """
    # Use string date comparison — avoids tz mismatch entirely
    idx = result.candles.index
    idx_dates = idx.strftime("%Y-%m-%d")
    mask = (idx_dates >= from_date) & (idx_dates <= to_date)
    trimmed_candles = result.candles.loc[mask]

    if trimmed_candles.empty:
        return result

    # Trim equity, returns, position to same range.
    trimmed_returns = result.returns.loc[mask].copy()
    trimmed_position = result.position.loc[mask].copy()

    # Warmup bars exist only to give indicators history — no position may be
    # held (and no return earned) before the visible range. Force the first
    # in-range bar flat so a warmup-spanning position doesn't manufacture a
    # phantom trade at the trim boundary.
    if len(trimmed_position) > 0:
        trimmed_position.iloc[0] = 0
        trimmed_returns.iloc[0] = 0.0

    # Renormalize equity so it starts at initial capital for a smooth ramp.
    trimmed_equity = result.equity.loc[mask].copy()
    if len(trimmed_equity) > 0:
        initial = result.config.initial_capital
        cum_returns = (1 + trimmed_returns).cumprod()
        trimmed_equity = pd.Series(
            initial * cum_returns.values,
            index=trimmed_equity.index,
        )

    trimmed = BacktestResult(
        equity=trimmed_equity,
        returns=trimmed_returns,
        position=trimmed_position,
        candles=trimmed_candles,
        config=result.config,
        metrics={},
    )
    # Recompute metrics on the trimmed frames so `bars`/drawdown/etc. match
    # the visible date range (metrics were computed over the full warmup set).
    trimmed.metrics = compute_metrics(trimmed)
    # Preserve run metadata stamped on by run_on_candles.
    for key in ("strategy", "strategy_params", "symbol", "stop_loss", "take_profit"):
        if key in result.metrics:
            trimmed.metrics[key] = result.metrics[key]
    return trimmed


# ---------------------------------------------------------------------------
# Engines
# ---------------------------------------------------------------------------


def run_backtest(
    candles: pd.DataFrame,
    strategy: str,
    params: dict[str, Any] | None,
    symbol: str,
    initial_capital: float,
) -> BacktestResult:
    """Run the CANONICAL engine: ``BacktestDriver`` over simulator/.

    Returns a :class:`BacktestResult` with the same shape the vectorized path
    produces, so :class:`BacktestAdapter` (and therefore the UI) is unchanged:
    the equity curve is the portfolio's per-bar equity snapshots and the
    per-bar holding state is read from those snapshots' ``position_value``
    (a true per-bar reading of the book — positions open/closed timestamps
    are wall-clock, not bar time, so they must not be used for this).
    Metrics and trades come from the same ``engine/metrics`` +
    ``engine/trades`` code the vectorized path uses.
    """
    strategy_instance = get_strategy(strategy)(**(params or {}))
    active = int((strategy_instance.generate_signals(candles).fillna(0) != 0).sum())
    if active == 0:
        logger.warning(
            "[run] %s produced NO signals on %s (%d bars, params=%s) — the run will be "
            "flat: the indicator warmup likely exceeds the window, or the thresholds "
            "never trigger on this data",
            strategy, symbol, len(candles), params,
        )
    portfolio = Portfolio(name=f"backtest-{strategy}", initial_capital=initial_capital)
    # Ticket #9 — the canonical backtest is a paper-bucket run (simulated
    # fills): risk limits resolve from the same classification the forward
    # engine uses, never a hardcoded global knob. ``FrameSource`` is not a
    # registered SOURCE_TAGS class, so it classifies as the canonical
    # default (synthetic). Paper defaults are permissive, so historical
    # P&L is unchanged.
    _, paper_bucket = resolve_bucket_risk("paper", "synthetic")
    portfolio.limits = paper_bucket.to_portfolio_limits()
    driver = BacktestDriver(
        source=FrameSource(candles),
        strategy=strategy_instance,
        portfolio=portfolio,
        executor=free_executor(portfolio, max_participation="1"),
        symbols=[str(symbol).strip().upper()],
        size_fn=all_in_size,
    )
    driver.run()

    points = portfolio.equity_history
    index = [pd.Timestamp(p.ts) for p in points]
    equity = pd.Series([float(p.total_equity) for p in points], index=index, dtype="float64")
    holding = pd.Series(
        [1.0 if float(p.position_value) > 0 else 0.0 for p in points],
        index=index, dtype="float64",
    )
    result = BacktestResult(
        equity=equity,
        returns=equity.pct_change(),
        position=holding,
        candles=candles,
        config=BacktestConfig(initial_capital=initial_capital),
        metrics={},
    )
    result.metrics = compute_metrics(result)
    result.metrics["strategy"] = strategy
    result.metrics["strategy_params"] = params
    result.metrics["symbol"] = symbol
    result.metrics["stop_loss"] = result.config.stop_loss
    result.metrics["take_profit"] = result.config.take_profit
    return result


def run_quick_screen(
    candles: pd.DataFrame,
    strategy: str,
    params: dict[str, Any] | None,
    symbol: str,
    initial_capital: float,
    from_date: str,
    to_date: str,
) -> BacktestResult:
    """Legacy vectorized quick filter: :func:`runner.run_on_candles` + trim.

    Prev-close fills and built-in costs (NOT the canonical fill-exact path);
    kept only as the optional fast rough filter. ``from_date``/``to_date``
    trim the warmup prefix off the result.
    """
    result = run_on_candles(
        candles, strategy, params, symbol,
        BacktestConfig(initial_capital=initial_capital),
    )
    return trim_to_range(result, from_date, to_date)
