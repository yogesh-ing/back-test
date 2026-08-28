"""Dynamic symbol list endpoint (PRD Task 4)."""

from __future__ import annotations

from flask import Blueprint, jsonify, current_app

from backtest.data.db_source import DbSource
from backtest.logging_config import get_logger

symbols_bp = Blueprint("symbols_api", __name__)
log = get_logger(__name__)

# Process-level cache: symbols don't change during a single run.
_CACHED_SYMBOLS: dict[str, list[str]] = {}


@symbols_bp.get("/api/symbols")
def list_symbols() -> tuple:
    """Return available DB symbols when source=db; otherwise empty list."""
    source = current_app.config.get("BACKTEST_SOURCE", "synthetic")
    timeframe = "day"

    if source != "db":
        # Backtest/Compare pages keep their own static symbol list in that mode,
        # so an empty payload here is expected, not an error.
        log.debug("/api/symbols: source=%s has no dynamic list (only 'db' does)", source)
        return jsonify({"symbols": [], "count": 0, "timeframe": timeframe}), 200

    # Use module-level cache so we don't hit DB on every request.
    cache_key = f"{timeframe}:{source}"
    if cache_key in _CACHED_SYMBOLS:
        syms = _CACHED_SYMBOLS[cache_key]
        return jsonify({"symbols": syms, "count": len(syms), "timeframe": timeframe}), 200

    try:
        syms = DbSource().list_symbols(timeframe=timeframe)
        _CACHED_SYMBOLS[cache_key] = syms
        log.info("/api/symbols: %d symbols @ %s", len(syms), timeframe)
        return jsonify({"symbols": syms, "count": len(syms), "timeframe": timeframe}), 200
    except Exception as exc:
        log.warning("Could not list symbols from the database: %s: %s",
                    exc.__class__.__name__, exc)
        log.debug("symbol listing traceback", exc_info=True)
        return jsonify({"error": "Database unavailable", "symbols": [], "count": 0}), 500
