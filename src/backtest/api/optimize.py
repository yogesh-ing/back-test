"""Parameter Optimization REST API (``/api/optimize/*``).

Thin HTTP layer over :class:`backtest.optimization.service.OptimizationService`.
The service is built lazily on first use from the configured database
(``app.config["OPTIMIZATION_SERVICE"]`` injects one — tests do). When no
database is reachable every endpoint answers **503** with a clear message;
the rest of the app is unaffected.

Endpoints
---------
GET    /api/optimize/strategies/<name>/space     default parameter space
POST   /api/optimize/estimate                    validate + time estimate
POST   /api/optimize/runs                        create (+start) a run
GET    /api/optimize/runs                        history (?strategy=&status=)
GET    /api/optimize/runs/<id>                   status / live progress / result
DELETE /api/optimize/runs/<id>                   delete a finished run
POST   /api/optimize/runs/<id>/{start,cancel,pause,resume,rerun}
GET    /api/optimize/runs/<id>/results           paginated, sortable results
GET    /api/optimize/runs/<id>/heatmap           ?x=&y=&metric=&agg=
GET    /api/optimize/runs/<id>/sensitivity       1-D curves + robustness
GET    /api/optimize/runs/<id>/walk-forward      WF report
GET    /api/optimize/runs/<id>/export.csv        every result as CSV
POST   /api/optimize/runs/<id>/apply             apply to runner (audited)
POST   /api/optimize/runs/<id>/presets           save best/selected as preset
GET    /api/optimize/presets                     ?strategy=
PATCH  /api/optimize/presets/<id>                rename / (de)activate
DELETE /api/optimize/presets/<id>
GET    /api/optimize/audit                       ?strategy=&run_id=
POST   /api/optimize/audit/<id>/rollback
"""

from __future__ import annotations

import csv
import io
import threading
from typing import Any, Tuple

from flask import Blueprint, Response, current_app, jsonify, request

from backtest.logging_config import get_logger
from backtest.optimization.config import (
    CONSTRAINT_METRICS,
    MAX_OPTIMIZED_PARAMS,
    METHODS,
    OBJECTIVES,
    ConfigValidationError,
    default_space,
    is_option_strategy,
)
from backtest.optimization.scoring import OBJECTIVE_LABELS
from backtest.optimization.store import RESULT_METRIC_COLUMNS, SORTABLE, clean_json

optimize_bp = Blueprint("optimize_api", __name__)
log = get_logger(__name__)

_build_lock = threading.Lock()


class _Unavailable(Exception):
    pass


def _service():
    """The app's optimization service (built once; None-cached on failure)."""
    app = current_app
    svc = app.config.get("OPTIMIZATION_SERVICE")
    if svc is not None:
        return svc
    ext = app.extensions.setdefault("optimization", {})
    if "service" not in ext:
        with _build_lock:
            if "service" not in ext:
                from backtest.optimization.service import build_default_service

                ext["service"] = build_default_service(app.config)
    if ext["service"] is None:
        raise _Unavailable()
    return ext["service"]


def _ok(payload: dict | list, status: int = 200) -> Tuple[Response, int]:
    body = payload if isinstance(payload, dict) else {"items": payload}
    return jsonify(clean_json({"success": True, **body})), status


def _error(message: str, status: int = 400, **extra: Any) -> Tuple[Response, int]:
    return jsonify(clean_json({"success": False, "error": message, **extra})), status


def _user() -> str | None:
    return (request.headers.get("X-User") or request.headers.get("X-Forwarded-User")
            or (request.get_json(silent=True) or {}).get("user") or None)


def _handle(fn):
    """Map service exceptions to HTTP responses."""
    from functools import wraps

    @wraps(fn)
    def wrapper(*args: Any, **kwargs: Any):
        from backtest.optimization.service import OptimizationError

        try:
            return fn(*args, **kwargs)
        except _Unavailable:
            return _error(
                "Optimization needs a database — set FORWARD_TEST_DB_URL (PostgreSQL) "
                "or run with the dev SQLite profile.", 503)
        except ConfigValidationError as exc:
            return _error("invalid optimization config", 400, errors=exc.errors)
        except OptimizationError as exc:
            return _error(str(exc), exc.status)
        except KeyError as exc:
            return _error(f"not found: {exc}", 404)
        except (TypeError, ValueError) as exc:
            return _error(f"bad request: {exc}", 400)
    return wrapper


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------


@optimize_bp.get("/api/optimize/meta")
def meta() -> Tuple[Response, int]:
    """Static choices for the setup page (no DB needed)."""
    return _ok({
        "objectives": [{"id": o, "label": OBJECTIVE_LABELS[o]} for o in OBJECTIVES],
        "methods": list(METHODS),
        "constraint_metrics": list(CONSTRAINT_METRICS),
        "max_optimized_params": MAX_OPTIMIZED_PARAMS,
        "sortable": list(SORTABLE),
        "metrics": list(RESULT_METRIC_COLUMNS),
    })


@optimize_bp.get("/api/optimize/strategies/<name>/space")
def strategy_space(name: str) -> Tuple[Response, int]:
    from backtest.strategy.registry import get_strategy

    try:
        cls = get_strategy(name)
    except KeyError:
        return _error(f"unknown strategy: {name}", 404)
    option = is_option_strategy(name)
    return _ok({
        "strategy": name,
        "description": getattr(cls, "description", ""),
        "engine": "options" if option else "driver",
        "is_option": option,
        "parameters": default_space(name),
        "default_symbol": "NIFTY" if option else "DEMO",
    })


_RUNNER_FIELDS = ("instance_id", "name", "mode", "status", "strategy_name", "symbols",
                  "timeframe", "allocated_capital")


@optimize_bp.get("/api/optimize/runners")
def list_runners() -> Tuple[Response, int]:
    """Runners the apply dialog can target (``?strategy=`` filter)."""
    strategy = request.args.get("strategy") or None
    try:
        svc = current_app.config.get("OPTIMIZATION_SERVICE")
        manager = svc._manager() if svc is not None else None
        if manager is None:
            from backtest.forward.portfolio_manager import get_portfolio_manager

            manager = get_portfolio_manager()
        states = manager.list_instances(None)
    except Exception as exc:  # noqa: BLE001 - dialog degrades to "new runner" only
        log.warning("[optimize] runner list unavailable: %s", exc)
        return _ok({"runners": [], "error": str(exc)})
    out = []
    for st in states:
        if strategy and st.get("strategy_name") != strategy:
            continue
        runner = manager.get_runner(st.get("instance_id"))
        params = dict(getattr(getattr(runner, "config", None), "strategy_params", {}) or {})
        row = {k: st.get(k) for k in _RUNNER_FIELDS}
        row["strategy_params"] = params
        out.append(row)
    return _ok({"runners": out})


@optimize_bp.post("/api/optimize/estimate")
@_handle
def estimate() -> Tuple[Response, int]:
    svc = _service()
    cfg = svc.parse(request.get_json(silent=True) or {})
    return _ok({"estimate": svc.estimate(cfg), "config": cfg.to_dict()})


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------


@optimize_bp.post("/api/optimize/runs")
@_handle
def create_run() -> Tuple[Response, int]:
    svc = _service()
    doc = request.get_json(silent=True) or {}
    start = bool(doc.pop("start", True)) if isinstance(doc, dict) else True
    run = svc.submit(doc, created_by=_user(), start=start)
    return _ok({"run": run, "run_id": run["run_id"]}, 201)


@optimize_bp.get("/api/optimize/runs")
@_handle
def list_runs() -> Tuple[Response, int]:
    svc = _service()
    limit = max(1, min(int(request.args.get("limit", 50)), 200))
    offset = max(0, int(request.args.get("offset", 0)))
    rows, total = svc.store.list_runs(strategy_id=request.args.get("strategy") or None,
                                      status=request.args.get("status") or None,
                                      limit=limit, offset=offset)
    return _ok({"runs": rows, "total": total, "limit": limit, "offset": offset})


@optimize_bp.get("/api/optimize/runs/<run_id>")
@_handle
def get_run(run_id: str) -> Tuple[Response, int]:
    return _ok({"run": _service().status(run_id)})


@optimize_bp.delete("/api/optimize/runs/<run_id>")
@_handle
def delete_run(run_id: str) -> Tuple[Response, int]:
    _service().delete(run_id)
    return _ok({"deleted": run_id})


@optimize_bp.post("/api/optimize/runs/<run_id>/<action>")
@_handle
def run_action(run_id: str, action: str) -> Tuple[Response, int]:
    svc = _service()
    if action == "cancel":
        return _ok({"run": svc.cancel(run_id)})
    if action == "pause":
        return _ok({"run": svc.pause(run_id)})
    if action == "resume":
        return _ok({"run": svc.resume(run_id)})
    if action == "start":
        return _ok({"run": svc.start(run_id)})
    if action == "rerun":
        body = request.get_json(silent=True) or {}
        run = svc.rerun(run_id, created_by=_user(), overrides=body.get("overrides"))
        return _ok({"run": run, "run_id": run["run_id"]}, 201)
    if action == "apply":
        return _apply(run_id)
    if action == "presets":
        body = request.get_json(silent=True) or {}
        name = str(body.get("name") or "").strip()
        if not name:
            return _error("preset name is required")
        preset = svc.save_preset_from_run(run_id, name=name,
                                          description=body.get("description"),
                                          params=body.get("params"), created_by=_user())
        return _ok({"preset": preset}, 201)
    return _error(f"unknown action: {action}", 404)


def _apply(run_id: str) -> Tuple[Response, int]:
    body = request.get_json(silent=True) or {}
    result = _service().apply(
        run_id,
        target=str(body.get("target") or "none"),
        params=body.get("params"),
        instance_id=body.get("instance_id") or None,
        confirm_live=bool(body.get("confirm_live")),
        allow_unvalidated=bool(body.get("allow_unvalidated")),
        capital=body.get("capital"),
        name=body.get("name"),
        user_id=_user(),
        ip_address=request.headers.get("X-Forwarded-For", request.remote_addr),
        user_agent=request.headers.get("User-Agent"),
        notes=body.get("notes"),
    )
    return _ok(result)


@optimize_bp.get("/api/optimize/runs/<run_id>/results")
@_handle
def run_results(run_id: str) -> Tuple[Response, int]:
    svc = _service()
    run = svc.status(run_id)
    limit = max(1, min(int(request.args.get("limit", 50)), 500))
    offset = max(0, int(request.args.get("offset", 0)))
    sort = request.args.get("sort", "objective_score")
    order = "asc" if request.args.get("order", "desc").lower() == "asc" else "desc"
    compliant = request.args.get("compliant", "").lower() in ("1", "true", "yes")
    live = svc.live_results(run_id)
    if live is not None:  # running: serve the in-memory rows
        rows = [r for r in live if r["constraints_met"]] if compliant else live
        key = "score" if sort in ("objective_score", "rank") else sort

        def sort_key(r: dict) -> float:
            v = r["score"] if key == "score" else r["metrics"].get(key)
            return float("-inf") if v is None else float(v)

        rows = sorted(rows, key=sort_key, reverse=(order == "desc"))
        page = [{"params": r["params"], "objective_score": r["score"],
                 "constraints_met": r["constraints_met"], "rank": None,
                 "constraint_violations": r["violations"], "error": r["error"],
                 "origin": r.get("origin", "search"), **{
                     c: r["metrics"].get(c) for c in RESULT_METRIC_COLUMNS}}
                for r in rows[offset:offset + limit]]
        return _ok({"results": page, "total": len(rows), "live": True,
                    "limit": limit, "offset": offset})
    rows, total = svc.store.get_results(run_id, sort=sort, order=order, limit=limit,
                                        offset=offset, compliant_only=compliant)
    return _ok({"results": rows, "total": total, "live": False, "limit": limit,
                "offset": offset, "status": run["status"]})


@optimize_bp.get("/api/optimize/runs/<run_id>/heatmap")
@_handle
def run_heatmap(run_id: str) -> Tuple[Response, int]:
    x, y = request.args.get("x"), request.args.get("y")
    if not x or not y or x == y:
        return _error("x and y must be two different parameter names")
    metric = request.args.get("metric", "score")
    if metric != "score" and metric not in RESULT_METRIC_COLUMNS:
        return _error(f"unknown metric: {metric}")
    agg = request.args.get("agg", "max")
    if agg not in ("max", "mean", "slice"):
        return _error("agg must be max, mean or slice")
    compliant = request.args.get("compliant", "").lower() in ("1", "true", "yes")
    data = _service().heatmap(run_id, x, y, metric=metric, agg=agg, compliant_only=compliant)
    return _ok({"heatmap": data})


@optimize_bp.get("/api/optimize/runs/<run_id>/sensitivity")
@_handle
def run_sensitivity(run_id: str) -> Tuple[Response, int]:
    run = _service().status(run_id)
    analysis = run.get("analysis") or {}
    return _ok({
        "sensitivity": analysis.get("sensitivity") or {},
        "robustness": analysis.get("robustness"),
        "cluster": analysis.get("cluster"),
        "warnings": analysis.get("warnings") or [],
    })


@optimize_bp.get("/api/optimize/runs/<run_id>/walk-forward")
@_handle
def run_walk_forward(run_id: str) -> Tuple[Response, int]:
    run = _service().status(run_id)
    if not run.get("walk_forward_enabled"):
        return _ok({"walk_forward": None, "enabled": False})
    wf = run.get("walk_forward_results")
    if wf is None and run.get("progress"):
        wf = {"splits": run["progress"].get("walk_forward_splits") or [], "partial": True}
    return _ok({"walk_forward": wf, "enabled": True})


@optimize_bp.get("/api/optimize/runs/<run_id>/export.csv")
@_handle
def run_export(run_id: str) -> Response:
    svc = _service()
    run = svc.status(run_id)
    rows, _ = svc.store.get_results(run_id, limit=10**7)
    names = sorted({k for r in rows for k in (r.get("params") or {})})
    buf = io.StringIO()
    writer = csv.writer(buf)
    metric_cols = list(RESULT_METRIC_COLUMNS)
    writer.writerow(["rank", "objective_score", "constraints_met", "origin", *names,
                     *metric_cols, "violations"])
    for r in rows:
        viol = "; ".join(
            f"{v.get('metric')} {v.get('operator')} {v.get('limit')} (got {v.get('actual')})"
            if "metric" in v else str(v.get("error"))
            for v in (r.get("constraint_violations") or []))
        writer.writerow([r.get("rank"), r.get("objective_score"), r.get("constraints_met"),
                         r.get("origin"), *[(r.get("params") or {}).get(n) for n in names],
                         *[r.get(c) for c in metric_cols], viol])
    filename = f"optimization_{run['strategy_id']}_{run_id[:8]}.csv"
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


# ---------------------------------------------------------------------------
# Presets
# ---------------------------------------------------------------------------


@optimize_bp.get("/api/optimize/presets")
@_handle
def list_presets() -> Tuple[Response, int]:
    svc = _service()
    presets = svc.store.list_presets(
        request.args.get("strategy") or None,
        include_defaults=request.args.get("defaults", "1") not in ("0", "false"),
        active_only=request.args.get("all", "") not in ("1", "true"))
    return _ok({"presets": presets})


@optimize_bp.post("/api/optimize/presets")
@_handle
def create_preset() -> Tuple[Response, int]:
    body = request.get_json(silent=True) or {}
    strategy = str(body.get("strategy_id") or "").strip()
    name = str(body.get("name") or "").strip()
    params = body.get("params")
    if not strategy or not name or not isinstance(params, dict) or not params:
        return _error("strategy_id, name and a non-empty params object are required")
    preset = _service().store.create_preset(
        strategy_id=strategy, name=name, params=params, source="manual",
        description=body.get("description"), created_by=_user())
    return _ok({"preset": preset}, 201)


@optimize_bp.patch("/api/optimize/presets/<preset_id>")
@_handle
def update_preset(preset_id: str) -> Tuple[Response, int]:
    body = request.get_json(silent=True) or {}
    preset = _service().store.update_preset(preset_id, **body)
    if preset is None:
        return _error("preset not found", 404)
    return _ok({"preset": preset})


@optimize_bp.delete("/api/optimize/presets/<preset_id>")
@_handle
def delete_preset(preset_id: str) -> Tuple[Response, int]:
    svc = _service()
    preset = svc.store.get_preset(preset_id)
    if preset is None:
        return _error("preset not found", 404)
    if preset["source"] == "default":
        return _error("default presets cannot be deleted — deactivate instead", 409)
    svc.store.delete_preset(preset_id)
    return _ok({"deleted": preset_id})


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


@optimize_bp.get("/api/optimize/audit")
@_handle
def list_audit() -> Tuple[Response, int]:
    limit = max(1, min(int(request.args.get("limit", 100)), 500))
    entries = _service().store.list_audit(strategy_id=request.args.get("strategy") or None,
                                          run_id=request.args.get("run_id") or None,
                                          limit=limit)
    return _ok({"audit": entries})


@optimize_bp.post("/api/optimize/audit/<audit_id>/rollback")
@_handle
def rollback(audit_id: str) -> Tuple[Response, int]:
    result = _service().rollback(
        audit_id, user_id=_user(),
        ip_address=request.headers.get("X-Forwarded-For", request.remote_addr),
        user_agent=request.headers.get("User-Agent"))
    return _ok(result)
