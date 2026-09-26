"""Portfolio intelligence API — Greeks, concentration, correlation, regime, alerts.

All reads are scoped by ``?mode=`` (omitted = every bucket, ``paper``,
``live``) with the same validation as the portfolio API. Responses share one
envelope: ``{"success": true, "<section>": {...}}``; errors are
``{"success": false, "error": "..."}`` with a 4xx status.

Routes
------
GET  /api/monitor/snapshot          every section + active alerts (the page)
GET  /api/monitor/greeks            §1.1 Greeks, scenarios, Greek alerts
GET  /api/monitor/concentration     §1.2 exposure / clustering
GET  /api/monitor/correlation       §1.2 strategy P&L correlation (?lookback=)
GET  /api/monitor/regime            §1.3 regime + fit (?symbol=)
GET  /api/monitor/alerts            active alerts (+ ?history=1 resolved)
POST /api/monitor/alerts/<id>/ack   acknowledge one alert
GET  /api/monitor/config            effective limits (read-only)
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any, Optional, Tuple

from flask import Blueprint, Response, current_app, jsonify, request

from backtest.forward.paper_runner import VALID_INSTANCE_MODES

log = logging.getLogger("backtest.api.monitor")

monitor_bp = Blueprint("monitor_api", __name__)

HISTORY_DAYS = 120


def _error(message: str, status: int = 400) -> Tuple[Response, int]:
    return jsonify({"success": False, "error": message}), status


def _mode() -> Optional[str]:
    raw = request.args.get("mode") or None
    if raw is None or str(raw).strip().lower() == "all":
        return None
    mode = str(raw).strip().lower()
    if mode not in VALID_INSTANCE_MODES:
        raise ValueError(f"mode must be one of {VALID_INSTANCE_MODES} or 'all', got {raw!r}")
    return mode


def _history_loader(app: Any):
    """Daily history from the app's configured data source (regime fallback)."""
    source_name = app.config.get("BACKTEST_SOURCE", "synthetic")

    def load(symbol: str):
        from backtest.runner import build_source

        end = date.today()
        start = end - timedelta(days=HISTORY_DAYS)
        frame = build_source(source_name).get_candles(
            symbol, start.isoformat(), end.isoformat(), "1day"
        )
        if frame is not None:
            frame.attrs["source"] = source_name
        return frame

    return load


def _monitor() -> Any:
    from backtest.forward.portfolio_manager import get_portfolio_manager

    monitor = get_portfolio_manager().get_monitor()
    if monitor.history_loader is None:
        monitor.history_loader = _history_loader(current_app._get_current_object())
    symbol = current_app.config.get("CURRENCY_SYMBOL")
    if symbol:
        monitor.config.currency_symbol = symbol
    return monitor


def _section(name: str) -> Tuple[Response, int]:
    try:
        mode = _mode()
    except ValueError as exc:
        return _error(str(exc))
    lookback = request.args.get("lookback", type=int)
    if lookback is not None and lookback < 2:
        return _error("lookback must be >= 2")
    symbol = (request.args.get("symbol") or "").strip() or None
    snap = _monitor().snapshot(mode=mode, sections=[name], symbol=symbol, lookback=lookback)
    return jsonify({"success": True, "mode": snap["mode"], "as_of": snap["as_of"],
                    name: snap.get(name), "alert_counts": snap["alert_counts"]}), 200


@monitor_bp.get("/api/monitor/snapshot")
def snapshot() -> Tuple[Response, int]:
    try:
        mode = _mode()
    except ValueError as exc:
        return _error(str(exc))
    lookback = request.args.get("lookback", type=int)
    if lookback is not None and lookback < 2:
        return _error("lookback must be >= 2")
    symbol = (request.args.get("symbol") or "").strip() or None
    snap = _monitor().snapshot(mode=mode, symbol=symbol, lookback=lookback)
    return jsonify({"success": True, "snapshot": snap}), 200


@monitor_bp.get("/api/monitor/greeks")
def greeks() -> Tuple[Response, int]:
    return _section("greeks")


@monitor_bp.get("/api/monitor/concentration")
def concentration() -> Tuple[Response, int]:
    return _section("concentration")


@monitor_bp.get("/api/monitor/correlation")
def correlation() -> Tuple[Response, int]:
    return _section("correlation")


@monitor_bp.get("/api/monitor/regime")
def regime() -> Tuple[Response, int]:
    return _section("regime")


@monitor_bp.get("/api/monitor/alerts")
def alerts() -> Tuple[Response, int]:
    try:
        mode = _mode()
    except ValueError as exc:
        return _error(str(exc))
    monitor = _monitor()
    scope = mode or "all"
    payload = {
        "success": True,
        "scope": scope,
        "alerts": monitor.alerts.active(scope),
        "counts": monitor.alerts.counts(scope),
        "last_sweep": monitor.last_sweep,
    }
    if request.args.get("history") in ("1", "true", "yes"):
        limit = max(1, min(request.args.get("limit", 100, type=int) or 100, 500))
        payload["history"] = monitor.alerts.history(scope, limit=limit)
    return jsonify(payload), 200


@monitor_bp.post("/api/monitor/alerts/<alert_id>/ack")
def acknowledge(alert_id: str) -> Tuple[Response, int]:
    rec = _monitor().alerts.acknowledge(alert_id)
    if rec is None:
        return _error(f"no active alert {alert_id!r}", 404)
    return jsonify({"success": True, "alert": rec}), 200


@monitor_bp.get("/api/monitor/config")
def config_view() -> Tuple[Response, int]:
    return jsonify({"success": True, "config": _monitor().config.to_dict()}), 200
