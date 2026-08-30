"""Strategy catalogue endpoints (PRD Task 1.3)."""

from __future__ import annotations

from flask import Blueprint, jsonify

from backtest.logging_config import get_logger
from backtest.strategy.registry import get_all, get_params

strategies_bp = Blueprint("strategies_api", __name__)
log = get_logger(__name__)


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
    log.debug("/api/strategies → %d entries", len(catalogue))
    if not catalogue:
        log.warning("/api/strategies returned an empty catalogue — check that "
                    "src/backtest/strategies/*.py exist and import cleanly")
    return jsonify(catalogue), 200


@strategies_bp.get("/api/strategies/<name>/params")
def strategy_params(name: str) -> tuple:
    """Return the normalised param schema for dynamic form rendering."""
    try:
        schema = get_params(name)
    except KeyError as exc:
        log.warning("/api/strategies/%s/params → 404 (%s)", name, exc)
        return jsonify({"error": f"unknown strategy: {name}"}), 404
    return jsonify(schema), 200
