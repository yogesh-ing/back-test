"""Analytics REST API endpoints (Live/Paper Performance & Strategy Analysis)."""

from __future__ import annotations

from typing import Tuple

from flask import Blueprint, Response, jsonify, request

from backtest.api.analytics_service import AnalyticsService
from backtest.logging_config import get_logger

analytics_bp = Blueprint("analytics_api", __name__)
log = get_logger(__name__)


def _error(msg: str, status: int = 400) -> Tuple[Response, int]:
    return jsonify({"success": False, "error": msg}), status


@analytics_bp.get("/api/analytics/overview")
def analytics_overview() -> Tuple[Response, int]:
    """Get portfolio-wide aggregated analytics and strategy health cards."""
    period = request.args.get("period", "30d")
    mode = request.args.get("mode") or None
    if mode in ("all", ""):
        mode = None

    try:
        service = AnalyticsService()
        data = service.get_portfolio_overview(period=period, mode=mode)
        return jsonify({"success": True, **data}), 200
    except Exception as exc:  # noqa: BLE001
        log.exception("analytics_overview failed: %s", exc)
        return _error(f"Failed to load analytics overview: {exc}", 500)


@analytics_bp.get("/api/analytics/strategy/<instance_id>")
def strategy_analytics_detail(instance_id: str) -> Tuple[Response, int]:
    """Get in-depth analytics, ratios, curves, and breakdowns for a specific runner."""
    period = request.args.get("period", "90d")

    try:
        service = AnalyticsService()
        detail = service.get_strategy_detail(instance_id, period=period)
        if not detail:
            return _error(f"Strategy runner {instance_id!r} not found", 404)
        return jsonify({"success": True, **detail}), 200
    except Exception as exc:  # noqa: BLE001
        log.exception("strategy_analytics_detail failed: %s", exc)
        return _error(f"Failed to load strategy detail: {exc}", 500)
