"""Backtest endpoints (PRD Tasks 1.5 + 1.6, re-routed in ticket P2.2).

* ``POST /api/backtest/run``      — single strategy deep dive
* ``POST /api/backtest/run-many`` — 2-4 slots in parallel via
  :class:`~concurrent.futures.ProcessPoolExecutor` (ticket P2.3): each slot
  runs :func:`run_single_backtest` in its own worker process with plain-dict
  params, so a crashed job cannot take down the web process.

Two engines, one response shape (the UI is unchanged this phase):

* **canonical (default)** — :class:`~backtest.engine.backtest_driver.BacktestDriver`
  over the simulator executor: the SAME loop the forward paper run uses,
  next-bar-open fills, Decimal-exact portfolio accounting. The portfolio's
  per-bar equity snapshots are mapped onto a ``BacktestResult`` so the
  ``BacktestAdapter`` payload (metrics/equity/drawdown/trades/signals) is
  byte-for-byte the same shape as before — computed by the same
  ``engine/metrics`` + ``engine/trades`` code as the vectorized path.
* ``mode='quick_screen'`` — the legacy vectorized ``Backtester`` (prev-close
  fills, fractional sizing, built-in cost model), kept ONLY as an optional
  fast rough filter.

Both log the resolved request, the bars fetched, the engine summary and any
failure with a traceback, so a UI error toast can always be traced back to one
line in the server log (the id is quoted back in the JSON body as
``request_id``).
"""

from __future__ import annotations

import logging
import traceback
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timedelta
from typing import Any

import pandas as pd
from flask import Blueprint, current_app, jsonify, request

from backtest.adapters.backtest_adapter import BacktestAdapter
from backtest.data.base import CANONICAL_TIMEFRAMES as SUPPORTED_TIMEFRAMES
from backtest.engine.backtester import BacktestConfig
from backtest.logging_config import get_logger, timed
from backtest.runner import build_source, run_on_candles
from backtest.strategy.registry import get_strategy

backtest_bp = Blueprint("backtest_api", __name__)
log = get_logger(__name__)

# Number of extra bars to load before start_date for strategy warmup.
# Set to 0 so the run covers exactly the requested range and the result
# matches a direct run over the same candles (indicators simply ramp over
# the first bars, as they do in a standalone backtest).
WARMUP_BARS = 0


def _interval(timeframe: str | None) -> str:
    """Resolve a UI timeframe to its canonical name (unknown → ``1day``).

    Case-insensitive. Kept permissive on purpose: rejecting an unsupported
    timeframe is part of gap G6/G11. Until then, say so loudly in the log
    instead of silently.
    """
    if not timeframe:
        return "1day"
    key = str(timeframe).strip().lower()
    if key not in SUPPORTED_TIMEFRAMES:
        log.warning(
            "[timeframe] unsupported timeframe %r — falling back to '1day' (supported: %s)",
            timeframe, ", ".join(SUPPORTED_TIMEFRAMES),
        )
        return "1day"
    return key


def _source() -> Any:
    name = current_app.config.get("BACKTEST_SOURCE", "synthetic")
    log.debug("[data] building source %r", name)
    return build_source(name)


def _candles(symbol: str, from_date: str, to_date: str, interval: str):
    """Fetch candles for a symbol at an already-resolved ``interval``."""
    return _source().get_candles(symbol, from_date, to_date, interval)


def _check_params(strategy_cls: Any, params: dict, where: str) -> list[str]:
    """Diagnostic: check ``params`` against the strategy's declared schema.

    Returns a list of human-readable problems (empty = fine). The UI's number
    inputs already carry ``min``/``max``, but nothing stopped a caller from
    posting any value it liked, which produced a silently vacuous backtest.
    Only *logs* for now — turning these into a 400 is gap G11.
    """
    problems: list[str] = []
    schema = strategy_cls.param_schema()
    for key, value in params.items():
        spec = schema.get(key)
        if spec is None:
            continue  # unknown keys are rejected by Strategy.__init__
        lo, hi, ptype = spec.get("min"), spec.get("max"), spec.get("type")
        if value is None:
            continue
        if ptype in ("int", "float") and isinstance(value, (int, float)):
            if lo is not None and value < lo:
                problems.append(f"{key}={value} is below min {lo}")
            if hi is not None and value > hi:
                problems.append(f"{key}={value} is above max {hi}")
    if problems:
        log.warning("[params] %s rejected: %s", where, "; ".join(problems))
    return problems


def _summarise(payload: dict, label: str, params: dict | None = None) -> None:
    cfg, m = payload.get("config", {}), payload.get("metrics", {})
    log.info(
        "[result] %s bars=%s trades=%s return=%.2f%% sharpe=%.2f maxDD=%.2f%% equity=%.2f",
        label,
        cfg.get("bars"),
        m.get("total_trades"),
        m.get("total_return_pct", 0.0),
        m.get("sharpe", 0.0),
        m.get("max_drawdown_pct", 0.0),
        m.get("final_equity", 0.0),
    )
    if not payload.get("trades"):
        log.warning(
            "[result] %s produced 0 trades over %s bars — check that the date range is "
            "longer than the strategy's warmup (params=%s)",
            label, cfg.get("bars"), params if params is not None else cfg.get("strategy_params"),
        )


def _trim_to_range(result, from_date: str, to_date: str):
    """Trim backtest result to the requested date range.

    Removes warmup bars from candles, equity, returns, and position series
    so the user only sees the date range they asked for.
    Uses string date comparison to avoid tz-aware/tz-naive issues.
    """
    from backtest.engine.backtester import BacktestResult

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
    from backtest.engine.metrics import compute_metrics

    trimmed.metrics = compute_metrics(trimmed)
    # Preserve run metadata stamped on by run_on_candles.
    for key in ("strategy", "strategy_params", "symbol", "stop_loss", "take_profit"):
        if key in result.metrics:
            trimmed.metrics[key] = result.metrics[key]
    return trimmed


def _resolve_strategy(name: str):
    """Return the strategy class or a (error_message) string."""
    if not name:
        return "strategy is required"
    try:
        return get_strategy(name)
    except KeyError as exc:
        return str(exc)


# ---------------------------------------------------------------------------
# Engines
# ---------------------------------------------------------------------------

#: Request mode that keeps the legacy vectorized path (fast rough filter only).
QUICK_SCREEN = "quick_screen"


def _run_driver(candles: pd.DataFrame, strategy: str, params: dict,
                symbol: str, capital: float) -> Any:
    """Run the CANONICAL engine: ``BacktestDriver`` over simulator/.

    Returns a :class:`BacktestResult` with the same shape the vectorized path
    produces, so :class:`BacktestAdapter` (and therefore the UI) is unchanged:
    the equity curve is the portfolio's per-bar equity snapshots and the
    per-bar holding state is read from those snapshots' ``position_value``
    (a true per-bar reading of the book — positions open/closed timestamps
    are wall-clock, not bar time, so they must not be used for this).
    Metrics and trades come from the same ``engine/metrics`` +
    ``engine/trades`` code the vectorized path uses.

    Imports are deferred so module import order can never tangle with
    ``backtest.forward`` (the API and the forward package both import each
    other's neighbourhood; at request time everything is fully loaded).
    """
    from backtest.engine.backtest_driver import BacktestDriver
    from backtest.engine.backtester import BacktestResult
    from backtest.engine.metrics import compute_metrics
    from backtest.forward.paper_runner import _FrameSource, _all_in_size, free_executor
    from backtest.simulator.portfolio import Portfolio

    strategy_instance = get_strategy(strategy)(**params)
    active = int((strategy_instance.generate_signals(candles).fillna(0) != 0).sum())
    if active == 0:
        log.warning(
            "[run] %s produced NO signals on %s (%d bars, params=%s) — the run will be "
            "flat: the indicator warmup likely exceeds the window, or the thresholds "
            "never trigger on this data",
            strategy, symbol, len(candles), params,
        )
    portfolio = Portfolio(name=f"backtest-{strategy}", initial_capital=capital)
    driver = BacktestDriver(
        source=_FrameSource(candles),
        strategy=strategy_instance,
        portfolio=portfolio,
        executor=free_executor(portfolio, max_participation="1"),
        symbols=[str(symbol).strip().upper()],
        size_fn=_all_in_size,
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
        config=BacktestConfig(initial_capital=capital),
        metrics={},
    )
    result.metrics = compute_metrics(result)
    result.metrics["strategy"] = strategy
    result.metrics["strategy_params"] = params
    result.metrics["symbol"] = symbol
    result.metrics["stop_loss"] = result.config.stop_loss
    result.metrics["take_profit"] = result.config.take_profit
    return result


# ---------------------------------------------------------------------------
# Single backtest
# ---------------------------------------------------------------------------


@backtest_bp.post("/api/backtest/run")
def run_backtest() -> tuple:
    data = request.get_json(silent=True) or {}

    strategy = data.get("strategy")
    resolved = _resolve_strategy(strategy)
    if isinstance(resolved, str):
        log.warning("[run] rejected: %s (body keys=%s)", resolved, sorted(data))
        return jsonify({"error": resolved}), 400

    symbol = data.get("symbol", "DEMO")
    from_date = data.get("from_date") or data.get("from")
    to_date = data.get("to_date") or data.get("to")
    if not from_date or not to_date:
        log.warning("[run] rejected: from_date/to_date missing (body keys=%s)", sorted(data))
        return jsonify({"error": "from_date and to_date are required"}), 400
    if from_date > to_date:
        log.warning("[run] rejected: from_date %s > to_date %s", from_date, to_date)
        return jsonify({"error": "from_date must be <= to_date"}), 400

    try:
        capital = float(data.get("capital", 100_000))
    except (TypeError, ValueError):
        log.warning("[run] rejected: capital=%r is not a number", data.get("capital"))
        return jsonify({"error": "capital must be a number"}), 400

    params = data.get("params") or {}
    timeframe = data.get("timeframe", "1D")
    interval = _interval(timeframe)
    mode = str(data.get("mode", "")).strip().lower()
    log.info(
        "[run] strategy=%s symbol=%s timeframe=%s→%s range=%s..%s capital=%s mode=%s params=%s",
        strategy, symbol, timeframe, interval, from_date, to_date, capital,
        mode or "driver", params,
    )
    problems = _check_params(resolved, params, f"run/{strategy}")
    if problems:
        log.warning("[run] continuing despite out-of-range params: %s", "; ".join(problems))

    # Calculate warmup start date (extra bars before from_date for strategy warmup)
    try:
        from_dt = datetime.strptime(from_date, "%Y-%m-%d")
        warmup_start = (from_dt - timedelta(days=WARMUP_BARS * 2)).strftime("%Y-%m-%d")
    except ValueError:
        log.warning("[run] unparseable from_date %r — no warmup applied", from_date)
        warmup_start = from_date

    try:
        with timed(log, f"[data] fetch {symbol} {warmup_start}..{to_date}", logging.DEBUG) as t:
            candles_full = _candles(symbol, warmup_start, to_date, interval)
    except Exception as exc:  # noqa: BLE001
        log.warning("[run] data error for %s: %s", symbol, exc)
        return jsonify({"error": f"data error: {exc}"}), 400
    log.debug("[data] %s → %d bars in %.1f ms", symbol, len(candles_full), t.elapsed_ms)

    try:
        if mode == QUICK_SCREEN:
            # Legacy vectorized quick filter (prev-close fills, built-in costs).
            with timed(log, f"[run] {strategy} on {symbol} (quick_screen)", logging.DEBUG):
                result = run_on_candles(
                    candles_full, strategy, params, symbol,
                    BacktestConfig(initial_capital=capital),
                )
            # Trim results to the requested date range (strip warmup period)
            before = len(result.equity)
            result = _trim_to_range(result, from_date, to_date)
            if before != len(result.equity):
                log.debug("[trim] %d → %d bars for %s..%s",
                          before, len(result.equity), from_date, to_date)
            engine = "quick_screen"
        else:
            # Canonical: BacktestDriver over simulator/ (next-bar-open fills).
            # It runs exactly the fetched range (WARMUP_BARS=0), so no trim.
            with timed(log, f"[run] {strategy} on {symbol} (driver)", logging.DEBUG):
                result = _run_driver(candles_full, strategy, params, symbol, capital)
            engine = "backtest_driver"
    except ValueError as exc:
        log.warning("[run] %s rejected input: %s", strategy, exc)
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:  # noqa: BLE001
        log.exception("[run] %s crashed on %s", strategy, symbol)
        return jsonify({"error": f"backtest failed: {exc}"}), 500

    payload = BacktestAdapter(result).to_all()
    payload["config"].update(
        {"timeframe": timeframe, "from_date": from_date, "to_date": to_date,
         "engine": engine}
    )
    _summarise(payload, f"run/{strategy}", params)
    return jsonify(payload), 200


# ---------------------------------------------------------------------------
# Parallel multi-slot backtest
# ---------------------------------------------------------------------------


@backtest_bp.post("/api/backtest/run-many")
def run_many() -> tuple:
    data = request.get_json(silent=True) or {}
    shared = data.get("shared", {}) or {}
    slots = data.get("slots", []) or []
    if not slots:
        log.warning("[run-many] rejected: no slots (body keys=%s)", sorted(data))
        return jsonify({"error": "at least one slot is required"}), 400
    if len(slots) > 4:
        log.warning("[run-many] rejected: %d slots (max 4)", len(slots))
        return jsonify({"error": "a maximum of 4 slots is supported"}), 400

    symbol = shared.get("symbol", "DEMO")
    from_date = shared.get("from_date") or shared.get("from")
    to_date = shared.get("to_date") or shared.get("to")
    if not from_date or not to_date:
        log.warning("[run-many] rejected: shared.from_date/to_date missing (shared=%s)", shared)
        return jsonify({"error": "shared.from_date and shared.to_date are required"}), 400
    try:
        capital = float(shared.get("capital", 100_000))
    except (TypeError, ValueError):
        log.warning("[run-many] rejected: shared.capital=%r", shared.get("capital"))
        return jsonify({"error": "shared.capital must be a number"}), 400
    log.info(
        "[run-many] %d slots on %s %s..%s capital=%s — %s",
        len(slots), symbol, from_date, to_date, capital,
        ", ".join(
            f"#{sl.get('id')}:{sl.get('strategy')}@{sl.get('timeframe', '1D')}" for sl in slots
        ),
    )

    source_name = current_app.config.get("BACKTEST_SOURCE", "synthetic")

    # Calculate warmup start date
    try:
        from_dt = datetime.strptime(from_date, "%Y-%m-%d")
        warmup_start = (from_dt - timedelta(days=WARMUP_BARS * 2)).strftime("%Y-%m-%d")
    except ValueError:
        log.warning("[run-many] unparseable shared.from_date %r — no warmup applied", from_date)
        warmup_start = from_date

    # One PLAIN-DICT job per slot (P2.3): the work runs in a process pool,
    # so job params must be picklable plain data — no source objects, no
    # closures, no lambdas. Each worker process rebuilds its own source by
    # name (sources are deterministic/plain-constructor, so this is exact).
    jobs = [
        {
            "id": slot.get("id"),
            "strategy": slot.get("strategy"),
            "params": slot.get("params") or {},
            "timeframe": slot.get("timeframe", "1D"),
            "mode": str(slot.get("mode", "")).strip().lower(),
            "symbol": symbol,
            "from_date": from_date,
            "to_date": to_date,
            "warmup_start": warmup_start,
            "capital": capital,
            "source_name": source_name,
        }
        for slot in slots
    ]

    max_workers = min(4, len(slots))
    with timed(log, f"[run-many] {len(slots)} slots (process pool)", logging.INFO) as t:
        with ProcessPoolExecutor(max_workers=max_workers) as pool:
            # chunksize=1: each slot is its own task, so one crashing job
            # cannot swallow its neighbours' results.
            payloads = list(pool.map(run_single_backtest, jobs, chunksize=1))

    # Per-slot logging happens HERE, in the web process: worker-process log
    # records do not surface in the web process's log capture (process
    # boundary), so the endpoint re-emits the canonical slot lines — with
    # the request id — and the worker's traceback rides back in the payload.
    results: dict[str, Any] = {}
    for job, payload in zip(jobs, payloads):
        sid = str(job["id"])
        results[sid] = payload
        if isinstance(payload, dict) and "error" in payload:
            traceback_text = payload.pop("traceback", None)
            log.warning("[slot %s] failed: %s", sid, payload["error"])
            if traceback_text:
                log.debug("[slot %s] traceback:\n%s", sid, traceback_text)
        else:
            _summarise(
                payload,
                f"slot {sid}/{job.get('strategy')}@{job.get('timeframe', '1D')}",
                job.get("params") or {},
            )
    failed = [k for k, v in results.items() if isinstance(v, dict) and "error" in v]
    log.info("[run-many] done in %.1f ms: %d ok, %d failed%s",
             t.elapsed_ms, len(results) - len(failed), len(failed),
             f" (slots {', '.join(failed)})" if failed else "")

    return jsonify({"results": results}), 200


# ---------------------------------------------------------------------------
# Process-pool worker (ticket P2.3)
# ---------------------------------------------------------------------------


def run_single_backtest(params: dict) -> dict:
    """Run ONE backtest slot — the top-level, picklable worker for the pool.

    Takes plain data only (strings/numbers/dicts — no source objects, no
    closures, no lambdas) and returns the plain-JSON slot payload, or
    ``{"error": ...}`` — one bad job must never poison the others. The
    worker rebuilds its source by name, so no unpicklable state crosses the
    process boundary. (Worker log lines drop the request id: contextvars do
    not cross processes; the endpoint's own log lines carry it.)
    """
    sid = params.get("id")
    strategy = params.get("strategy")
    symbol = str(params.get("symbol", "DEMO"))
    from_date = str(params["from_date"])
    to_date = str(params["to_date"])
    capital = float(params.get("capital", 100_000))
    try:
        resolution = _resolve_strategy(strategy)
        if isinstance(resolution, str):
            raise ValueError(resolution)
        slot_params = params.get("params") or {}
        timeframe = params.get("timeframe", "1D")
        interval = _interval(timeframe)
        mode = str(params.get("mode", "")).strip().lower()
        _check_params(resolution, slot_params, f"slot {sid}")

        source = build_source(str(params.get("source_name", "synthetic")))
        candles_full = source.get_candles(symbol, str(params["warmup_start"]), to_date, interval)
        log.debug("[slot %s] %s: %d bars @ %s (%s)",
                  sid, strategy, len(candles_full), interval, mode or "driver")

        if mode == QUICK_SCREEN:
            result = run_on_candles(
                candles_full, strategy, slot_params, symbol,
                BacktestConfig(initial_capital=capital),
            )
            result = _trim_to_range(result, from_date, to_date)
            engine = "quick_screen"
        else:
            result = _run_driver(candles_full, strategy, slot_params, symbol, capital)
            engine = "backtest_driver"

        payload = BacktestAdapter(result).to_all()
        payload["config"].update(
            {
                "timeframe": timeframe,
                "from_date": from_date,
                "to_date": to_date,
                "engine": engine,
            }
        )
        # NOTE: the [result]/[slot ...] INFO lines are emitted by the ENDPOINT
        # (web process) after the pool returns — worker-process log records
        # do not surface in the web process's log capture.
        return payload
    except Exception as exc:  # noqa: BLE001 — one bad job must not poison others
        traceback_text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        log.debug("[slot %s] failed: %s", sid, exc)  # child-side only
        return {"error": f"{exc.__class__.__name__}: {exc}", "traceback": traceback_text}
