"""PnL Diary REST API blueprint (``/api/diary/*``).

Slice 1 endpoints:
* ``GET /api/diary/calendar`` — IST session calendar heatmap + weekly strip
* ``GET /api/diary/summary``  — Executive day/range KPI strip + ledger health

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
