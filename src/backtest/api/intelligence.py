"""Portfolio Intelligence & Alerts API.

Read-only analytics over the portfolio plus the alert lifecycle endpoints.
Nothing here opens, closes or resizes a position — the only writes are alert
bookkeeping (dismiss / review / resolve) and market *inputs* (a VIX reading,
an option-chain snapshot) that feed the analytics.

Endpoints
---------
* ``GET  /api/portfolio/greeks?mode=``          aggregate Greeks + breakdown + scenarios
* ``GET  /api/portfolio/concentration?mode=``   by underlying / by strike
* ``GET  /api/portfolio/correlation?mode=&group_by=runner|strategy&refresh=1``
* ``GET  /api/portfolio/intelligence?mode=``    everything above in one call (Risk Board)
* ``GET  /api/market/regime``                   VIX regime, history, strategy fit
* ``POST /api/market/vix``                      ``{"value": 18.4}`` explicit India VIX reading
* ``GET  /api/market/oi-activity?symbol=``      OI anomalies + spread dry-ups
* ``POST /api/market/chain-activity``           ``{"underlying", "rows": [...]}`` chain snapshot
* ``GET  /api/alerts/active``                   open alerts (``?include_dismissed=1``)
* ``GET  /api/alerts/history?from=&type=&severity=&limit=``
* ``GET  /api/alerts/<id>``                     detail + context + subscriptions
* ``GET  /api/alerts/subscriptions``            strategy → alert types
* ``POST /api/alerts/<id>/dismiss|review|resolve``
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Tuple

from flask import Blueprint, Response, jsonify, request

from backtest.alerts.types import AlertType, Severity
from backtest.forward.portfolio_manager import get_portfolio_manager
from backtest.logging_config import get_logger

intelligence_bp = Blueprint("intelligence_api", __name__)
log = get_logger(__name__)

VALID_MODES = ("paper", "live")
VALID_GROUP_BY = ("runner", "strategy")


def _intel():
    manager = get_portfolio_manager()
    intel = getattr(manager, "intelligence", None)
    if intel is None or not getattr(intel, "enabled", True):
        raise RuntimeError("portfolio intelligence is disabled")
    return intel


def _error(message: str, status: int = 400) -> Tuple[Response, int]:
    log.warning("rejected (%d): %s", status, message)
    return jsonify({"success": False, "error": message}), status


def _mode() -> Optional[str]:
    mode = (request.args.get("mode") or "").strip().lower() or None
    if mode in (None, "all", "combined"):
        return None
    if mode not in VALID_MODES:
        raise ValueError(f"mode must be one of {VALID_MODES + ('all',)}")
    return mode


def _flag(name: str) -> bool:
    return str(request.args.get(name, "")).strip().lower() in {"1", "true", "yes", "on"}


def _guard(fn):
    """Uniform error envelope: 400 on bad input, 503 when disabled."""

    def wrapper(*args: Any, **kwargs: Any):
        try:
            return fn(*args, **kwargs)
        except ValueError as exc:
            return _error(str(exc), 400)
        except RuntimeError as exc:
            return _error(str(exc), 503)

    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper


# ---------------------------------------------------------------------------
# Portfolio analytics
# ---------------------------------------------------------------------------


@intelligence_bp.get("/api/portfolio/greeks")
@_guard
def portfolio_greeks():
    greeks = dict(_intel().greeks(_mode()))
    return jsonify({"success": True, **greeks})


@intelligence_bp.get("/api/portfolio/concentration")
@_guard
def portfolio_concentration():
    return jsonify({"success": True, **_intel().concentration(_mode())})


@intelligence_bp.get("/api/portfolio/correlation")
@_guard
def portfolio_correlation():
    group_by = (request.args.get("group_by") or "runner").lower()
    if group_by not in VALID_GROUP_BY:
        raise ValueError(f"group_by must be one of {VALID_GROUP_BY}")
    result = _intel().correlation_matrix(
        _mode(), group_by=group_by, use_cache=not _flag("refresh")
    )
    return jsonify({"success": True, **result})


@intelligence_bp.get("/api/portfolio/intelligence")
@_guard
def portfolio_intelligence():
    intel = _intel()
    intel.maybe_evaluate()
    return jsonify({"success": True, **intel.overview(_mode())})


# ---------------------------------------------------------------------------
# Market context
# ---------------------------------------------------------------------------


@intelligence_bp.get("/api/market/regime")
@_guard
def market_regime():
    intel = _intel()
    intel.maybe_evaluate()
    return jsonify({"success": True, **intel.regime_snapshot(_mode())})


@intelligence_bp.post("/api/market/vix")
@_guard
def market_vix():
    data = request.get_json(silent=True) or {}
    try:
        value = float(data.get("value"))
    except (TypeError, ValueError):
        raise ValueError("value must be a number (India VIX level, e.g. 18.4)") from None
    if not 0 < value < 200:
        raise ValueError("value must be between 0 and 200")
    source = str(data.get("source") or "manual")[:40]
    snap = _intel().ingest_vix(value, source=source)
    log.info("VIX reading %.2f ingested (source=%s)", value, source)
    return jsonify({"success": True, **snap})


@intelligence_bp.get("/api/market/oi-activity")
@_guard
def market_oi_activity():
    symbol = (request.args.get("symbol") or "").strip().upper() or None
    return jsonify({"success": True, **_intel().oi_activity(symbol)})


@intelligence_bp.post("/api/market/chain-activity")
@_guard
def market_chain_activity():
    data = request.get_json(silent=True) or {}
    underlying = str(data.get("underlying") or "").strip().upper()
    rows = data.get("rows")
    if not underlying:
        raise ValueError("underlying is required")
    if not isinstance(rows, list) or not rows:
        raise ValueError("rows must be a non-empty list of chain rows")
    result = _intel().ingest_chain(
        underlying, rows, ts=data.get("ts"), source=str(data.get("source") or "api")[:40]
    )
    return jsonify({"success": True, **result})


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------


@intelligence_bp.get("/api/alerts/active")
@_guard
def alerts_active():
    intel = _intel()
    # Evaluate on read too: a dead feed produces no ticks, and the stale-feed
    # alert must still appear when the only thing running is the UI poll.
    intel.maybe_evaluate()
    return jsonify({"success": True, **intel.alerts_payload(_flag("include_dismissed"))})


def _parse_since(raw: Optional[str]) -> Optional[datetime]:
    if not raw:
        return None
    raw = raw.strip()
    units = {"m": 60, "h": 3600, "d": 86400}
    if raw[-1:].lower() in units and raw[:-1].replace(".", "", 1).isdigit():
        seconds = float(raw[:-1]) * units[raw[-1].lower()]
        return datetime.now(timezone.utc) - timedelta(seconds=seconds)
    try:
        ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("from must be an ISO timestamp/date or a span like 24h, 7d") from None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


@intelligence_bp.get("/api/alerts/history")
@_guard
def alerts_history():
    since = _parse_since(request.args.get("from"))
    alert_type = (request.args.get("type") or "").strip() or None
    severity = (request.args.get("severity") or "").strip().lower() or None
    if alert_type and alert_type not in {t.value for t in AlertType}:
        raise ValueError(f"unknown alert type: {alert_type}")
    if severity and severity not in {s.value for s in Severity}:
        raise ValueError(f"unknown severity: {severity}")
    try:
        limit = max(1, min(1000, int(request.args.get("limit", 200))))
    except ValueError:
        raise ValueError("limit must be an integer") from None

    intel = _intel()
    rows = None
    source = "memory"
    if intel.persister is not None:
        rows = intel.persister.alert_history(
            since=since, alert_type=alert_type, severity=severity, limit=limit
        )
        if rows is not None:
            source = "database"
            rows = [intel._with_context(row, brief=True) for row in rows]
    if rows is None:
        alerts = intel.broker.history(
            since=since, alert_type=alert_type, severity=severity, limit=limit
        )
        rows = [intel._with_context(a.to_dict(), brief=True) for a in alerts]
    return jsonify(
        {
            "success": True,
            "alerts": rows,
            "count": len(rows),
            "source": source,
            "from": since.isoformat() if since else None,
        }
    )


@intelligence_bp.get("/api/alerts/subscriptions")
@_guard
def alerts_subscriptions():
    return jsonify({"success": True, **_intel().broker.subscriptions()})


@intelligence_bp.get("/api/alerts/<alert_id>")
@_guard
def alert_detail(alert_id: str):
    detail = _intel().alert_detail(alert_id)
    if detail is None:
        return _error(f"unknown alert: {alert_id}", 404)
    return jsonify({"success": True, "alert": detail})


def _lifecycle(alert_id: str, action: str):
    broker = _intel().broker
    data = request.get_json(silent=True) or {}
    by = str(data.get("by") or "trader")[:40]
    if action == "dismiss":
        alert = broker.dismiss(alert_id, by=by)
    elif action == "review":
        alert = broker.review(alert_id, by=by)
    else:
        alert = broker.resolve_alert(alert_id, reason=f"manual:{by}")
    if alert is None:
        return _error(f"unknown alert: {alert_id}", 404)
    log.info("alert %s %s by %s (%s)", alert_id[:8], action, by, alert.alert_type)
    return jsonify({"success": True, "alert": alert.to_dict(), "counts": broker.counts()})


@intelligence_bp.post("/api/alerts/<alert_id>/dismiss")
@_guard
def alert_dismiss(alert_id: str):
    return _lifecycle(alert_id, "dismiss")


@intelligence_bp.post("/api/alerts/<alert_id>/review")
@_guard
def alert_review(alert_id: str):
    return _lifecycle(alert_id, "review")


@intelligence_bp.post("/api/alerts/<alert_id>/resolve")
@_guard
def alert_resolve(alert_id: str):
    return _lifecycle(alert_id, "resolve")
