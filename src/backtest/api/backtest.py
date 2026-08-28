"""Backtest endpoints (PRD Tasks 1.5 + 1.6).

* ``POST /api/backtest/run``      — single strategy deep dive
* ``POST /api/backtest/run-many`` — 2-4 slots in parallel via ThreadPoolExecutor

Both log the resolved request, the bars fetched, the engine summary and any
failure with a traceback, so a UI error toast can always be traced back to one
line in the server log (the id is quoted back in the JSON body as
``request_id``).
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Any

import pandas as pd
from flask import Blueprint, current_app, jsonify, request

from backtest.adapters.backtest_adapter import BacktestAdapter
from backtest.engine.backtester import BacktestConfig
from backtest.logging_config import get_logger, timed, with_request_context
from backtest.runner import build_source, run_on_candles
from backtest.strategy.registry import get_strategy

backtest_bp = Blueprint("backtest_api", __name__)
log = get_logger(__name__)

# Number of extra bars to load before start_date for strategy warmup.
# Set to 0 so the run covers exactly the requested range and the result
# matches a direct run over the same candles (indicators simply ramp over
# the first bars, as they do in a standalone backtest).
WARMUP_BARS = 0

_TIMEFRAME_TO_INTERVAL = {
    "1D": "day", "D": "day", "DAY": "day",
    "1W": "week", "W": "week",
    "1H": "hour", "H": "hour",
    "4H": "4hour",
    "15M": "15minute",
    "5M": "5minute",
}

#: Timeframes the UI offers. Aliases in the map above are tolerated as well;
#: anything else falls back to ``day`` with a WARNING (gap G6/G11 will harden it).
SUPPORTED_TIMEFRAMES = ("1D", "1H", "4H", "1W", "15M", "5M")


def _interval(timeframe: str | None) -> str:
    """Resolve a UI timeframe to a source interval (unknown → ``day``).

    Kept permissive on purpose: rejecting an unsupported timeframe is part of
    gap G6/G11. Until then, say so loudly in the log instead of silently.
    """
    if not timeframe:
        return "day"
    key = str(timeframe).upper()
    if key not in _TIMEFRAME_TO_INTERVAL:
        log.warning(
            "[timeframe] unsupported timeframe %r — falling back to 'day' (supported: %s)",
            timeframe, ", ".join(SUPPORTED_TIMEFRAMES),
        )
    return _TIMEFRAME_TO_INTERVAL.get(key, "day")


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
    log.info(
        "[run] strategy=%s symbol=%s timeframe=%s→%s range=%s..%s capital=%s params=%s",
        strategy, symbol, timeframe, interval, from_date, to_date, capital, params,
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

    # Run strategy on full dataset (includes warmup bars)
    config = BacktestConfig(initial_capital=capital)
    try:
        with timed(log, f"[run] {strategy} on {symbol}", logging.DEBUG):
            result = run_on_candles(candles_full, strategy, params, symbol, config)
    except ValueError as exc:
        log.warning("[run] %s rejected input: %s", strategy, exc)
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:  # noqa: BLE001
        log.exception("[run] %s crashed on %s", strategy, symbol)
        return jsonify({"error": f"backtest failed: {exc}"}), 500

    # Trim results to the requested date range (strip warmup period)
    before = len(result.equity)
    result = _trim_to_range(result, from_date, to_date)
    if before != len(result.equity):
        log.debug("[trim] %d → %d bars for %s..%s", before, len(result.equity), from_date, to_date)

    payload = BacktestAdapter(result).to_all()
    payload["config"].update(
        {"timeframe": timeframe, "from_date": from_date, "to_date": to_date}
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

    source = _source()

    # Calculate warmup start date
    try:
        from_dt = datetime.strptime(from_date, "%Y-%m-%d")
        warmup_start = (from_dt - timedelta(days=WARMUP_BARS * 2)).strftime("%Y-%m-%d")
    except ValueError:
        log.warning("[run-many] unparseable shared.from_date %r — no warmup applied", from_date)
        warmup_start = from_date

    def run_slot(slot: dict) -> tuple[Any, dict]:
        sid = slot.get("id")
        try:
            strategy = slot.get("strategy")
            resolution = _resolve_strategy(strategy)
            if isinstance(resolution, str):
                raise ValueError(resolution)
            params = slot.get("params") or {}
            timeframe = slot.get("timeframe", "1D")
            interval = _interval(timeframe)
            _check_params(resolution, params, f"slot {sid}")
            candles_full = source.get_candles(symbol, warmup_start, to_date, interval)
            log.debug("[slot %s] %s: %d bars @ %s", sid, strategy, len(candles_full), interval)
            config = BacktestConfig(initial_capital=capital)
            result = run_on_candles(candles_full, strategy, params, symbol, config)
            result = _trim_to_range(result, from_date, to_date)
            payload = BacktestAdapter(result).to_all()
            payload["config"].update(
                {
                    "timeframe": timeframe,
                    "from_date": from_date,
                    "to_date": to_date,
                }
            )
            _summarise(payload, f"slot {sid}/{strategy}@{timeframe}", params)
            return sid, payload
        except Exception as exc:  # noqa: BLE001 — one bad slot must not poison others
            log.warning("[slot %s] failed: %s: %s", sid, exc.__class__.__name__, exc)
            log.debug("[slot %s] traceback", sid, exc_info=True)
            return sid, {"error": str(exc)}

    results: dict[str, Any] = {}
    max_workers = min(4, len(slots))
    # Run each slot inside a copy of this request's context so the worker threads
    # keep the request id on their log lines (contextvars do not cross threads).
    run_slot_ctx = with_request_context(run_slot)
    with timed(log, f"[run-many] {len(slots)} slots", logging.INFO) as t:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            for sid, payload in pool.map(run_slot_ctx, slots):
                results[str(sid)] = payload
    failed = [k for k, v in results.items() if isinstance(v, dict) and "error" in v]
    log.info("[run-many] done in %.1f ms: %d ok, %d failed%s",
             t.elapsed_ms, len(results) - len(failed), len(failed),
             f" (slots {', '.join(failed)})" if failed else "")

    return jsonify({"results": results}), 200
