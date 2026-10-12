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

from backtest.optimization.attestation import (
    SYNTHETIC_ACKNOWLEDGEMENT,
    attestation_is_satisfied,
    attestation_warnings,
)

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

from backtest.api.data_guard import guard_source  # noqa: E402  (cycle-free sibling)
from backtest.engine.monte_carlo import DEFAULT_SIMULATIONS

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
    if isinstance(body.get("run"), dict):
        # Every run payload carries its own provenance stamp (PRD
        # backTest-enhance §1.2) so the results page never has to guess which
        # engine and which data produced the numbers.
        body = {**body, "run": {**body["run"], "provenance": _run_provenance(body["run"])}}
    return jsonify(clean_json({"success": True, **body})), status


def _service_default_source() -> str:
    """What the optimizer loads candles from, for a run that never recorded it.

    A run created through the service always carries ``backtestConfig.source``;
    this only speaks for older or test-built records that predate that. It is
    the deployment's configured source — never the literal ``"synthetic"``,
    which claimed generated data for runs that read real candles.
    """
    try:
        svc = _service()
    except Exception:  # noqa: BLE001 — no service means no better answer
        svc = None
    named = getattr(svc, "default_source", None) if svc is not None else None
    return str(named or current_app.config.get("BACKTEST_SOURCE") or "")


def _run_provenance(run: dict) -> dict:
    """The data provenance block for a stored optimize run.

    Read back from the run row — never from whatever source the app happens to
    be started with when the page is opened.

    Two paths, in order of honesty:

    1. **The stored attestation** (PRD Part 2 §2, migration 015) — measured on
       the candles the search actually loaded, recorded at the time.
    2. **A derived block** for runs predating that migration — rebuilt from
       ``backtest_config`` (what the run was asked to use) and
       ``analysis.stats`` (what it loaded).

    The fallback is marked ``derived: true`` so the UI can say "reconstructed"
    rather than presenting a rebuilt record with the same authority as a
    measured one. A record that cannot say how it was made is worth less than
    one that can, and pretending otherwise is the failure mode §2 exists to
    prevent.
    """
    stored = run.get("data_attestation") or {}

    from backtest.data.provenance import build_provenance

    bt = dict(run.get("backtest_config") or {})
    stats = dict((run.get("analysis") or {}).get("stats") or {})
    record = build_provenance(
        # The search records no source of its own, so the next honest answer is
        # the source the engine actually loads candles from — not a hard-coded
        # "synthetic", which used to stamp generated-data provenance on runs
        # that read the database.
        source=bt.get("source") or _service_default_source(),
        engine=bt.get("engine") or "driver",
        symbol=bt.get("symbol") or "",
        timeframe=bt.get("timeframe") or "",
        start_date=bt.get("startDate") or bt.get("start_date"),
        end_date=bt.get("endDate") or bt.get("end_date"),
        bars=stats.get("bars"),
        data_from=stats.get("data_from"),
        data_to=stats.get("data_to"),
    )
    if not stored:
        record["derived"] = True
        return record

    # Merge rather than replace. The stored attestation is the DATA half only —
    # it is measured on candles. The ENGINE half still comes from
    # build_provenance, because a run that remembers its data but has lost its
    # engine label is not more complete, just differently incomplete.
    merged = {**record, **stored, "derived": False}
    # The attestation's own naming is authoritative where the two overlap; the
    # derived block spelled these differently and the page must show one.
    merged["date_from"] = stored.get("date_from")
    merged["date_to"] = stored.get("date_to")
    return merged


def _error(message: str, status: int = 400, **extra: Any) -> Tuple[Response, int]:
    return jsonify(clean_json({"success": False, "error": message, **extra})), status


def _user() -> str | None:
    return (
        request.headers.get("X-User")
        or request.headers.get("X-Forwarded-User")
        or (request.get_json(silent=True) or {}).get("user")
        or None
    )


def _option_refusal(doc: Any) -> Tuple[Response, int] | None:
    """A 400 when the config names an options strategy, else ``None``.

    Optimization runs on DB candles and the DB stores no historical option
    chains, so an option robustness score would be Black-Scholes over prices
    nobody traded. The evaluator refuses at the dispatch
    (:mod:`backtest.optimization.evaluator`); this is the same rule at the
    request boundary, where it costs a status code instead of a dead run.
    """
    if not isinstance(doc, dict):
        return None
    sid = str(doc.get("strategyId") or doc.get("strategy_id") or "").strip()
    if not sid:
        return None
    try:
        from backtest.api.backtest import _refuse_option_in_backtest
        from backtest.strategy.registry import get_strategy

        message = _refuse_option_in_backtest(get_strategy(sid), "optimize")
    except Exception:  # noqa: BLE001 — an unknown id is the endpoint's to report
        return None
    return _error(message) if message else None


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
                "or run with the dev SQLite profile.",
                503,
            )
        except ConfigValidationError as exc:
            return _error("invalid optimization config", 400, errors=exc.errors)
        except OptimizationError as exc:
            # `code` lets the setup page tell "tick the synthetic box" apart
            # from every other refusal without matching on the wording.
            return _error(str(exc), exc.status, code=exc.code, **exc.details)
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
    return _ok(
        {
            "objectives": [{"id": o, "label": OBJECTIVE_LABELS[o]} for o in OBJECTIVES],
            "methods": list(METHODS),
            "constraint_metrics": list(CONSTRAINT_METRICS),
            "max_optimized_params": MAX_OPTIMIZED_PARAMS,
            "sortable": list(SORTABLE),
            "metrics": list(RESULT_METRIC_COLUMNS),
        }
    )


@optimize_bp.get("/api/optimize/strategies/<name>/space")
def strategy_space(name: str) -> Tuple[Response, int]:
    from backtest.strategy.registry import get_strategy

    try:
        cls = get_strategy(name)
    except KeyError:
        return _error(f"unknown strategy: {name}", 404)
    option = is_option_strategy(name)
    return _ok(
        {
            "strategy": name,
            "description": getattr(cls, "description", ""),
            "engine": "options" if option else "driver",
            "is_option": option,
            "parameters": default_space(name),
            "default_symbol": "NIFTY" if option else "DEMO",
        }
    )


_RUNNER_FIELDS = (
    "instance_id",
    "name",
    "mode",
    "status",
    "strategy_name",
    "symbols",
    "timeframe",
    "allocated_capital",
)


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
    _refused = guard_source()
    if _refused:
        return _refused  # type: ignore[return-value]
    svc = _service()
    doc = request.get_json(silent=True) or {}
    refused = _option_refusal(doc)
    if refused:
        return refused  # type: ignore[return-value]
    cfg = svc.parse(doc)
    return _ok({"estimate": svc.estimate(cfg), "config": cfg.to_dict()})


@optimize_bp.post("/api/optimize/attestation")
@_handle
def data_attestation() -> Tuple[Response, int]:
    """PRD Part 2 §2 — the setup page's data confirmation box.

    A preview only: bar count and actual coverage are not knowable until the
    candles are fetched, so this reports them as unknown rather than guessing.
    The run record carries the measured version once the search has loaded them.
    """
    _refused = guard_source()
    if _refused:
        return _refused  # type: ignore[return-value]
    svc = _service()
    doc = request.get_json(silent=True) or {}
    att = svc.attestation_for(doc)
    return _ok(
        {
            "attestation": att,
            "warnings": attestation_warnings(att),
            "satisfied": attestation_is_satisfied(att),
            "acknowledgement": SYNTHETIC_ACKNOWLEDGEMENT,
        }
    )


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------


@optimize_bp.post("/api/optimize/runs")
@_handle
def create_run() -> Tuple[Response, int]:
    _refused = guard_source()
    if _refused:
        return _refused  # type: ignore[return-value]
    svc = _service()
    doc = request.get_json(silent=True) or {}
    refused = _option_refusal(doc)
    if refused:
        return refused  # type: ignore[return-value]
    start = bool(doc.pop("start", True)) if isinstance(doc, dict) else True
    run = svc.submit(doc, created_by=_user(), start=start)
    return _ok({"run": run, "run_id": run["run_id"]}, 201)


@optimize_bp.get("/api/optimize/runs")
@_handle
def list_runs() -> Tuple[Response, int]:
    svc = _service()
    limit = max(1, min(int(request.args.get("limit", 50)), 200))
    offset = max(0, int(request.args.get("offset", 0)))
    rows, total = svc.store.list_runs(
        strategy_id=request.args.get("strategy") or None,
        status=request.args.get("status") or None,
        limit=limit,
        offset=offset,
    )
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


@optimize_bp.post("/api/optimize/runs/<run_id>/monte-carlo")
@_handle
def monte_carlo(run_id: str) -> Tuple[Response, int]:
    """PRD Part 2 §4 — Monte Carlo on the winning result.

    On the best result only, not on every candidate: the winner is the one
    that would go to paper, and the question is whether *its* trade sequence
    is a lucky ordering. Walk-forward already asked whether the parameters
    generalise across time; this asks a different question about the same run.

    Explicitly **not** guarded by the data-source policy: the candles were
    already read to produce this run, and the resampling is arithmetic on
    trades that exist. Refusing it would remove a check on a result the user
    can already see.
    """
    doc = request.get_json(silent=True) or {}
    try:
        simulations = int(doc.get("simulations", DEFAULT_SIMULATIONS))
    except (TypeError, ValueError):
        return _error("simulations must be an integer", 400)
    if simulations < 2 or simulations > 50_000:
        return _error("simulations must be between 2 and 50000", 400)
    return _ok({"monte_carlo": _service().monte_carlo_best(run_id, simulations=simulations)})


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
        preset = svc.save_preset_from_run(
            run_id,
            name=name,
            description=body.get("description"),
            params=body.get("params"),
            created_by=_user(),
        )
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
        monte_carlo_acknowledged=bool(body.get("monte_carlo_acknowledged")),
        monte_carlo_profit_probability=body.get("monte_carlo_profit_probability"),
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
        page = [
            {
                "params": r["params"],
                "objective_score": r["score"],
                "constraints_met": r["constraints_met"],
                "rank": None,
                "constraint_violations": r["violations"],
                "error": r["error"],
                "origin": r.get("origin", "search"),
                **{c: r["metrics"].get(c) for c in RESULT_METRIC_COLUMNS},
            }
            for r in rows[offset : offset + limit]
        ]
        return _ok(
            {"results": page, "total": len(rows), "live": True, "limit": limit, "offset": offset}
        )
    rows, total = svc.store.get_results(
        run_id, sort=sort, order=order, limit=limit, offset=offset, compliant_only=compliant
    )
    return _ok(
        {
            "results": rows,
            "total": total,
            "live": False,
            "limit": limit,
            "offset": offset,
            "status": run["status"],
        }
    )


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
    return _ok(
        {
            "sensitivity": analysis.get("sensitivity") or {},
            "robustness": analysis.get("robustness"),
            "cluster": analysis.get("cluster"),
            "warnings": analysis.get("warnings") or [],
        }
    )


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
    writer.writerow(
        ["rank", "objective_score", "constraints_met", "origin", *names, *metric_cols, "violations"]
    )
    for r in rows:
        viol = "; ".join(
            (
                f"{v.get('metric')} {v.get('operator')} {v.get('limit')} (got {v.get('actual')})"
                if "metric" in v
                else str(v.get("error"))
            )
            for v in (r.get("constraint_violations") or [])
        )
        writer.writerow(
            [
                r.get("rank"),
                r.get("objective_score"),
                r.get("constraints_met"),
                r.get("origin"),
                *[(r.get("params") or {}).get(n) for n in names],
                *[r.get(c) for c in metric_cols],
                viol,
            ]
        )
    filename = f"optimization_{run['strategy_id']}_{run_id[:8]}.csv"
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


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
        active_only=request.args.get("all", "") not in ("1", "true"),
    )
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
        strategy_id=strategy,
        name=name,
        params=params,
        source="manual",
        description=body.get("description"),
        # PRD R3 lineage: a preset saved from a plain backtest names the
        # ledger row it came from (nullable; SET NULL if that run is removed).
        backtest_run_id=body.get("backtest_run_id") or None,
        created_by=_user(),
    )
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
    entries = _service().store.list_audit(
        strategy_id=request.args.get("strategy") or None,
        run_id=request.args.get("run_id") or None,
        limit=limit,
    )
    return _ok({"audit": entries})


@optimize_bp.post("/api/optimize/audit/<audit_id>/rollback")
@_handle
def rollback(audit_id: str) -> Tuple[Response, int]:
    result = _service().rollback(
        audit_id,
        user_id=_user(),
        ip_address=request.headers.get("X-Forwarded-For", request.remote_addr),
        user_agent=request.headers.get("User-Agent"),
    )
    return _ok(result)
