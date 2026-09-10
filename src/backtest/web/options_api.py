"""Options trading UI & API — positions, structures, Greeks, expiry alerts.

Phase 7 of the options PRD.  These routes back the ``/options`` dashboard
page and its JSON API.  The dashboard renders whatever the in-process
:class:`~backtest.options.paper_trading.OptionPaperBroker` holds — V1 is
paper-only, so no live credentials are touched here.

Usage::

    # in app.py
    from backtest.web.options_api import register_options_routes
    register_options_routes(app)
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from datetime import datetime
from typing import Any

from flask import Flask, jsonify, render_template, request

from backtest.options.paper_trading import OptionPaperBroker, PositionStatus
from backtest.options.portfolio_greeks import PortfolioGreeksCalculator
from backtest.options.expiry import ExpiryManager, ExpiryAlertType

logger = logging.getLogger("backtest.web.options")

# ---------------------------------------------------------------------------
# Singleton broker for the UI (V1: one paper book per process)
# ---------------------------------------------------------------------------

_broker: OptionPaperBroker | None = None
_expiry_manager: ExpiryManager | None = None


def get_option_broker() -> OptionPaperBroker:
    """Return the process-wide paper option broker (create on first use)."""
    global _broker
    if _broker is None:
        _broker = OptionPaperBroker(capital=1_000_000.0)
    return _broker


def get_expiry_manager() -> ExpiryManager:
    """Return the process-wide expiry manager."""
    global _expiry_manager
    if _expiry_manager is None:
        _expiry_manager = ExpiryManager(get_option_broker())
    return _expiry_manager


def reset_option_state() -> None:
    """Reset the singleton broker/manager (used by tests)."""
    global _broker, _expiry_manager
    _broker = None
    _expiry_manager = None


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def register_options_routes(app: Flask) -> None:
    """Attach the options page + JSON API to the Flask app."""

    # ------------------------------------------------------------------
    # Page
    # ------------------------------------------------------------------
    @app.get("/options")
    def options_page() -> Any:
        return render_template("options.html", active="options")

    # ------------------------------------------------------------------
    # API — summary
    # ------------------------------------------------------------------
    @app.get("/api/options/summary")
    def options_summary() -> tuple:
        """One call the dashboard polls: positions, structures, Greeks, alerts."""
        broker = get_option_broker()
        manager = get_expiry_manager()

        open_positions = broker.get_open_positions()
        structures = broker.get_open_structures()

        greeks_calc = PortfolioGreeksCalculator()
        portfolio_greeks = greeks_calc.calculate(
            open_positions,
            spot_prices=request.args.get("spot", type=dict) or None,
        )

        return (
            jsonify(
                {
                    "capital": float(broker.capital),
                    "available_cash": float(broker.available_cash),
                    "total_equity": float(broker.total_equity),
                    "realized_pnl": float(broker.total_realized_pnl),
                    "margin_used": float(broker.total_margin_used),
                    "open_position_count": len(open_positions),
                    "open_structure_count": len(structures),
                    "greeks": portfolio_greeks.to_dict(),
                    "positions": [_position_to_dict(p) for p in open_positions],
                    "structures": [_structure_to_dict(s) for s in structures],
                    "alerts": [a.to_dict() for a in manager.get_alerts()],
                }
            ),
            200,
        )

    # ------------------------------------------------------------------
    # API — positions
    # ------------------------------------------------------------------
    @app.get("/api/options/positions")
    def options_positions() -> tuple:
        broker = get_option_broker()
        status_filter = request.args.get("status", "open")
        positions = (
            broker.get_open_positions()
            if status_filter == "open"
            else list(broker._positions.values())
        )
        return (
            jsonify({"positions": [_position_to_dict(p) for p in positions]}),
            200,
        )

    # ------------------------------------------------------------------
    # API — close a structure
    # ------------------------------------------------------------------
    @app.post("/api/options/structures/<structure_id>/close")
    def close_structure(structure_id: str) -> tuple:
        """Manually close a structure at current LTP (FakeQuoteProvider in V1)."""
        from backtest.options.paper_trading import FakeQuoteProvider

        broker = get_option_broker()
        quotes = FakeQuoteProvider(default_price=100.0)

        try:
            pnl = broker.close_structure(structure_id, quotes)
        except ValueError as e:
            return jsonify({"error": str(e)}), 404

        return (
            jsonify(
                {
                    "structure_id": structure_id,
                    "realized_pnl": float(pnl),
                    "closed_at": datetime.utcnow().isoformat(),
                }
            ),
            200,
        )

    # ------------------------------------------------------------------
    # API — run expiry pipeline on demand
    # ------------------------------------------------------------------
    @app.post("/api/options/expiry/process")
    def process_expiry() -> tuple:
        from backtest.options.paper_trading import FakeQuoteProvider
        from backtest.options.expiry import StaticSettlementProvider

        manager = get_expiry_manager()
        quotes = FakeQuoteProvider(default_price=100.0)
        settlement = StaticSettlementProvider({"NIFTY": 24800.0, "BANKNIFTY": 52000.0})

        summary = manager.process_expiries(quotes, settlement)
        return jsonify(summary), 200

    # ------------------------------------------------------------------
    # API — Greeks detail
    # ------------------------------------------------------------------
    @app.get("/api/options/greeks")
    def options_greeks() -> tuple:
        broker = get_option_broker()
        calc = PortfolioGreeksCalculator()
        result = calc.calculate(broker.get_open_positions())
        return jsonify(result.to_dict()), 200


# ---------------------------------------------------------------------------
# Serializers
# ---------------------------------------------------------------------------

def _position_to_dict(p: OptionPaperBroker and Any) -> dict[str, Any]:
    """Serialize an OptionPosition for the UI."""
    from backtest.options.paper_trading import OptionPosition

    return {
        "position_id": p.position_id,
        "structure_id": p.structure_id,
        "strategy_name": p.strategy_name,
        "trading_symbol": p.trading_symbol,
        "underlying": p.underlying,
        "option_type": p.option_type,
        "strike": str(p.strike),
        "expiry": str(p.expiry) if p.expiry else None,
        "side": p.side,
        "quantity": p.quantity,
        "lot_size": p.lot_size,
        "total_quantity": p.total_quantity,
        "entry_price": float(p.entry_price),
        "current_price": float(p.current_price),
        "unrealized_pnl": float(p.unrealized_pnl),
        "realized_pnl": float(p.realized_pnl),
        "status": p.status.value,
        "opened_at": p.opened_at.isoformat(),
    }


def _structure_to_dict(s: Any) -> dict[str, Any]:
    """Serialize a StructurePosition for the UI."""
    return {
        "structure_id": s.structure_id,
        "structure_type": s.structure_type,
        "strategy_name": s.strategy_name,
        "underlying": s.underlying,
        "expiry": str(s.expiry) if s.expiry else None,
        "leg_count": len(s.legs),
        "total_entry_cost": float(s.total_entry_cost),
        "total_unrealized_pnl": float(s.total_unrealized_pnl),
        "legs": [_position_to_dict(leg) for leg in s.legs],
        "opened_at": s.opened_at.isoformat(),
    }
