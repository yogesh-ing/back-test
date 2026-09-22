"""Portfolio Command Center API (PRD Phase 5 + Risk Management).

REST + SSE surface for PortfolioManager plus Risk Board:
* summary, buckets, equity, audit
* risk board, aggregated trades, risk config (GET/POST)
* runner create/control, bulk control, emergency, breach test, stream
"""

from __future__ import annotations

import json
import time
from typing import Tuple

from flask import Blueprint, Response, current_app, jsonify, request
from flask import stream_with_context

from backtest.data.universe import (
    CORRELATION_GROUPS,
    correlation_group_for,
    is_universe,
    list_universes,
)
from backtest.forward.paper_runner import TARGET_POOL, TARGET_SINGLE, RunnerConfig
from backtest.forward.portfolio_manager import get_portfolio_manager
from backtest.logging_config import get_logger
from backtest.simulator.bucket_risk import BUCKET_RISK_LIMITS

portfolio_bp = Blueprint("portfolio_api", __name__)
log = get_logger(__name__)


def _manager():
    return get_portfolio_manager()


VALID_INSTANCE_MODES = ("paper", "live")


def list_instances(mode: str | None = None) -> list[dict]:
    return _manager().list_instances(mode)


def _error(message: str, status: int = 400) -> Tuple[Response, int]:
    log.warning("rejected (%d): %s", status, message)
    return jsonify({"success": False, "error": message}), status


def _parse_target(data: dict) -> Tuple[str, list, str | None]:
    target_type = str(data.get("target_type", "")).upper()
    target = data.get("target") or data.get("symbol") or data.get("universe") or ""
    symbols = data.get("symbols") or []
    universe_id = data.get("universe_id")

    if universe_id or is_universe(str(target)):
        uid = universe_id or str(target)
        if not is_universe(uid):
            raise ValueError(f"unknown universe: {uid}")
        from backtest.data.universe import get_universe_symbols

        return TARGET_POOL, get_universe_symbols(uid), uid.upper()

    if target_type == TARGET_POOL:
        if not symbols:
            raise ValueError("pool runner needs a universe_id or symbols")
        return TARGET_POOL, [str(s).upper() for s in symbols], None

    sym = symbols[0] if symbols else target
    if not sym:
        raise ValueError("single runner needs a symbol")
    return TARGET_SINGLE, [str(sym).upper()], None


# ---------------------------------------------------------------------------
# Summary / meta
# ---------------------------------------------------------------------------


@portfolio_bp.get("/api/portfolio/summary")
def summary() -> Tuple[Response, int]:
    mode = request.args.get("mode") or None
    try:
        return jsonify(
            {"success": True, "portfolio": _manager().get_portfolio_summary(mode)}
        ), 200
    except ValueError as exc:
        return _error(str(exc))


@portfolio_bp.get("/api/portfolio/universes")
def universes() -> Tuple[Response, int]:
    return jsonify({"success": True, "universes": list_universes()}), 200


@portfolio_bp.get("/api/portfolio/buckets")
def buckets() -> Tuple[Response, int]:
    return jsonify(
        {"success": True, "buckets": _manager().get_bucket_aggregates()}
    ), 200


@portfolio_bp.get("/api/portfolio/equity/snapshot")
def equity_snapshot() -> Tuple[Response, int]:
    mode = request.args.get("mode") or None
    try:
        snap = _manager().get_equity_snapshots(mode=mode)
    except ValueError as exc:
        return _error(str(exc), 400)
    return jsonify({"success": True, "snapshot": snap}), 200


@portfolio_bp.get("/api/portfolio/audit")
def audit_log() -> Tuple[Response, int]:
    scope = request.args.get("scope") or None
    try:
        limit = int(request.args.get("limit", 100))
    except (TypeError, ValueError):
        limit = 100
    limit = max(1, min(limit, 1000))
    try:
        entries = _manager().get_audit_log(scope=scope, limit=limit)
    except Exception:
        entries = []
    return jsonify({"success": True, "audit": entries, "scope": scope or "all"}), 200


# ---------------------------------------------------------------------------
# Risk Board — Tier 2 & Tier 3
# ---------------------------------------------------------------------------


@portfolio_bp.get("/api/portfolio/risk")
def risk_board() -> Tuple[Response, int]:
    mode = request.args.get("mode") or None
    if mode is not None:
        mode = str(mode).strip().lower()
        if mode not in VALID_INSTANCE_MODES:
            return _error(
                f"mode must be one of {VALID_INSTANCE_MODES}, got {mode!r}"
            )

    mgr = _manager()
    try:
        summ = mgr.get_portfolio_summary(mode=mode)
    except ValueError as exc:
        return _error(str(exc))

    buckets_data = summ.get("buckets", {}) or mgr.get_bucket_aggregates()

    bucket_limits = {}
    for bmode, lim in BUCKET_RISK_LIMITS.items():
        try:
            bucket_limits[bmode] = lim.to_dict()
        except Exception:
            bucket_limits[bmode] = {}

    sup_cfg = mgr.supervisor.config
    global_config = {
        "daily_loss_limit": sup_cfg.daily_loss_limit,
        "max_drawdown_pct": sup_cfg.max_drawdown_pct,
        "max_leverage": getattr(sup_cfg, "max_leverage", 1.0),
        "breach_mode": getattr(sup_cfg, "breach_mode", "PAUSE_AND_HOLD"),
        "correlation_warning_threshold": getattr(
            sup_cfg, "correlation_warning_threshold", 3
        ),
    }

    symbol_exposure: dict[str, dict] = {}
    correlation_exposure: dict[str, dict] = {}
    total_gross = 0.0
    try:
        runners = mgr.list_instances(mode=mode)
        for rs in runners:
            inst_id = rs.get("instance_id")
            runner = mgr.get_runner(inst_id)
            if runner is None:
                continue
            for sym, pos in runner.positions.items():
                qty = pos.get("qty", 0) if isinstance(pos, dict) else 0
                entry = pos.get("entry_price", 0) if isinstance(pos, dict) else 0
                last_px = (
                    runner.last_price.get(sym, entry)
                    if hasattr(runner, "last_price")
                    else entry
                )
                notional = abs(float(qty or 0)) * float(last_px or 0)
                total_gross += notional
                if sym not in symbol_exposure:
                    symbol_exposure[sym] = {
                        "symbol": sym,
                        "notional": 0.0,
                        "qty": 0.0,
                        "runners": 0,
                        "sides": set(),
                    }
                symbol_exposure[sym]["notional"] += notional
                symbol_exposure[sym]["qty"] += float(qty or 0)
                symbol_exposure[sym]["runners"] += 1
                side = pos.get("side", "LONG") if isinstance(pos, dict) else "LONG"
                symbol_exposure[sym]["sides"].add(side)

        for sym, data in symbol_exposure.items():
            grp = correlation_group_for(sym) or "OTHER"
            if grp not in correlation_exposure:
                meta = CORRELATION_GROUPS.get(grp, {})
                correlation_exposure[grp] = {
                    "group": grp,
                    "label": meta.get("label", grp),
                    "threshold": meta.get(
                        "threshold", sup_cfg.correlation_warning_threshold
                    ),
                    "notional": 0.0,
                    "symbols": [],
                }
            correlation_exposure[grp]["notional"] += data["notional"]
            correlation_exposure[grp]["symbols"].append(sym)

        for v in symbol_exposure.values():
            v["sides"] = sorted(list(v["sides"]))
            v["notional"] = round(v["notional"], 2)
            v["qty"] = round(v["qty"], 6)

        for v in correlation_exposure.values():
            v["notional"] = round(v["notional"], 2)
            v["symbols"] = sorted(set(v["symbols"]))
            v["count"] = len(v["symbols"])

    except Exception as exc:  # noqa: BLE001
        log.debug("risk exposure breakdown failed: %s", exc)

    trade_stats = {
        "total_trades": 0,
        "wins": 0,
        "losses": 0,
        "win_rate": 0.0,
        "total_pnl": 0.0,
        "avg_win": 0.0,
        "avg_loss": 0.0,
        "profit_factor": 0.0,
        "max_consecutive_losses": 0,
        "best_trade": 0.0,
        "worst_trade": 0.0,
    }
    try:
        all_trades = []
        for rs in mgr.list_instances(mode=mode):
            runner = mgr.get_runner(rs.get("instance_id"))
            if runner is None:
                continue
            all_trades.extend(runner.closed_trades)

        if all_trades:
            pnls = [float(t.get("pnl", 0)) for t in all_trades]
            wins = [p for p in pnls if p >= 0]
            losses = [p for p in pnls if p < 0]
            trade_stats["total_trades"] = len(pnls)
            trade_stats["wins"] = len(wins)
            trade_stats["losses"] = len(losses)
            trade_stats["win_rate"] = round(len(wins) / len(pnls), 4) if pnls else 0.0
            trade_stats["total_pnl"] = round(sum(pnls), 2)
            trade_stats["avg_win"] = round(sum(wins) / len(wins), 2) if wins else 0.0
            trade_stats["avg_loss"] = round(sum(losses) / len(losses), 2) if losses else 0.0
            trade_stats["best_trade"] = round(max(pnls), 2) if pnls else 0.0
            trade_stats["worst_trade"] = round(min(pnls), 2) if pnls else 0.0
            gross_profit = sum(wins) if wins else 0.0
            gross_loss = abs(sum(losses)) if losses else 0.0
            if gross_loss > 0:
                trade_stats["profit_factor"] = round(gross_profit / gross_loss, 4)

            max_consec = 0
            cur = 0
            for p in pnls:
                if p < 0:
                    cur += 1
                    max_consec = max(max_consec, cur)
                else:
                    cur = 0
            trade_stats["max_consecutive_losses"] = max_consec
    except Exception as exc:  # noqa: BLE001
        log.debug("risk trade stats failed: %s", exc)

    risk_audit = []
    try:
        audit = mgr.get_audit_log(scope=mode or "all", limit=100)
        keywords = ("HALT", "FLATTEN", "BREAKER", "RISK", "BLOCKED", "EMERGENCY", "PAUSE")
        for entry in audit:
            act = str(entry.get("action", "")).upper()
            if any(k in act for k in keywords):
                risk_audit.append(entry)
            elif entry.get("scope") in ("paper", "live"):
                risk_audit.append(entry)
        risk_audit = risk_audit[:50]
    except Exception:
        risk_audit = []

    total_eq = summ.get("total_equity") or 0.0
    gross_pct = round(total_gross / total_eq, 4) if total_eq else 0.0

    payload = {
        "timestamp": summ.get("timestamp"),
        "mode": mode or "all",
        "global_config": global_config,
        "bucket_limits": bucket_limits,
        "buckets": buckets_data,
        "current": {
            "total_equity": summ.get("total_equity", 0.0),
            "total_capital": summ.get("total_capital", 0.0),
            "deployed_capital": summ.get("deployed_capital", 0.0),
            "deployed_pct": summ.get("deployed_pct", 0.0),
            "daily_pnl": summ.get("daily_pnl", 0.0),
            "daily_loss_used": summ.get("daily_loss_used", 0.0),
            "daily_loss_pct": summ.get("daily_loss_pct", 0.0),
            "drawdown_pct": summ.get("drawdown_pct", 0.0),
            "halted": summ.get("halted", False),
            "halt_reason": summ.get("halt_reason"),
            "halt_mode": summ.get("halt_mode"),
            "warnings": summ.get("warnings", []),
        },
        "exposure": {
            "by_symbol": sorted(
                symbol_exposure.values(),
                key=lambda x: x["notional"],
                reverse=True,
            ),
            "by_correlation": sorted(
                correlation_exposure.values(),
                key=lambda x: x["notional"],
                reverse=True,
            ),
            "total_gross_notional": round(total_gross, 2),
            "gross_exposure_pct": gross_pct,
        },
        "trade_stats": trade_stats,
        "risk_audit": risk_audit,
        "capability": summ.get("capability", {}),
    }

    return jsonify({"success": True, "risk": payload}), 200


@portfolio_bp.get("/api/portfolio/trades/aggregated")
def aggregated_trades() -> Tuple[Response, int]:
    mode = request.args.get("mode") or None
    if mode is not None:
        mode = str(mode).strip().lower()
        if mode not in VALID_INSTANCE_MODES:
            return _error(f"mode must be one of {VALID_INSTANCE_MODES}, got {mode!r}")

    kind = (request.args.get("kind") or "all").lower()
    if kind not in ("all", "equity", "option"):
        return _error("kind must be all|equity|option")

    try:
        limit = int(request.args.get("limit", 200))
    except (TypeError, ValueError):
        limit = 200
    limit = max(1, min(limit, 1000))

    mgr = _manager()
    trades: list[dict] = []
    try:
        for rs in mgr.list_instances(mode=mode):
            runner = mgr.get_runner(rs.get("instance_id"))
            if runner is None:
                continue
            for t in runner.closed_trades:
                if kind != "all" and t.get("kind", "equity") != kind:
                    continue
                enriched = dict(t)
                enriched["runner_id"] = rs.get("instance_id")
                enriched["runner_name"] = rs.get("name")
                enriched["strategy"] = rs.get("strategy_name")
                enriched["mode"] = rs.get("mode")
                trades.append(enriched)

        trades.sort(key=lambda x: x.get("exit_ts") or "", reverse=True)
        total = len(trades)
        sliced = trades[:limit]

        pnls = [float(t.get("pnl", 0)) for t in trades]
        summary = {
            "total": total,
            "returned": len(sliced),
            "total_pnl": round(sum(pnls), 2) if pnls else 0.0,
            "win_rate": round(sum(1 for p in pnls if p >= 0) / len(pnls), 4)
            if pnls
            else 0.0,
        }

    except Exception as exc:  # noqa: BLE001
        log.warning("aggregated trades failed: %s", exc)
        return _error(f"failed to aggregate trades: {exc}", 500)

    return jsonify(
        {"success": True, "trades": sliced, "summary": summary, "mode": mode or "all", "kind": kind}
    ), 200


@portfolio_bp.get("/api/portfolio/risk/config")
def risk_config_view() -> Tuple[Response, int]:
    mgr = _manager()
    sup_cfg = mgr.supervisor.config
    limits = {k: v.to_dict() for k, v in BUCKET_RISK_LIMITS.items()}
    corr_safe = {}
    for gid, meta in CORRELATION_GROUPS.items():
        corr_safe[gid] = {
            "label": meta.get("label", gid),
            "symbols": sorted(list(meta.get("symbols", set()))),
            "threshold": meta.get("threshold", 3),
        }
    return jsonify(
        {
            "success": True,
            "global": {
                "daily_loss_limit": sup_cfg.daily_loss_limit,
                "max_drawdown_pct": sup_cfg.max_drawdown_pct,
                "max_leverage": getattr(sup_cfg, "max_leverage", 1.0),
                "breach_mode": getattr(sup_cfg, "breach_mode", "PAUSE_AND_HOLD"),
                "correlation_warning_threshold": getattr(
                    sup_cfg, "correlation_warning_threshold", 3
                ),
            },
            "buckets": limits,
            "correlation_groups": corr_safe,
        }
    ), 200


@portfolio_bp.post("/api/portfolio/risk/config")
def risk_config_save() -> Tuple[Response, int]:
    data = request.get_json(silent=True) or {}
    mgr = _manager()
    updated = {}

    if "global" in data and isinstance(data["global"], dict):
        g = data["global"]
        try:
            sup = mgr.supervisor
            cfg = sup.config
            if "daily_loss_limit" in g:
                cfg.daily_loss_limit = float(g["daily_loss_limit"])
            if "max_drawdown_pct" in g:
                cfg.max_drawdown_pct = float(g["max_drawdown_pct"])
            if "max_leverage" in g:
                cfg.max_leverage = float(g["max_leverage"])
            if "breach_mode" in g:
                cfg.breach_mode = str(g["breach_mode"])
            if "correlation_warning_threshold" in g:
                cfg.correlation_warning_threshold = int(
                    g["correlation_warning_threshold"]
                )
            updated["global"] = True
            log.info("risk config global updated: %s", g)
            try:
                mgr._audit_log(
                    "RISK_CONFIG_UPDATE", scope="all", detail=f"global={g}"
                )
            except Exception:
                pass
        except Exception as exc:
            return _error(f"failed to update global config: {exc}", 500)

    if "buckets" in data and isinstance(data["buckets"], dict):
        try:
            for mode, lim in data["buckets"].items():
                if mode not in BUCKET_RISK_LIMITS:
                    continue
                cur = BUCKET_RISK_LIMITS[mode]
                if "max_position_pct" in lim:
                    cur.max_position_pct = float(lim["max_position_pct"])
                if "max_position_value" in lim:
                    cur.max_position_value = float(lim["max_position_value"])
                if "max_positions" in lim:
                    cur.max_positions = int(lim["max_positions"])
                if "allowed_sources" in lim:
                    cur.allowed_sources = list(lim["allowed_sources"])
            updated["buckets"] = list(data["buckets"].keys())
            log.info("risk config buckets updated: %s", list(data["buckets"].keys()))
            try:
                keys = list(data["buckets"].keys())
                mgr._audit_log(
                    "RISK_BUCKET_UPDATE", scope="all", detail=f"buckets={keys}"
                )
            except Exception:
                pass
        except Exception as exc:
            return _error(f"failed to update buckets: {exc}", 500)

    return jsonify({"success": True, "updated": updated}), 200


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
    instrument = data.get("instrument") or {"type": "equity"}
    if (
        isinstance(instrument, dict)
        and str(instrument.get("type", "equity")).lower() == "option"
        and target_type == TARGET_POOL
    ):
        return _error(
            "option runners trade a single underlying in V1 — use SINGLE_SYMBOL"
        )
    if (
        isinstance(instrument, dict)
        and str(instrument.get("type", "equity")).lower() == "option"
        and target_type == TARGET_SINGLE
    ):
        underlyings = {str(s).strip().upper() for s in (symbols or [])}
        if not underlyings or not underlyings <= {"NIFTY", "BANKNIFTY"}:
            return _error("Option strategies trade an index — pick NIFTY/BANKNIFTY")

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
                float(data["position_pct"])
                if data.get("position_pct") is not None
                else None
            ),
            mode=data.get("mode") or "paper",
            source=data.get("source") or "synthetic",
            instrument=instrument,
            playbook_id=data.get("playbook_id"),
            playbook_version=(
                int(data["playbook_version"])
                if data.get("playbook_version") is not None
                else None
            ),
            playbook_snapshot=(
                data.get("playbook_snapshot")
                or data.get("instrument", {}).get("expression")
            ),
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
    return (
        jsonify(
            {
                "success": True,
                "action": action,
                "scope": state.get("mode", "paper"),
                "runner": state,
            }
        ),
        200,
    )


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
    mode = request.args.get("mode") or None
    if mode is not None:
        mode = str(mode).strip().lower()
        if mode not in VALID_INSTANCE_MODES:
            return _error(f"mode must be one of {VALID_INSTANCE_MODES}, got {mode!r}")
    try:
        if action == "pause_all":
            n = manager.pause_all(mode=mode)
        elif action == "resume_all":
            n = manager.resume_all(mode=mode)
        elif action == "stop_all":
            n = manager.stop_all(mode=mode)
        elif action == "emergency_flatten":
            n = manager.emergency_flatten_all(reason="manual_emergency", mode=mode)
        elif action == "reset_breaker":
            manager.reset_circuit_breaker(mode=mode)
            n = 0
        else:
            return _error(f"unknown bulk action: {action}")
    except RuntimeError as exc:
        return _error(str(exc), 409)
    log.info(
        "bulk action %s%s affected %d runner(s)",
        action,
        f" [{mode}]" if mode else "",
        n,
    )
    return (
        jsonify(
            {
                "success": True,
                "action": action,
                "affected": n,
                "scope": mode or "all",
                "portfolio": manager.get_portfolio_summary(),
            }
        ),
        200,
    )


@portfolio_bp.post("/api/portfolio/emergency_stop")
def emergency_stop() -> Tuple[Response, int]:
    data = request.get_json(silent=True) or {}
    reason = str(data.get("reason", "manual_emergency"))
    mode = data.get("mode")
    if mode is not None:
        mode = str(mode).strip().lower()
        if mode not in VALID_INSTANCE_MODES:
            return _error(f"mode must be one of {VALID_INSTANCE_MODES}, got {mode!r}")
    count = _manager().emergency_flatten_all(reason=reason, mode=mode)
    return (
        jsonify(
            {
                "success": True,
                "flattened_positions": count,
                "scope": mode or "all",
                "portfolio": _manager().get_portfolio_summary(),
            }
        ),
        200,
    )


@portfolio_bp.post("/api/portfolio/test/breach")
def test_breach() -> Tuple[Response, int]:
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
# SSE stream
# ---------------------------------------------------------------------------


@portfolio_bp.get("/api/portfolio/stream")
def stream() -> Response:
    interval = current_app.config.get("PORTFOLIO_SSE_INTERVAL", 1.0)

    @stream_with_context
    def event_stream():
        log.info(
            "SSE stream opened (cadence %.1fs, client=%s)", interval, request.remote_addr
        )
        errors = 0
        yield ": connected to /api/portfolio/stream\n\n"
        while True:
            try:
                payload = _manager().get_portfolio_summary()
                errors = 0
                yield f"event: portfolio\ndata: {json.dumps(payload, default=str)}\n\n"
            except GeneratorExit:
                raise
            except Exception as exc:  # noqa: BLE001
                errors += 1
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
