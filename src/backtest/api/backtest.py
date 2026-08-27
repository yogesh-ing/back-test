"""Backtest endpoints (PRD Tasks 1.5 + 1.6).

* ``POST /api/backtest/run``      — single strategy deep dive
* ``POST /api/backtest/run-many`` — 2-4 slots in parallel via ThreadPoolExecutor
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Any

import pandas as pd
from flask import Blueprint, current_app, jsonify, request

from backtest.adapters.backtest_adapter import BacktestAdapter
from backtest.engine.backtester import BacktestConfig
from backtest.runner import build_source, run_on_candles
from backtest.strategy.registry import get_strategy

backtest_bp = Blueprint("backtest_api", __name__)

# Number of extra bars to load before start_date for strategy warmup.
# Set to 0 so the run covers exactly the requested range and the result
# matches a direct run over the same candles (indicators simply ramp over
# the first bars, as they do in a standalone backtest).
WARMUP_BARS = 0

_TIMEFRAME_TO_INTERVAL = {
    "1D": "day", "D": "day", "DAY": "day", "1D": "day",
    "1W": "week", "W": "week",
    "1H": "hour", "H": "hour",
    "4H": "4hour",
    "15M": "15minute",
    "5M": "5minute",
}


def _interval(timeframe: str | None) -> str:
    if not timeframe:
        return "day"
    return _TIMEFRAME_TO_INTERVAL.get(str(timeframe).upper(), "day")


def _source() -> Any:
    name = current_app.config.get("BACKTEST_SOURCE", "synthetic")
    return build_source(name)


def _candles(symbol: str, from_date: str, to_date: str, timeframe: str):
    return _source().get_candles(symbol, from_date, to_date, _interval(timeframe))


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
    err = _resolve_strategy(strategy)
    if isinstance(err, str):
        return jsonify({"error": err}), 400

    symbol = data.get("symbol", "DEMO")
    from_date = data.get("from_date") or data.get("from")
    to_date = data.get("to_date") or data.get("to")
    if not from_date or not to_date:
        return jsonify({"error": "from_date and to_date are required"}), 400
    if from_date > to_date:
        return jsonify({"error": "from_date must be <= to_date"}), 400

    try:
        capital = float(data.get("capital", 100_000))
    except (TypeError, ValueError):
        return jsonify({"error": "capital must be a number"}), 400

    params = data.get("params") or {}
    timeframe = data.get("timeframe", "1D")

    # Calculate warmup start date (extra bars before from_date for strategy warmup)
    try:
        from_dt = datetime.strptime(from_date, "%Y-%m-%d")
        warmup_start = (from_dt - timedelta(days=WARMUP_BARS * 2)).strftime("%Y-%m-%d")
    except ValueError:
        warmup_start = from_date

    try:
        candles_full = _candles(symbol, warmup_start, to_date, timeframe)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"data error: {exc}"}), 400

    # Run strategy on full dataset (includes warmup bars)
    config = BacktestConfig(initial_capital=capital)
    try:
        result = run_on_candles(candles_full, strategy, params, symbol, config)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"backtest failed: {exc}"}), 500

    # Trim results to the requested date range (strip warmup period)
    result = _trim_to_range(result, from_date, to_date)

    payload = BacktestAdapter(result).to_all()
    payload["config"].update(
        {"timeframe": timeframe, "from_date": from_date, "to_date": to_date}
    )
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
        return jsonify({"error": "at least one slot is required"}), 400
    if len(slots) > 4:
        return jsonify({"error": "a maximum of 4 slots is supported"}), 400

    symbol = shared.get("symbol", "DEMO")
    from_date = shared.get("from_date") or shared.get("from")
    to_date = shared.get("to_date") or shared.get("to")
    if not from_date or not to_date:
        return jsonify({"error": "shared.from_date and shared.to_date are required"}), 400
    try:
        capital = float(shared.get("capital", 100_000))
    except (TypeError, ValueError):
        return jsonify({"error": "shared.capital must be a number"}), 400

    source = _source()

    # Calculate warmup start date
    try:
        from_dt = datetime.strptime(from_date, "%Y-%m-%d")
        warmup_start = (from_dt - timedelta(days=WARMUP_BARS * 2)).strftime("%Y-%m-%d")
    except ValueError:
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
            candles_full = source.get_candles(symbol, warmup_start, to_date, interval)
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
            return sid, payload
        except Exception as exc:  # noqa: BLE001 — one bad slot must not poison others
            return sid, {"error": str(exc)}

    results: dict[str, Any] = {}
    max_workers = min(4, len(slots))
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        for sid, payload in pool.map(run_slot, slots):
            results[str(sid)] = payload

    return jsonify({"results": results}), 200
