"""Playbook API — 6 routes verbatim per ARCHITECTURE-UNIFIED-TRADING.md §2 / U1.2

Endpoints (final spec):
GET    /api/playbooks?tag=&underlying=     list
GET    /api/playbooks/<id>                 single
POST   /api/playbooks                      create
PUT    /api/playbooks/<id>                 update (bumps version)
DELETE /api/playbooks/<id>                 delete (blocks pb_default_*)
POST   /api/playbooks/<id>/spawn           returns runner config; caller creates (no side effects)

Spawn returning config without side effects is ratified: one creation path
(POST /api/portfolio/runner/create), one audit trail.

Audit: every mutation logs scope=playbook (AC-16).
"""

from __future__ import annotations

from typing import Tuple

from flask import Blueprint, Response, jsonify, request

from backtest.logging_config import get_logger
from backtest.playbooks.models import Playbook
from backtest.playbooks.registry import get_playbook_registry

log = get_logger(__name__)

playbooks_bp = Blueprint("playbooks_api", __name__)


def _error(message: str, status: int = 400) -> Tuple[Response, int]:
    log.warning("playbooks rejected (%d): %s", status, message)
    return jsonify({"success": False, "error": message}), status


def _audit_log(action: str, playbook_id: str, scope: str = "playbook") -> None:
    """AC-16: audit log line per mutation with scope."""
    try:
        # Use forward audit logger if available, else just log
        from backtest.forward.portfolio_manager import get_portfolio_manager

        manager = get_portfolio_manager()
        # If manager has audit, use it; else fallback to logger
        if hasattr(manager, "_audit_log"):
            manager._audit_log(f"{action} playbook {playbook_id}", scope=scope)
        else:
            log.info("[AUDIT] scope=%s action=%s playbook_id=%s", scope, action, playbook_id)
    except Exception:
        log.info("[AUDIT] scope=%s action=%s playbook_id=%s", scope, action, playbook_id)


@playbooks_bp.get("/api/playbooks")
def list_playbooks() -> Tuple[Response, int]:
    """List playbooks, optional filters ?tag= & ?underlying="""
    registry = get_playbook_registry()
    tag = request.args.get("tag")
    underlying = request.args.get("underlying")
    playbooks = registry.list(tag=tag, underlying=underlying)
    return jsonify({
        "success": True,
        "playbooks": [pb.to_dict() for pb in playbooks],
        "count": len(playbooks),
    }), 200


@playbooks_bp.post("/api/playbooks")
def create_playbook() -> Tuple[Response, int]:
    """Create a new playbook."""
    data = request.get_json(silent=True) or {}
    if not data.get("name"):
        return _error("name is required")

    try:
        pb = Playbook.from_dict(data)
        registry = get_playbook_registry()
        registry.save(pb)
        _audit_log("CREATE", pb.playbook_id, scope="playbook")
    except ValueError as exc:
        return _error(str(exc))
    except Exception as exc:
        log.exception("Failed to create playbook")
        return _error(f"failed to create: {exc}", 500)

    return jsonify({"success": True, "playbook": pb.to_dict()}), 201


@playbooks_bp.get("/api/playbooks/<playbook_id>")
def get_playbook(playbook_id: str) -> Tuple[Response, int]:
    registry = get_playbook_registry()
    pb = registry.get(playbook_id)
    if pb is None:
        return _error(f"unknown playbook: {playbook_id}", 404)
    return jsonify({"success": True, "playbook": pb.to_dict()}), 200


@playbooks_bp.put("/api/playbooks/<playbook_id>")
def update_playbook(playbook_id: str) -> Tuple[Response, int]:
    """Update — bumps version per architecture §2."""
    registry = get_playbook_registry()
    existing = registry.get(playbook_id)
    if existing is None:
        return _error(f"unknown playbook: {playbook_id}", 404)

    data = request.get_json(silent=True) or {}
    merged = existing.to_dict()
    merged.update(data)
    merged["playbook_id"] = playbook_id  # don't allow id change

    try:
        pb = Playbook.from_dict(merged)
        # Registry.save() auto-bumps version if existing
        registry.save(pb)
        _audit_log("UPDATE", playbook_id, scope="playbook")
    except ValueError as exc:
        return _error(str(exc))
    except Exception as exc:
        log.exception("Failed to update playbook")
        return _error(f"failed to update: {exc}", 500)

    return jsonify({"success": True, "playbook": pb.to_dict()}), 200


@playbooks_bp.delete("/api/playbooks/<playbook_id>")
def delete_playbook(playbook_id: str) -> Tuple[Response, int]:
    """Delete with pb_default_* guard."""
    registry = get_playbook_registry()
    try:
        deleted = registry.delete(playbook_id)
        if deleted:
            _audit_log("DELETE", playbook_id, scope="playbook")
    except ValueError as exc:
        return _error(str(exc), 400)

    if not deleted:
        return _error(f"unknown playbook: {playbook_id}", 404)

    return jsonify({"success": True, "deleted": playbook_id}), 200


@playbooks_bp.post("/api/playbooks/<playbook_id>/spawn")
def spawn_from_playbook(playbook_id: str) -> Tuple[Response, int]:
    """Spawn returns runner config; caller creates (no side effects).

    Body: {
        "strategy": "directional_options",
        "allocated_capital": 100000,
        "timeframe": "1hour",
        "mode": "paper",
        "source": "synthetic",
        "name": "optional override",
        "params": {"ema_period": 9}
    }

    Returns config dict that POST /api/portfolio/runner/create accepts.
    One creation path, one audit trail — ratified per architecture.
    """
    registry = get_playbook_registry()
    pb = registry.get(playbook_id)
    if pb is None:
        return _error(f"unknown playbook: {playbook_id}", 404)

    data = request.get_json(silent=True) or {}
    strategy_name = str(data.get("strategy", data.get("strategy_name", ""))).strip()
    if not strategy_name:
        return _error("strategy is required")

    try:
        capital = float(data.get("allocated_capital", data.get("capital", 100_000)))
    except (TypeError, ValueError):
        return _error("allocated_capital must be a number")
    if capital <= 0:
        return _error("allocated_capital must be positive")

    # Build runner config from playbook — no side effects, just config
    runner_config_dict = pb.to_runner_config(
        strategy_name=strategy_name,
        allocated_capital=capital,
        timeframe=str(data.get("timeframe", "1hour")),
        mode=data.get("mode") or "paper",
        source=data.get("source") or "synthetic",
        name=data.get("name"),
    )
    if data.get("params"):
        runner_config_dict["params"] = data["params"]
    if data.get("strategy_params"):
        runner_config_dict["params"] = data["strategy_params"]

    _audit_log("SPAWN", playbook_id, scope="playbook")

    return jsonify({
        "success": True,
        "playbook_id": playbook_id,
        "runner_config": runner_config_dict,
        # Backward compat: also return as "config" and "runner" for old UI that expects it
        "config": runner_config_dict,
    }), 200
