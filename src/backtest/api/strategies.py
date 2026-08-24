"""Strategy catalogue endpoints (PRD Task 1.3)."""

from __future__ import annotations

from flask import Blueprint, jsonify

from backtest.strategy.registry import get_all, get_params

strategies_bp = Blueprint("strategies_api", __name__)


@strategies_bp.get("/api/strategies")
def list_strategies() -> tuple:
    """Return ``[{name, description, version, author}]``, sorted alphabetically."""
    catalogue = [
        {
            "name": s["name"],
            "description": s["description"],
            "version": s["version"],
            "author": s["author"],
        }
        for s in get_all()
    ]
    return jsonify(catalogue), 200


@strategies_bp.get("/api/strategies/<name>/params")
def strategy_params(name: str) -> tuple:
    """Return the normalised param schema for dynamic form rendering."""
    try:
        schema = get_params(name)
    except KeyError:
        return jsonify({"error": f"unknown strategy: {name}"}), 404
    return jsonify(schema), 200
