"""Playbook API — CRUD for option playbooks + spawn from playbook.

Playbook = declarative config (structure, strikes policy, exits, sizing).
Portfolio spawns Runners *from* Playbooks. One concept, embedded, no new page.

Endpoints:
* GET  /api/playbooks              — list playbooks (filter by tag/underlying)
* POST /api/playbooks              — create playbook
* GET  /api/playbooks/<id>         — get one playbook
* PUT  /api/playbooks/<id>         — update playbook
* DELETE /api/playbooks/<id>       — delete playbook
* POST /api/playbooks/<id>/spawn   — spawn runner from playbook
* GET  /api/playbooks/chains/<underlying> — chain snapshot (data ownership: engine provides)
"""

from __future__ import annotations

from decimal import Decimal
from typing import Tuple

from flask import Blueprint, Response, jsonify, request

from backtest.logging_config import get_logger
from backtest.options.playbook import get_playbook_registry

log = get_logger(__name__)

playbooks_bp = Blueprint("playbooks_api", __name__)


def _error(message: str, status: int = 400) -> Tuple[Response, int]:
    log.warning("playbooks rejected (%d): %s", status, message)
    return jsonify({"success": False, "error": message}), status


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
        from backtest.options.playbook import Playbook
        pb = Playbook.from_dict(data)
        registry = get_playbook_registry()
        registry.save(pb)
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
    registry = get_playbook_registry()
    existing = registry.get(playbook_id)
    if existing is None:
        return _error(f"unknown playbook: {playbook_id}", 404)

    data = request.get_json(silent=True) or {}
    # Merge with existing
    merged = existing.to_dict()
    merged.update(data)
    merged["playbook_id"] = playbook_id  # don't allow id change

    try:
        from backtest.options.playbook import Playbook
        pb = Playbook.from_dict(merged)
        registry.save(pb)
    except ValueError as exc:
        return _error(str(exc))
    except Exception as exc:
        log.exception("Failed to update playbook")
        return _error(f"failed to update: {exc}", 500)

    return jsonify({"success": True, "playbook": pb.to_dict()}), 200


@playbooks_bp.delete("/api/playbooks/<playbook_id>")
def delete_playbook(playbook_id: str) -> Tuple[Response, int]:
    registry = get_playbook_registry()
    try:
        deleted = registry.delete(playbook_id)
    except ValueError as exc:
        return _error(str(exc), 400)

    if not deleted:
        return _error(f"unknown playbook: {playbook_id}", 404)

    return jsonify({"success": True, "deleted": playbook_id}), 200


@playbooks_bp.post("/api/playbooks/<playbook_id>/spawn")
def spawn_from_playbook(playbook_id: str) -> Tuple[Response, int]:
    """Spawn a runner from a playbook.

    Body: {
        "strategy": "directional_options",
        "allocated_capital": 100000,
        "timeframe": "1hour",
        "mode": "paper",
        "source": "synthetic",
        "name": "optional override",
        "params": {"ema_period": 9}
    }
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

    # Build runner config from playbook
    runner_config_dict = pb.to_runner_config(
        strategy_name=strategy_name,
        allocated_capital=capital,
        timeframe=str(data.get("timeframe", "1hour")),
        mode=data.get("mode") or "paper",
        source=data.get("source") or "synthetic",
        name=data.get("name"),
    )
    # Merge strategy params
    if data.get("params"):
        runner_config_dict["params"] = data["params"]
    if data.get("strategy_params"):
        runner_config_dict["params"] = data["strategy_params"]

    # Now spawn via portfolio manager (same path as portfolio API)
    try:
        from backtest.forward.paper_runner import RunnerConfig
        from backtest.forward.portfolio_manager import get_portfolio_manager

        config = RunnerConfig(
            name=runner_config_dict["name"],
            strategy_name=runner_config_dict["strategy"],
            allocated_capital=runner_config_dict["allocated_capital"],
            target_type=runner_config_dict["target_type"],
            symbols=runner_config_dict["symbols"],
            timeframe=runner_config_dict["timeframe"],
            strategy_params=runner_config_dict.get("params") or {},
            mode=runner_config_dict["mode"],
            source=runner_config_dict["source"],
            instrument=runner_config_dict["instrument"],
        )
        manager = get_portfolio_manager()
        instance_id = manager.add_runner(config, start=data.get("auto_start", True))
        runner = manager.get_runner(instance_id)
    except (ValueError, KeyError, TypeError) as exc:
        return _error(f"invalid runner config: {exc}")
    except Exception as exc:
        log.exception("Failed to spawn from playbook %s", playbook_id)
        return _error(f"spawn failed: {exc}", 500)

    return jsonify({
        "success": True,
        "instance_id": instance_id,
        "playbook_id": playbook_id,
        "runner": runner.get_state(),
    }), 201


@playbooks_bp.get("/api/playbooks/chains/<underlying>")
def get_chain_snapshot(underlying: str) -> Tuple[Response, int]:
    """Chain snapshot — data ownership: engine provides chain to strategies.

    This is the service that keeps strategies from touching broker APIs directly.
    Returns available strikes + chain for an underlying/expiry.

    Query params:
    ?expiry=2026-09-24 (optional, defaults to nearest)
    ?option_type=CE|PE (optional, defaults to both)
    """
    underlying = underlying.upper()
    expiry_str = request.args.get("expiry")
    option_type = request.args.get("option_type", "CE").upper()

    try:
        # Use the same quote provider logic as options_api
        from backtest.web.options_api import get_quote_provider
        quotes = get_quote_provider()

        generator = getattr(quotes, "generator", None) or getattr(
            getattr(quotes, "inner", None), "generator", None
        )
        if generator is None:
            from backtest.options.quote_providers import SyntheticChainGenerator
            generator = SyntheticChainGenerator()

        # Parse expiry
        expiry = None
        if expiry_str:
            try:
                from datetime import date
                expiry = date.fromisoformat(expiry_str)
            except ValueError:
                return _error(f"invalid expiry format, expected YYYY-MM-DD, got {expiry_str!r}")

        if expiry is None:
            from backtest.options.expiry_policy import NearestExpiryPolicy
            expiry = NearestExpiryPolicy().select_expiry(
                generator.available_expiries(underlying)
            )
            if expiry is None:
                return _error(f"no available expiry for {underlying}", 404)

        # Generate chain
        chain = generator.generate_chain(underlying, expiry=expiry, option_type=option_type)

        # Spot
        spot = generator.get_spot(underlying) if hasattr(generator, "get_spot") else 0

        # Build response — strikes with LTP
        strikes_data = []
        for strike, contract in sorted(chain.items()):
            try:
                # Try to get quote
                quote = quotes.get_quote(contract.instrument_token) if hasattr(quotes, "get_quote") else {"ltp": 0}
                ltp = quote.get("ltp", 0) if isinstance(quote, dict) else 0
            except Exception:
                ltp = 0

            strikes_data.append({
                "strike": float(strike),
                "ltp": float(ltp),
                "trading_symbol": getattr(contract, "trading_symbol", ""),
                "instrument_token": getattr(contract, "instrument_token", ""),
            })

        return jsonify({
            "success": True,
            "underlying": underlying,
            "expiry": str(expiry),
            "spot": float(spot),
            "option_type": option_type,
            "strikes": strikes_data,
            "count": len(strikes_data),
            "quote_source": getattr(quotes, "source_name", "unknown"),
        }), 200

    except Exception as exc:
        log.exception("Chain snapshot failed for %s", underlying)
        return _error(f"chain snapshot failed: {exc}", 500)


@playbooks_bp.get("/api/playbooks/risk/envelope")
def risk_envelope_preview() -> Tuple[Response, int]:
    """Preview risk envelope for a playbook — instrument-agnostic.

    Query: ?playbook_id=xxx&spot=24800&lot_size=50
    Or body: {playbook dict + spot}
    """
    playbook_id = request.args.get("playbook_id")
    spot_str = request.args.get("spot", "24800")
    lot_size_str = request.args.get("lot_size", "50")

    try:
        spot = float(spot_str)
        lot_size = int(lot_size_str)
    except (TypeError, ValueError):
        return _error("spot must be number, lot_size must be int")

    if playbook_id:
        registry = get_playbook_registry()
        pb = registry.get(playbook_id)
        if pb is None:
            return _error(f"unknown playbook: {playbook_id}", 404)
        envelope = pb.risk_envelope(spot_price=spot, lot_size=lot_size)
    else:
        # Ad-hoc: need playbook in body
        data = request.get_json(silent=True) or {}
        if not data:
            return _error("provide playbook_id or playbook JSON body")
        try:
            from backtest.options.playbook import Playbook
            pb = Playbook.from_dict(data)
            envelope = pb.risk_envelope(spot_price=spot, lot_size=lot_size)
        except Exception as exc:
            return _error(f"invalid playbook: {exc}")

    return jsonify({"success": True, "envelope": envelope}), 200
