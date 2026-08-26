"""Dynamic symbol list endpoint (PRD Task 4)."""

from __future__ import annotations

from flask import Blueprint, jsonify, current_app

from backtest.data.db_source import DbSource

symbols_bp = Blueprint("symbols_api", __name__)

# Process-level cache: symbols don't change during a single run.
_CACHED_SYMBOLS: dict[str, list[str]] = {}


@symbols_bp.get("/api/symbols")
def list_symbols() -> tuple:
    """Return available DB symbols when source=db; otherwise empty list."""
    source = current_app.config.get("BACKTEST_SOURCE", "synthetic")
    timeframe = "day"

    if source != "db":
        return jsonify({"symbols": [], "count": 0, "timeframe": timeframe}), 200

    # Use module-level cache so we don't hit DB on every request.
    cache_key = f"{timeframe}:{source}"
    if cache_key in _CACHED_SYMBOLS:
        syms = _CACHED_SYMBOLS[cache_key]
        return jsonify({"symbols": syms, "count": len(syms), "timeframe": timeframe}), 200

    try:
        syms = DbSource().list_symbols(timeframe=timeframe)
        _CACHED_SYMBOLS[cache_key] = syms
        return jsonify({"symbols": syms, "count": len(syms), "timeframe": timeframe}), 200
    except Exception as exc:
        current_app.logger.warning(f"[DB] Could not list symbols: {exc}")
        return jsonify({"error": "Database unavailable", "symbols": [], "count": 0}), 500
