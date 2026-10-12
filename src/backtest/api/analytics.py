"""Analytics REST API endpoints (Live/Paper Performance & Strategy Analysis).

Two families live on this one blueprint:

* **Per-strategy** — ``/overview`` and ``/strategy/<id>`` (unchanged).
* **Cross-broker** (PRD-003) — ``/api/analytics/cross-broker/*``: the broker
  dimension the per-strategy view structurally cannot express (which venue
  filled best, whether the difference is real, what a migration would cost).
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from flask import Blueprint, Response, jsonify, request

from backtest.analytics.cross_broker import CrossBrokerAnalyticsService
from backtest.analytics.portfolio import AnalyticsService
from backtest.logging_config import get_logger

analytics_bp = Blueprint("analytics_api", __name__)
log = get_logger(__name__)


def _error(msg: str, status: int = 400) -> Tuple[Response, int]:
    return jsonify({"success": False, "error": msg}), status


def _mode_arg() -> Optional[str]:
    """``?mode=`` is optional everywhere on this blueprint; ``all`` means None."""
    mode = request.args.get("mode") or None
    return None if mode in ("all", "") else mode


def _json_body() -> Dict[str, Any]:
    return request.get_json(silent=True) or {}


@analytics_bp.get("/api/analytics/overview")
def analytics_overview() -> Tuple[Response, int]:
    """Get portfolio-wide aggregated analytics and strategy health cards."""
    period = request.args.get("period", "30d")
    mode = _mode_arg()

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


# ----------------------------------------------------------------------
# Cross-broker analytics (PRD-003) — the broker dimension /analytics lacked
# ----------------------------------------------------------------------


@analytics_bp.get("/api/analytics/cross-broker/summary")
def cross_broker_summary() -> Tuple[Response, int]:
    """Endpoint 1 — portfolio totals, per-broker and per-segment rollup, rankings."""
    period = request.args.get("period", "30d")
    mode = _mode_arg()
    try:
        data = CrossBrokerAnalyticsService().get_summary(period=period, mode=mode)
        return jsonify({"success": True, **data}), 200
    except Exception as exc:  # noqa: BLE001
        log.exception("cross_broker_summary failed: %s", exc)
        return _error(f"Failed to load cross-broker summary: {exc}", 500)


@analytics_bp.get("/api/analytics/cross-broker/execution")
def cross_broker_execution() -> Tuple[Response, int]:
    """Endpoint 2 — execution quality detail for one broker (or all of them)."""
    period = request.args.get("period", "30d")
    broker = request.args.get("broker") or None
    strategy = request.args.get("strategy") or None
    mode = _mode_arg()
    try:
        data = CrossBrokerAnalyticsService().get_execution_quality(
            broker=broker, period=period, strategy=strategy, mode=mode
        )
        return jsonify({"success": True, **data}), 200
    except Exception as exc:  # noqa: BLE001
        log.exception("cross_broker_execution failed: %s", exc)
        return _error(f"Failed to load execution quality: {exc}", 500)


@analytics_bp.post("/api/analytics/cross-broker/compare")
def cross_broker_compare() -> Tuple[Response, int]:
    """Endpoint 3 — side-by-side metric comparison with significance tests.

    Body::

        {"brokers": ["mstock", "dhan"], "metrics": ["sharpe", "avg_slippage_bps"],
         "period": "30d", "statistical_test": true}
    """
    data = _json_body()
    brokers = data.get("brokers") or []
    if not isinstance(brokers, list) or len(brokers) < 2:
        return _error("brokers must be a list of at least two broker names")
    try:
        result = CrossBrokerAnalyticsService().compare_brokers(
            brokers=brokers,
            metrics=data.get("metrics"),
            period=str(data.get("period") or "30d"),
            statistical_test=bool(data.get("statistical_test", True)),
            mode=data.get("mode") or None,
        )
    except ValueError as exc:
        return _error(str(exc), 400)
    except Exception as exc:  # noqa: BLE001
        log.exception("cross_broker_compare failed: %s", exc)
        return _error(f"Broker comparison failed: {exc}", 500)
    return jsonify({"success": True, **result}), 200


@analytics_bp.post("/api/analytics/cross-broker/migration-impact")
def cross_broker_migration_impact() -> Tuple[Response, int]:
    """Endpoint 4 — what-if: what happens if this strategy moves broker?

    Body::

        {"strategy": "ema_crossover", "from_broker": "mstock",
         "to_broker": "dhan", "period": "30d"}
    """
    data = _json_body()
    try:
        result = CrossBrokerAnalyticsService().migration_impact(
            strategy=str(data.get("strategy") or ""),
            from_broker=str(data.get("from_broker") or ""),
            to_broker=str(data.get("to_broker") or ""),
            period=str(data.get("period") or "30d"),
            mode=data.get("mode") or None,
        )
    except ValueError as exc:
        return _error(str(exc), 400)
    except LookupError as exc:
        return _error(str(exc), 404)
    except Exception as exc:  # noqa: BLE001
        log.exception("cross_broker_migration_impact failed: %s", exc)
        return _error(f"Migration impact failed: {exc}", 500)
    return jsonify({"success": True, **result}), 200


@analytics_bp.post("/api/analytics/cross-broker/recommend-broker")
def cross_broker_recommend() -> Tuple[Response, int]:
    """Endpoint 5 — which broker should a NEW strategy run on?

    Body::

        {"strategy_type": "scalper", "segment": "equity",
         "avg_trade_size": 50000, "trade_frequency": "high", "period": "30d"}
    """
    data = _json_body()
    try:
        size = data.get("avg_trade_size")
        result = CrossBrokerAnalyticsService().recommend_broker(
            strategy_type=str(data.get("strategy_type") or "scalper"),
            segment=data.get("segment") or None,
            avg_trade_size=float(size) if size is not None else None,
            trade_frequency=str(data.get("trade_frequency") or "high"),
            period=str(data.get("period") or "30d"),
            mode=data.get("mode") or None,
        )
    except (TypeError, ValueError) as exc:
        return _error(str(exc), 400)
    except Exception as exc:  # noqa: BLE001
        log.exception("cross_broker_recommend failed: %s", exc)
        return _error(f"Broker recommendation failed: {exc}", 500)
    return jsonify({"success": True, **result}), 200
