"""PnL Diary REST API blueprint (``/api/diary/*``).

Endpoints:
* ``GET /api/diary/calendar``                  — Slice 1 IST session calendar heatmap
* ``GET /api/diary/summary``                   — Slice 1 Executive day/range KPI strip
* ``GET /api/diary/attribution``               — Slice 2 Strategy → Instance → Trade tree
* ``GET /api/diary/instance/<key>/trades``     — Slice 2 Full Level-3 drilldown ledger

Paper and Live books are strictly segregated by ``?mode=paper|live``.
"""

from __future__ import annotations

from typing import Tuple

from flask import Blueprint, Response, jsonify, request

from backtest.diary.service import DiaryService
from backtest.logging_config import get_logger

diary_bp = Blueprint("diary_api", __name__)
log = get_logger(__name__)


def _error(message: str, status: int = 400) -> Tuple[Response, int]:
    return jsonify({"success": False, "error": message}), status


@diary_bp.get("/api/diary/calendar")
def diary_calendar() -> Tuple[Response, int]:
    mode = request.args.get("mode", "paper")
    from_arg = request.args.get("from") or None
    to_arg = request.args.get("to") or None
    strategy = request.args.get("strategy") or None
    try:
        payload = DiaryService().get_calendar(
            mode=mode,
            from_date=from_arg,
            to_date=to_arg,
            strategy=strategy,
        )
        return jsonify({"success": True, **payload}), 200
    except ValueError as exc:
        return _error(str(exc), 400)
    except Exception as exc:  # noqa: BLE001
        log.exception("diary_calendar failed: %s", exc)
        return _error(f"Failed to load diary calendar: {exc}", 500)


@diary_bp.get("/api/diary/summary")
def diary_summary() -> Tuple[Response, int]:
    mode = request.args.get("mode", "paper")
    from_arg = request.args.get("from") or None
    to_arg = request.args.get("to") or None
    strategy = request.args.get("strategy") or None
    try:
        payload = DiaryService().get_summary(
            mode=mode,
            from_date=from_arg,
            to_date=to_arg,
            strategy=strategy,
        )
        return jsonify({"success": True, **payload}), 200
    except ValueError as exc:
        return _error(str(exc), 400)
    except Exception as exc:  # noqa: BLE001
        log.exception("diary_summary failed: %s", exc)
        return _error(f"Failed to load diary summary: {exc}", 500)


@diary_bp.get("/api/diary/attribution")
def diary_attribution() -> Tuple[Response, int]:
    mode = request.args.get("mode", "paper")
    from_arg = request.args.get("from") or None
    to_arg = request.args.get("to") or None
    strategy = request.args.get("strategy") or None
    try:
        payload = DiaryService().get_attribution(
            mode=mode,
            from_date=from_arg,
            to_date=to_arg,
            strategy=strategy,
        )
        return jsonify({"success": True, **payload}), 200
    except ValueError as exc:
        return _error(str(exc), 400)
    except Exception as exc:  # noqa: BLE001
        log.exception("diary_attribution failed: %s", exc)
        return _error(f"Failed to load diary attribution: {exc}", 500)


@diary_bp.get("/api/diary/instance/<path:instance_key>/trades")
def diary_instance_trades(instance_key: str) -> Tuple[Response, int]:
    mode = request.args.get("mode", "paper")
    from_arg = request.args.get("from") or None
    to_arg = request.args.get("to") or None
    try:
        payload = DiaryService().get_instance_trades(
            instance_key=instance_key,
            mode=mode,
            from_date=from_arg,
            to_date=to_arg,
        )
        return jsonify({"success": True, **payload}), 200
    except ValueError as exc:
        return _error(str(exc), 400)
    except KeyError as exc:
        return _error(str(exc), 404)
    except Exception as exc:  # noqa: BLE001
        log.exception("diary_instance_trades failed: %s", exc)
        return _error(f"Failed to load instance trades: {exc}", 500)


@diary_bp.get("/api/diary/edge/regimes")
def diary_edge_regimes() -> Tuple[Response, int]:
    mode = request.args.get("mode", "paper")
    from_arg = request.args.get("from") or None
    to_arg = request.args.get("to") or None
    strategy = request.args.get("strategy") or None
    clock = request.args.get("clock", "entry")
    group_by = request.args.get("group_by", "strategy")
    try:
        payload = DiaryService().get_edge_regimes(
            mode=mode,
            from_date=from_arg,
            to_date=to_arg,
            strategy=strategy,
            clock=clock,
            group_by=group_by,
        )
        return jsonify({"success": True, **payload}), 200
    except ValueError as exc:
        return _error(str(exc), 400)
    except Exception as exc:  # noqa: BLE001
        log.exception("diary_edge_regimes failed: %s", exc)
        return _error(f"Failed to load edge regimes: {exc}", 500)


@diary_bp.get("/api/diary/edge/payoff")
def diary_edge_payoff() -> Tuple[Response, int]:
    mode = request.args.get("mode", "paper")
    from_arg = request.args.get("from") or None
    to_arg = request.args.get("to") or None
    strategy = request.args.get("strategy") or None
    group_by = request.args.get("group_by", "instance")
    try:
        payload = DiaryService().get_edge_payoff(
            mode=mode,
            from_date=from_arg,
            to_date=to_arg,
            strategy=strategy,
            group_by=group_by,
        )
        return jsonify({"success": True, **payload}), 200
    except ValueError as exc:
        return _error(str(exc), 400)
    except Exception as exc:  # noqa: BLE001
        log.exception("diary_edge_payoff failed: %s", exc)
        return _error(f"Failed to load edge payoff diagnostics: {exc}", 500)
