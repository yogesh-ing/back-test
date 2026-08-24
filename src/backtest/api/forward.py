"""Forward test endpoints (PRD Task 4.3).

* ``POST /api/forward/start``  {strategy, symbol, timeframe, from_date, to_date, capital, params}
* ``POST /api/forward/stop``
* ``GET  /api/forward/status`` → {status, metrics, equity, drawdown, trades, positions, progress}

Sandbox note: there is no live market feed available (mStock needs credentials),
so forward testing is implemented as an in-process **paper-trading replay**. On
``/start`` the strategy is run over the candle range once; on each ``/status``
poll the revealed bar count advances, and metrics/equity/trades/positions are
computed on the prefix via ``BacktestAdapter`` + ``compute_metrics`` — the same
shape the Backtest page consumes, so the frontend components are reusable. State
is server-side (survives a page refresh; DB persistence is V2).
"""

from __future__ import annotations

import threading
from typing import Any

from flask import Blueprint, current_app, jsonify, request

from backtest.adapters.backtest_adapter import BacktestAdapter
from backtest.api.backtest import _interval, _resolve_strategy
from backtest.engine.backtester import BacktestConfig, BacktestResult
from backtest.engine.metrics import compute_metrics
from backtest.runner import build_source, run_on_candles

forward_bp = Blueprint("forward_api", __name__)

_lock = threading.Lock()
_session: dict[str, Any] = {
    "result": None,
    "candles": None,
    "body": None,
    "symbol": "DEMO",
    "revealed": 0,
    "total": 0,
    "status": "idle",
}


def _reset_session() -> None:
    _session.update(
        result=None, candles=None, body=None, symbol="DEMO",
        revealed=0, total=0, status="idle",
    )


@forward_bp.post("/api/forward/start")
def start() -> tuple:
    data = request.get_json(silent=True) or {}

    resolution = _resolve_strategy(data.get("strategy"))
    if isinstance(resolution, str):
        return jsonify({"error": resolution}), 400

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
    source = build_source(current_app.config.get("BACKTEST_SOURCE", "synthetic"))

    try:
        candles = source.get_candles(symbol, from_date, to_date, _interval(timeframe))
        config = BacktestConfig(initial_capital=capital)
        result = run_on_candles(candles, data.get("strategy"), params, symbol, config)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"forward start failed: {exc}"}), 400

    warmup = min(20, len(candles))
    with _lock:
        _session.update(
            result=result, candles=candles, body=data, symbol=symbol,
            revealed=warmup, total=len(candles), status="running",
        )
    return jsonify({"status": "running", "total": len(candles), "revealed": warmup}), 200


@forward_bp.post("/api/forward/stop")
def stop() -> tuple:
    with _lock:
        if _session["result"] is None:
            return jsonify({"status": "idle"}), 200
        _session["status"] = "stopped"
    return jsonify({"status": "stopped"}), 200


@forward_bp.get("/api/forward/status")
def status() -> tuple:
    with _lock:
        if _session["result"] is None:
            return jsonify({"status": "idle"}), 200

        total = _session["total"]
        if _session["status"] == "running":
            step = max(1, total // 60)
            _session["revealed"] = min(total, _session["revealed"] + step)
            if _session["revealed"] >= total:
                _session["status"] = "stopped"  # replay finished

        return jsonify(_build_snapshot()), 200


def _build_snapshot() -> dict[str, Any]:
    """Build a live snapshot from the revealed prefix (called under lock)."""
    full: BacktestResult = _session["result"]
    candles = _session["candles"]
    n = _session["revealed"]
    total = _session["total"]

    partial = BacktestResult(
        equity=full.equity.iloc[:n],
        returns=full.returns.iloc[:n],
        position=full.position.iloc[:n],
        candles=candles.iloc[:n],
        config=full.config,
        metrics={},
    )
    partial.metrics = compute_metrics(partial)
    # carry over metadata that compute_metrics() doesn't emit (adapter reads these)
    partial.metrics["strategy"] = full.metrics.get("strategy", "")
    partial.metrics["symbol"] = full.metrics.get("symbol", _session["symbol"])
    adapter = BacktestAdapter(partial)
    out = adapter.to_all()

    # trades: drop the adapter's forced-close of a still-open position
    trades = adapter.to_trades()
    holding = bool(len(partial.position) and float(partial.position.iloc[-1]) != 0)
    positions: list[dict[str, Any]] = []
    if holding and trades:
        open_trade = trades.pop()
        cur_price = float(candles["close"].iloc[:n].iloc[-1])
        entry = open_trade["entry"]
        side = open_trade["side"]
        if side == "LONG":
            pct = (cur_price / entry - 1) * 100 if entry else 0.0
        else:
            pct = (entry / cur_price - 1) * 100 if cur_price else 0.0
        positions = [{
            "symbol": _session["symbol"],
            "side": side,
            "entry": entry,
            "entry_date": open_trade["date"],
            "current": round(cur_price, 2),
            "unrealized_pnl_pct": round(pct, 2),
        }]

    out["trades"] = trades
    out["positions"] = positions
    out["status"] = _session["status"]
    out["progress"] = {
        "revealed": n,
        "total": total,
        "pct": round(n / total * 100, 1) if total else 0.0,
    }
    return out
