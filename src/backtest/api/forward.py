"""Forward test endpoints — live paper trading via LiveForwardEngine.

* ``POST /api/forward/start``  {strategy, symbol, timeframe, capital, params}
* ``POST /api/forward/stop``
* ``GET  /api/forward/status`` → {status, equity, positions, trades, market_open, ...}
* ``GET  /api/forward/trades`` → [{id, symbol, side, entry, exit, pnl, ...}]
* ``GET  /api/forward/equity`` → [{ts, equity, drawdown_pct}]
"""

from __future__ import annotations

import json
import os
from typing import Any

from flask import Blueprint, current_app, jsonify, request
from sqlalchemy import text

from backtest.brokers.session_manager import get_session_manager

forward_bp = Blueprint("forward_api", __name__)

DB_URL = os.getenv("FORWARD_TEST_DB_URL", "")


def _get_engine():
    from sqlalchemy import create_engine
    return create_engine(DB_URL)


# ---------------------------------------------------------------------------
# POST /api/forward/start
# ---------------------------------------------------------------------------
@forward_bp.post("/api/forward/start")
def start() -> tuple:
    """Start a live forward test.

    Creates a row in ``forward_test_state``, then launches the
    ``LiveForwardEngine`` in a background thread.
    """
    data = request.get_json(silent=True) or {}

    # Server-side auth guard
    if not get_session_manager().is_authenticated():
        return jsonify({
            "success": False,
            "error": "broker_not_authenticated",
            "message": "Valid broker session required to start forward test",
        }), 403

    strategy = data.get("strategy", "")
    symbol = data.get("symbol", "DEMO")
    timeframe = data.get("timeframe", "1min")
    try:
        capital = float(data.get("capital", 100_000))
    except (TypeError, ValueError):
        return jsonify({"error": "capital must be a number"}), 400
    params = data.get("params") or {}

    if not strategy:
        return jsonify({"error": "strategy is required"}), 400

    # Check if there's already a running engine for this symbol+strategy
    engine_db = _get_engine()
    with engine_db.connect() as conn:
        existing = conn.execute(
            text("""
                SELECT id FROM forward_test_state
                WHERE symbol = :symbol AND strategy = :strategy AND status = 'running'
                LIMIT 1
            """),
            {"symbol": symbol, "strategy": strategy},
        ).fetchone()
        if existing:
            return jsonify({"error": "Forward test already running for this symbol+strategy",
                            "state_id": existing.id}), 409

        # Insert new state row
        result = conn.execute(
            text("""
                INSERT INTO forward_test_state
                    (strategy, symbol, timeframe, status, capital, params)
                VALUES
                    (:strategy, :symbol, :timeframe, 'running', :capital, :params)
                RETURNING id
            """),
            {
                "strategy": strategy,
                "symbol": symbol,
                "timeframe": timeframe,
                "capital": capital,
                "params": json.dumps(params),
            },
        )
        state_id = result.fetchone()[0]
        conn.commit()

    # Start the live engine
    from backtest.forward.live_engine import start_engine
    try:
        engine = start_engine(state_id, db_url=DB_URL)
    except Exception as exc:
        # Mark state as failed
        with engine_db.connect() as conn:
            conn.execute(
                text("UPDATE forward_test_state SET status = 'error' WHERE id = :id"),
                {"id": state_id},
            )
            conn.commit()
        return jsonify({"error": f"Failed to start engine: {exc}"}), 500

    return jsonify({
        "status": "running",
        "state_id": state_id,
        "symbol": symbol,
        "strategy": strategy,
    }), 200


# ---------------------------------------------------------------------------
# POST /api/forward/stop
# ---------------------------------------------------------------------------
@forward_bp.post("/api/forward/stop")
def stop() -> tuple:
    """Stop the running forward test engine."""
    data = request.get_json(silent=True) or {}
    state_id = data.get("state_id")

    if state_id:
        from backtest.forward.live_engine import stop_engine
        stop_engine(int(state_id))
        return jsonify({"status": "stopped", "state_id": state_id}), 200

    # Stop all running engines
    from backtest.forward.live_engine import stop_engine, _engines
    for sid in list(_engines.keys()):
        stop_engine(sid)

    return jsonify({"status": "stopped"}), 200


# ---------------------------------------------------------------------------
# GET /api/forward/status
# ---------------------------------------------------------------------------
@forward_bp.get("/api/forward/status")
def status() -> tuple:
    """Get live forward test status."""
    from backtest.forward.live_engine import _engines

    # Find the most recent running engine
    engine_db = _get_engine()
    with engine_db.connect() as conn:
        row = conn.execute(
            text("""
                SELECT id, strategy, symbol, timeframe, status, capital, params,
                       last_bar_ts, created_at, updated_at
                FROM forward_test_state
                ORDER BY id DESC
                LIMIT 1
            """),
        ).fetchone()

    if row is None:
        return jsonify({"status": "idle"}), 200

    # Check if engine is running in memory
    live_engine = _engines.get(row.id)

    if live_engine:
        engine_status = live_engine.get_status()
        engine_status["state_id"] = row.id
        return jsonify(engine_status), 200

    # Engine not in memory — load from DB
    with engine_db.connect() as conn:
        # Get latest trades
        trades = conn.execute(
            text("""
                SELECT * FROM forward_test_trades
                WHERE state_id = :sid
                ORDER BY id DESC
                LIMIT 50
            """),
            {"sid": row.id},
        ).fetchall()

        # Get latest equity
        equity_rows = conn.execute(
            text("""
                SELECT equity, unrealized_pnl, ts
                FROM forward_test_equity
                WHERE state_id = :sid
                ORDER BY id DESC
                LIMIT 1
            """),
            {"sid": row.id},
        ).fetchall()

        # Get open positions
        positions = conn.execute(
            text("""
                SELECT * FROM forward_test_trades
                WHERE state_id = :sid AND status = 'open'
            """),
            {"sid": row.id},
        ).fetchall()

    equity = float(equity_rows[0].equity) if equity_rows else float(row.capital)
    unrealized = float(equity_rows[0].unrealized_pnl) if equity_rows else 0

    trade_list = []
    for t in trades:
        trade_list.append({
            "id": t.id,
            "symbol": t.symbol,
            "side": t.side,
            "entry": float(t.entry_price),
            "exit": float(t.exit_price) if t.exit_price else None,
            "pnl": float(t.pnl) if t.pnl else None,
            "pnl_pct": float(t.pnl_pct) if t.pnl_pct else None,
            "status": t.status,
            "date": str(t.entry_ts),
        })

    position_list = []
    for p in positions:
        position_list.append({
            "symbol": p.symbol,
            "side": p.side,
            "entry": float(p.entry_price),
            "current": 0,  # Unknown without live engine
            "unrealized_pnl_pct": 0,
            "entry_date": str(p.entry_ts),
            "quantity": float(p.quantity),
        })

    return jsonify({
        "status": row.status,
        "state_id": row.id,
        "symbol": row.symbol,
        "strategy": row.strategy,
        "timeframe": row.timeframe,
        "capital": float(row.capital),
        "equity": equity,
        "unrealized_pnl": unrealized,
        "total_bars": 0,
        "total_trades": len([t for t in trades if t.status == "closed"]),
        "bars_in_memory": 0,
        "last_bar_ts": str(row.last_bar_ts) if row.last_bar_ts else None,
        "market_open": False,
        "positions": position_list,
        "trades": trade_list,
        "error": None,
    }), 200


# ---------------------------------------------------------------------------
# GET /api/forward/trades
# ---------------------------------------------------------------------------
@forward_bp.get("/api/forward/trades")
def trades() -> tuple:
    """Get trade history for the current or most recent forward test."""
    engine_db = _get_engine()
    with engine_db.connect() as conn:
        row = conn.execute(
            text("SELECT id FROM forward_test_state ORDER BY id DESC LIMIT 1"),
        ).fetchone()

    if not row:
        return jsonify([]), 200

    with engine_db.connect() as conn:
        rows = conn.execute(
            text("""
                SELECT * FROM forward_test_trades
                WHERE state_id = :sid
                ORDER BY id DESC
                LIMIT 100
            """),
            {"sid": row.id},
        ).fetchall()

    result = []
    for t in rows:
        result.append({
            "id": t.id,
            "symbol": t.symbol,
            "side": t.side,
            "entry": float(t.entry_price),
            "exit": float(t.exit_price) if t.exit_price else None,
            "pnl": float(t.pnl) if t.pnl else None,
            "pnl_pct": float(t.pnl_pct) if t.pnl_pct else None,
            "status": t.status,
            "entry_date": str(t.entry_ts),
            "exit_date": str(t.exit_ts) if t.exit_ts else None,
        })

    return jsonify(result), 200


# ---------------------------------------------------------------------------
# GET /api/forward/equity
# ---------------------------------------------------------------------------
@forward_bp.get("/api/forward/equity")
def equity() -> tuple:
    """Get equity curve for charting."""
    engine_db = _get_engine()
    with engine_db.connect() as conn:
        row = conn.execute(
            text("SELECT id FROM forward_test_state ORDER BY id DESC LIMIT 1"),
        ).fetchone()

    if not row:
        return jsonify([]), 200

    with engine_db.connect() as conn:
        rows = conn.execute(
            text("""
                SELECT ts, equity, unrealized_pnl
                FROM forward_test_equity
                WHERE state_id = :sid
                ORDER BY ts ASC
            """),
            {"sid": row.id},
        ).fetchall()

    result = [{"ts": str(r.ts), "equity": float(r.equity)} for r in rows]
    return jsonify(result), 200
