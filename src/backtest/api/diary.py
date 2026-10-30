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


@diary_bp.get("/api/diary/graduation")
def diary_graduation() -> Tuple[Response, int]:
    mode = request.args.get("mode", "paper")
    from_arg = request.args.get("from") or None
    to_arg = request.args.get("to") or None
    strategy = request.args.get("strategy") or None
    try:
        payload = DiaryService().get_graduation(
            mode=mode,
            from_date=from_arg,
            to_date=to_arg,
            strategy=strategy,
        )
        return jsonify({"success": True, **payload}), 200
    except ValueError as exc:
        return _error(str(exc), 400)
    except Exception as exc:  # noqa: BLE001
        log.exception("diary_graduation failed: %s", exc)
        return _error(f"Failed to load graduation scorecard: {exc}", 500)


@diary_bp.get("/api/diary/notes")
def diary_notes_list() -> Tuple[Response, int]:
    mode = request.args.get("mode", "paper")
    from_arg = request.args.get("from") or None
    to_arg = request.args.get("to") or None
    strategy = request.args.get("strategy") or None
    try:
        payload = DiaryService().list_notes(
            mode=mode,
            from_date=from_arg,
            to_date=to_arg,
            strategy=strategy,
        )
        return jsonify({"success": True, **payload}), 200
    except ValueError as exc:
        return _error(str(exc), 400)
    except Exception as exc:  # noqa: BLE001
        log.exception("diary_notes_list failed: %s", exc)
        return _error(f"Failed to list diary notes: {exc}", 500)


@diary_bp.post("/api/diary/notes")
def diary_notes_save() -> Tuple[Response, int]:
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return _error("Expected JSON object body", 400)
    mode = body.get("mode") if "mode" in body else request.args.get("mode", "paper")
    session_date = body.get("session_date")
    if not session_date:
        return _error("session_date (YYYY-MM-DD) is required", 400)
    try:
        saved = DiaryService().save_note(
            mode=mode,
            session_date=str(session_date),
            note_text=body.get("note_text", ""),
            regime_tags=body.get("regime_tags"),
            strategy_name=body.get("strategy_name"),
            portfolio_id=body.get("portfolio_id"),
            instance_label=body.get("instance_label"),
            note_id=body.get("note_id"),
        )
        status = 200 if body.get("note_id") else 201
        return jsonify({"success": True, "note": saved}), status
    except ValueError as exc:
        return _error(str(exc), 400)
    except KeyError as exc:
        return _error(str(exc), 404)
    except Exception as exc:  # noqa: BLE001
        log.exception("diary_notes_save failed: %s", exc)
        return _error(f"Failed to save diary note: {exc}", 500)


@diary_bp.delete("/api/diary/notes/<note_id>")
def diary_notes_delete(note_id: str) -> Tuple[Response, int]:
    body = request.get_json(silent=True)
    mode = (
        body.get("mode")
        if isinstance(body, dict) and "mode" in body
        else request.args.get("mode")
    )
    if not mode:
        return _error("mode ('paper' or 'live') query param is required", 400)
    try:
        deleted = DiaryService().delete_note(note_id, mode=mode)
        return jsonify({"success": True, **deleted}), 200
    except ValueError as exc:
        return _error(str(exc), 400)
    except KeyError as exc:
        return _error(str(exc), 404)
    except Exception as exc:  # noqa: BLE001
        log.exception("diary_notes_delete failed: %s", exc)
        return _error(f"Failed to delete diary note: {exc}", 500)
