"""Portfolio Command Center API (PRD Phase 5 / Tasks 5.1 & 5.2).

REST + Server-Sent Events surface for the multi-strategy
:class:`~backtest.forward.portfolio_manager.PortfolioManager`:

* ``GET  /api/portfolio/summary``             — aggregate stats + instance rows
* ``GET  /api/portfolio/universes``           — symbol universe catalogue
* ``POST /api/portfolio/runner/create``       — spawn a runner (single or pool)
* ``POST /api/portfolio/runner/<id>/control`` — pause/resume/stop/flatten/deep_dive
* ``GET  /api/portfolio/runner/<id>``          — deep-dive detail payload
* ``POST /api/portfolio/control/<action>``    — pause_all / resume_all / stop_all /
                                                emergency_flatten / reset_breaker
* ``POST /api/portfolio/emergency_stop``       — global emergency halt/flatten
* ``POST /api/portfolio/test/breach``         — demo/test crash injection
* ``GET  /api/portfolio/stream``               — SSE, JSON snapshot every 1 s
"""

from __future__ import annotations

import json
import time
from typing import Tuple

from flask import Blueprint, Response, current_app, jsonify, request, stream_with_context

from backtest.data.universe import is_universe, list_universes
from backtest.forward.paper_runner import TARGET_POOL, TARGET_SINGLE, RunnerConfig
from backtest.forward.portfolio_manager import get_portfolio_manager
from backtest.logging_config import get_logger

portfolio_bp = Blueprint("portfolio_api", __name__)
log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _manager():
    return get_portfolio_manager()


#: Bucket modes accepted by :func:`list_instances` / ``?mode=`` (ticket P4.1).
VALID_INSTANCE_MODES = ("paper", "live")


def list_instances(mode: str | None = None) -> list[dict]:
    """Command-center instance rows, optionally filtered by bucket mode.

    ``mode`` is ``None`` (all buckets) or one of :data:`VALID_INSTANCE_MODES`;
    anything else raises :class:`ValueError` (the HTTP layer maps it to 400).
    """
    return _manager().list_instances(mode)


def _error(message: str, status: int = 400) -> Tuple[Response, int]:
    # Every rejected portfolio request now leaves a trace (these were silent).
    log.warning("rejected (%d): %s", status, message)
    return jsonify({"success": False, "error": message}), status


def _parse_target(data: dict) -> Tuple[str, list, str | None]:
    """Resolve (target_type, symbols, universe_id) from spawn payload."""
    target_type = str(data.get("target_type", "")).upper()
    target = data.get("target") or data.get("symbol") or data.get("universe") or ""
    symbols = data.get("symbols") or []
    universe_id = data.get("universe_id")

    # Explicit universe id, or a target that names a registered universe.
    if universe_id or is_universe(str(target)):
        uid = universe_id or str(target)
        if not is_universe(uid):
            raise ValueError(f"unknown universe: {uid}")
        from backtest.data.universe import get_universe_symbols

        return TARGET_POOL, get_universe_symbols(uid), uid.upper()

    if target_type == TARGET_POOL:
        # Ad-hoc basket supplied as symbols
        if not symbols:
            raise ValueError("pool runner needs a universe_id or symbols")
        return TARGET_POOL, [str(s).upper() for s in symbols], None

    # Single symbol
    sym = symbols[0] if symbols else target
    if not sym:
        raise ValueError("single runner needs a symbol")
    return TARGET_SINGLE, [str(sym).upper()], None


# ---------------------------------------------------------------------------
# Summary / meta
# ---------------------------------------------------------------------------


@portfolio_bp.get("/api/portfolio/summary")
def summary() -> Tuple[Response, int]:
    """Combined stats + instance rows; ``?mode=paper|live`` scopes a bucket."""
    mode = request.args.get("mode") or None
    try:
        return jsonify({"success": True, "portfolio": _manager().get_portfolio_summary(mode)}), 200
    except ValueError as exc:
        return _error(str(exc))


@portfolio_bp.get("/api/portfolio/universes")
def universes() -> Tuple[Response, int]:
    return jsonify({"success": True, "universes": list_universes()}), 200


@portfolio_bp.get("/api/portfolio/runner/<instance_id>")
def runner_detail(instance_id: str) -> Tuple[Response, int]:
    try:
        detail = _manager().get_runner_detail(instance_id)
    except KeyError:
        return _error(f"unknown runner: {instance_id}", 404)
    return jsonify({"success": True, "runner": detail}), 200


# ---------------------------------------------------------------------------
# Spawn
# ---------------------------------------------------------------------------


@portfolio_bp.post("/api/portfolio/runner/create")
def create_runner() -> Tuple[Response, int]:
    data = request.get_json(silent=True) or {}

    name = str(data.get("name", "")).strip()
    strategy_name = str(data.get("strategy", data.get("strategy_name", ""))).strip()
    if not strategy_name:
        return _error("strategy is required")
    try:
        capital = float(data.get("allocated_capital", data.get("capital", 100_000)))
    except (TypeError, ValueError):
        return _error("allocated_capital must be a number")
    if capital <= 0:
        return _error("allocated_capital must be positive")

    try:
        target_type, symbols, universe_id = _parse_target(data)
    except ValueError as exc:
        return _error(str(exc))

    if not name:
        kind = universe_id or symbols[0]
        name = f"{strategy_name} · {kind}"

    params = data.get("params") or {}
    try:
        config = RunnerConfig(
            name=name,
            strategy_name=strategy_name,
            allocated_capital=capital,
            target_type=target_type,
            symbols=symbols,
            universe_id=universe_id,
            timeframe=str(data.get("timeframe", "1hour")),
            strategy_params=params,
            max_pool_positions=int(data.get("max_pool_positions", 5)),
            position_pct=(
                float(data["position_pct"]) if data.get("position_pct") is not None else None
            ),
            mode=data.get("mode") or "paper",
            source=data.get("source") or "synthetic",
        )
        auto_start = bool(data.get("auto_start", True))
        instance_id = _manager().add_runner(config, start=auto_start)
    except (ValueError, KeyError, TypeError) as exc:
        return _error(f"invalid runner config: {exc}")

    runner = _manager().get_runner(instance_id)
    return (
        jsonify(
            {
                "success": True,
                "instance_id": instance_id,
                "runner": runner.get_state(),
            }
        ),
        201,
    )


# ---------------------------------------------------------------------------
# Instance control
# ---------------------------------------------------------------------------


@portfolio_bp.post("/api/portfolio/runner/<instance_id>/control")
def control_runner(instance_id: str) -> Tuple[Response, int]:
    data = request.get_json(silent=True) or {}
    action = str(data.get("action", "")).strip().lower()

    if action in ("", "deep_dive", "detail", "dive"):
        try:
            detail = _manager().get_runner_detail(instance_id)
        except KeyError:
            return _error(f"unknown runner: {instance_id}", 404)
        return jsonify({"success": True, "action": "deep_dive", "runner": detail}), 200

    if action not in ("pause", "resume", "stop", "flatten", "start"):
        return _error(f"unknown action: {action}")

    try:
        state = _manager().control_runner(instance_id, action)
    except KeyError:
        return _error(f"unknown runner: {instance_id}", 404)
    except (ValueError, RuntimeError) as exc:
        return _error(str(exc), 409)
    log.info("runner %s: action=%s → %s", instance_id, action, state.get("status", "?"))
    return jsonify({"success": True, "action": action, "runner": state}), 200


@portfolio_bp.delete("/api/portfolio/runner/<instance_id>")
def remove_runner(instance_id: str) -> Tuple[Response, int]:
    if not _manager().remove_runner(instance_id):
        return _error(f"unknown runner: {instance_id}", 404)
    log.info("runner %s removed", instance_id)
    return jsonify({"success": True, "removed": instance_id}), 200


# ---------------------------------------------------------------------------
# Bulk / global control
# ---------------------------------------------------------------------------


@portfolio_bp.post("/api/portfolio/control/<action>")
def bulk_control(action: str) -> Tuple[Response, int]:
    manager = _manager()
    action = action.lower()
    try:
        if action == "pause_all":
            n = manager.pause_all()
        elif action == "resume_all":
            n = manager.resume_all()
        elif action == "stop_all":
            n = manager.stop_all()
        elif action == "emergency_flatten":
            n = manager.emergency_flatten_all(reason="manual_emergency")
        elif action == "reset_breaker":
            manager.reset_circuit_breaker()
            n = 0
        else:
            return _error(f"unknown bulk action: {action}")
    except RuntimeError as exc:
        return _error(str(exc), 409)
    log.info("bulk action %s affected %d runner(s)", action, n)
    return (
        jsonify(
            {
                "success": True,
                "action": action,
                "affected": n,
                "portfolio": manager.get_portfolio_summary(),
            }
        ),
        200,
    )


@portfolio_bp.post("/api/portfolio/emergency_stop")
def emergency_stop() -> Tuple[Response, int]:
    data = request.get_json(silent=True) or {}
    reason = str(data.get("reason", "manual_emergency"))
    count = _manager().emergency_flatten_all(reason=reason)
    return (
        jsonify(
            {
                "success": True,
                "flattened_positions": count,
                "portfolio": _manager().get_portfolio_summary(),
            }
        ),
        200,
    )


@portfolio_bp.post("/api/portfolio/test/breach")
def test_breach() -> Tuple[Response, int]:
    """Demo/verification: inject a simulated crash to trip circuit breakers.

    Re-baselines daily PnL and (by default) tightens the limits so the breach
    fires deterministically — this powers PRD acceptance step 5.
    """
    data = request.get_json(silent=True) or {}
    try:
        crash_pct = float(data.get("crash_pct", 0.25))
    except (TypeError, ValueError):
        return _error("crash_pct must be a number")

    tighten = data.get("tighten_limits", True)
    summary = _manager().stress_test(
        crash_pct=crash_pct,
        daily_loss_limit=1_000.0 if tighten else None,
        max_drawdown_pct=0.05 if tighten else None,
    )
    return (
        jsonify(
            {
                "success": True,
                "portfolio": summary,
            }
        ),
        200,
    )


# ---------------------------------------------------------------------------
# SSE stream (Task 5.2) — zero-polling live updates
# ---------------------------------------------------------------------------


@portfolio_bp.get("/api/portfolio/stream")
def stream() -> Response:
    """Server-Sent Events: broadcast a JSON portfolio snapshot every second."""
    interval = current_app.config.get("PORTFOLIO_SSE_INTERVAL", 1.0)

    @stream_with_context
    def event_stream():
        # SSE hello — lets the client confirm the channel immediately.
        log.info("SSE stream opened (cadence %.1fs, client=%s)", interval, request.remote_addr)
        errors = 0
        yield ": connected to /api/portfolio/stream\n\n"
        while True:
            try:
                payload = _manager().get_portfolio_summary()
                errors = 0
                yield f"event: portfolio\ndata: {json.dumps(payload, default=str)}\n\n"
            except GeneratorExit:
                raise
            except Exception as exc:  # noqa: BLE001 — keep the stream alive
                errors += 1
                # Log once, then at most every 10th repeat, so a broken manager
                # cannot flood the log at 1 Hz.
                if errors == 1 or errors % 10 == 0:
                    log.warning(
                        "SSE snapshot failed (%d in a row): %s: %s",
                        errors,
                        exc.__class__.__name__,
                        exc,
                    )
                yield f"event: error\ndata: {json.dumps({'error': str(exc)})}\n\n"
            time.sleep(interval)

    return Response(
        event_stream(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
