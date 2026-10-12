"""Strategy catalogue endpoints (PRD Task 1.3)."""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from backtest.logging_config import get_logger
from backtest.strategy.registry import get_all, get_params

strategies_bp = Blueprint("strategies_api", __name__)
log = get_logger(__name__)


@strategies_bp.get("/api/strategies")
def list_strategies() -> tuple:
    """Return ``[{name, description, version, author}]``, sorted alphabetically.

    ``?venue=backtest`` (also ``compare``) drops option strategies from the
    catalogue. This is a presentation rule with an engine behind it, not a
    filter to be worked around: the DB data source holds candles and no
    historical chains, so a backtested option P&L could only be Black-Scholes
    off a generated chain. The same refusal is enforced where the run is
    executed (:mod:`backtest.api.backtest`), so a hand-crafted request cannot
    get one through either. ``?venue=forward`` (or no venue) returns everything
    — options belong there, priced from the broker.
    """
    venue = str(request.args.get("venue", "")).strip().lower()
    backtest_venue = venue in {"backtest", "compare"}
    catalogue = [
        {
            "name": s["name"],
            "description": s["description"],
            "version": s["version"],
            "author": s["author"],
            "params": s["params"],
            "signal_kind": s["signal_kind"],
            "eligible_instruments": s.get("eligible_instruments"),
            "default_segment": s.get("default_segment"),
            "readable_criteria": s.get("readable_criteria"),
        }
        for s in get_all()
        if not (backtest_venue and s["signal_kind"] == "option")
    ]
    log.debug(
        "/api/strategies → %d entries%s",
        len(catalogue),
        f" (venue={venue}, options excluded)" if backtest_venue else "",
    )
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
