"""Dynamic symbol list endpoint (PRD Task 4)."""

from __future__ import annotations

from flask import Blueprint, current_app, jsonify

from backtest.data.db_source import DbSource
from backtest.logging_config import get_logger

symbols_bp = Blueprint("symbols_api", __name__)
log = get_logger(__name__)

# Process-level cache: symbols don't change during a single run.
_CACHED_SYMBOLS: dict[str, list[str]] = {}

# Index-option lot sizes, cached for the process lifetime. Source of truth is
# the mStock scriptmaster (real exchange values, owner request 2026-09-22:
# "pull the lot size for all the instruments available"); the static map is
# the offline fallback so the form still works without a broker session.
_FALLBACK_LOT_SIZES: dict[str, int] = {
    "NIFTY": 65,
    "BANKNIFTY": 30,
    "FINNIFTY": 60,
    "MIDCPNIFTY": 120,
    "SENSEX": 20,
}
_CACHED_LOT_SIZES: dict[str, int] | None = None


@symbols_bp.get("/api/symbols/lot-sizes")
def lot_sizes() -> tuple:
    """Index → lot size, from the live mStock scriptmaster when a broker
    session exists; static fallback otherwise. Cached process-wide (the
    scriptmaster is large — ~8k option rows — and lots change only at
    exchange reseats, not intraday)."""
    global _CACHED_LOT_SIZES
    if _CACHED_LOT_SIZES is not None:
        return jsonify({"lot_sizes": _CACHED_LOT_SIZES, "source": "cache"}), 200
    try:
        import io
        import os

        import pandas as pd
        import requests

        from backtest.brokers.session_manager import get_session_manager
        from backtest.data.mstock_live_feed import _typea_headers

        token = get_session_manager().get_active_session_token()
        if not token:
            raise RuntimeError("no broker session")
        headers = _typea_headers(os.getenv("MSTOCK_API_KEY", ""), token)
        resp = requests.get(
            "https://api.mstock.trade/openapi/typea/instruments/scriptmaster",
            headers=headers,
            timeout=30,
        )
        resp.raise_for_status()
        frame = pd.read_csv(io.StringIO(resp.text), low_memory=False)
        frame.columns = [c.strip().lower() for c in frame.columns]
        ts_col = (
            "tradingsymbol" if "tradingsymbol" in frame.columns else "trading_symbol"
        )
        syms = frame[ts_col].astype(str).str.upper()
        mask = syms.str.match(r"^([A-Z]+)\d{2}[A-Z]{3}\d+(CE|PE)$")
        extracted = frame[mask].copy()
        extracted["underlying"] = syms[mask].str.extract(r"^([A-Z]+)")[0]
        lots = extracted.groupby("underlying")["lot_size"].first().to_dict()
        result = {
            str(k).strip().upper(): int(v)
            for k, v in lots.items()
            if str(k).strip().upper() and int(v) > 0
        }
        if result:
            _CACHED_LOT_SIZES = result
            return jsonify({"lot_sizes": result, "source": "mstock_live"}), 200
        raise RuntimeError("scriptmaster parse produced no rows")
    except Exception as exc:  # noqa: BLE001 — offline fallback keeps the form working
        log.warning(
            "lot-sizes: live fetch failed (%s: %s) — static fallback",
            exc.__class__.__name__, exc,
        )
        return jsonify({"lot_sizes": _FALLBACK_LOT_SIZES, "source": "fallback"}), 200


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
        log.warning("Could not list symbols from the database: %s: %s", exc.__class__.__name__, exc)
        log.debug("symbol listing traceback", exc_info=True)
        return jsonify({"error": "Database unavailable", "symbols": [], "count": 0}), 500
