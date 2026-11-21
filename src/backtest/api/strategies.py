"""Strategy catalogue endpoints (PRD Task 1.3)."""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from backtest.logging_config import get_logger
from backtest.strategy.registry import get_all, get_params

strategies_bp = Blueprint("strategies_api", __name__)
log = get_logger(__name__)


@strategies_bp.get("/api/strategies")
def list_strategies() -> tuple:
    """Return ``[{name, description, version, author, signal_kind, ...}]``, sorted alphabetically.

    ``?venue=backtest`` (also ``compare``) returns ALL strategies — equity and
    option — each tagged with ``signal_kind``. The frontend decides which engine
    to run: equity strategies go to ``/api/backtest/run``, option strategies go
    to ``/api/backtest/options-model``. ``?venue=forward`` (or no venue)
    returns everything as well; options belong there, priced from the broker.
    """
    venue = str(request.args.get("venue", "")).strip().lower()
    catalogue = [
        {
            "name": s["name"],
            "description": s["description"],
            "description_sections": s.get("description_sections"),
            "version": s["version"],
            "author": s["author"],
            "params": s["params"],
            "signal_kind": s["signal_kind"],
            "eligible_instruments": s.get("eligible_instruments"),
            "default_segment": s.get("default_segment"),
            "readable_criteria": s.get("readable_criteria"),
            "execution": s.get("execution"),
        }
        for s in get_all()
    ]
    log.debug("/api/strategies → %d entries (venue=%s)", len(catalogue), venue)
    if not catalogue:
        log.warning(
            "/api/strategies returned an empty catalogue — check that "
            "src/backtest/strategies/*.py exist and import cleanly"
        )
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
