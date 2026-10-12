"""Backtest endpoints (PRD Tasks 1.5 + 1.6, re-routed in ticket P2.2).

* ``POST /api/backtest/run``      — single strategy deep dive
* ``POST /api/backtest/run-many`` — 2-4 slots in parallel via
  :class:`~concurrent.futures.ProcessPoolExecutor` (ticket P2.3): each slot
  runs :func:`run_single_backtest` in its own worker process with plain-dict
  params, so a crashed job cannot take down the web process.

Both engines are built and run by the canonical entry
:mod:`backtest.engine.backtest_runner` (single bootstrap/key-result wiring —
ticket #6); this module is the HTTP layer only.

* **canonical (default)** — :func:`backtest.engine.backtest_runner.run_backtest`
  (:class:`~backtest.engine.backtest_driver.BacktestDriver` over the simulator
  executor): the SAME loop the forward paper run uses, next-bar-open fills,
  Decimal-exact portfolio accounting. The portfolio's per-bar equity
  snapshots are mapped onto a ``BacktestResult`` so the ``BacktestAdapter``
  payload (metrics/equity/drawdown/trades/signals) is byte-for-byte the same
  shape as before — computed by the same ``engine/metrics`` +
  ``engine/trades`` code as the vectorized path.
* ``mode='quick_screen'`` — the legacy vectorized ``Backtester``
  (:func:`backtest.engine.backtest_runner.run_quick_screen`: prev-close
  fills, fractional sizing, built-in cost model), kept ONLY as an optional
  fast rough filter.

Both log the resolved request, the bars fetched, the engine summary and any
failure with a traceback, so a UI error toast can always be traced back to one
line in the server log (the id is quoted back in the JSON body as
``request_id``).
"""

from __future__ import annotations

import logging
import os
import threading
import traceback
from concurrent.futures import ProcessPoolExecutor
from typing import Any, Optional

import pandas as pd
from flask import Blueprint, current_app, jsonify, request

from backtest.adapters.backtest_adapter import BacktestAdapter
from backtest.api.backtest_run_store import (
    BacktestRunLedger,
    PayloadVersionMismatch,
    build_ledger,
)
from backtest.data.provenance import ENGINE_FILL_EXACT, ENGINE_MIXED
from backtest.engine.backtest_runner import resolve_interval, resolve_warmup_start
from backtest.engine.backtest_runner import run_backtest as _run_driver
from backtest.engine.backtest_runner import run_quick_screen
from backtest.engine.comparison import correlation_matrix, sharpe_significance
from backtest.engine.cost_shock import run_cost_shock
from backtest.engine.monte_carlo import monte_carlo_trade_order
from backtest.engine.readiness import build_readiness
from backtest.logging_config import get_logger, timed
from backtest.api.data_guard import guard_source
from backtest.runner import build_source
from backtest.strategy.registry import get_strategy, signal_kind

backtest_bp = Blueprint("backtest_api", __name__)
log = get_logger(__name__)

# Number of extra bars to load before start_date for strategy warmup.
# Set to 0 so the run covers exactly the requested range and the result
# matches a direct run over the same candles (indicators simply ramp over
# the first bars, as they do in a standalone backtest).
WARMUP_BARS = 0

#: Guards the first lazy build of the app's run ledger (PRD R3). The ledger is
#: a deployment feature: it attaches to the configured candle DB, never a
#: default. Tests/dev apps inject ``app.config["BACKTEST_LEDGER"]`` (a ledger,
#: or ``None`` to disable); a synthetic-source app never touches the DB.
_ledger_lock = threading.Lock()


def _ledger() -> "BacktestRunLedger | None":
    """The app's run ledger (injected, or lazily built once for real sources)."""
    app = current_app
    if "BACKTEST_LEDGER" in app.config:
        return app.config["BACKTEST_LEDGER"]  # explicit ledger or explicit None
    ext = app.extensions.setdefault("backtest_ledger", {})
    if "ledger" not in ext:
        with _ledger_lock:
            if "ledger" not in ext:
                # A synthetic demo app has no candle DB to persist against and
                # must never open the deployment one — cache the decision.
                if str(app.config.get("BACKTEST_SOURCE") or "").lower() == "synthetic":
                    ext["ledger"] = None
                else:
                    ext["ledger"] = build_ledger()
    return ext["ledger"]


def _persist_run(payload: dict, *, strategy_cls: Any, where: str) -> dict:
    """Write a completed single run to the ledger; fail soft but loud.

    Returns the fields to fold into the response (PRD R3): ``{}`` when no
    ledger is configured (dev/synthetic — the run response keeps its plain
    shape), ``{persisted: true, run_id, persist_error: null}`` on success, or
    a ``persisted=false`` field set + alert on a real write failure. The
    parent row commits first, then the heavy series separately; a series
    failure still leaves a listable row with flat metrics, so ``persisted``
    stays ``true`` when the parent landed.
    """
    ledger = _ledger()
    if ledger is None:
        return {}
    try:
        run_id = ledger.save_run(payload, kind="single", strategy_cls=strategy_cls)
        try:
            ledger.save_series(run_id, payload)
        except Exception as exc:  # noqa: BLE001 — series is best-effort
            log.warning("[%s] series write failed run=%s: %s", where, run_id, exc)
        return ledger.ok_persist(run_id)
    except Exception as exc:  # noqa: BLE001 — the run itself already succeeded
        return ledger.fail_persist(where, exc)


def _persist_compare(
    data: dict,
    jobs: list,
    results: dict,
    shared_provenance: dict,
    comparison: Optional[dict],
) -> dict:
    """Write a completed run-many to the ledger; same soft-but-loud contract.

    Transaction 1 (atomic): R1b parent + one R1 child per successful slot.
    Transaction 2+: each child's series separately, best-effort — a series
    failure leaves the parent and flat metrics listable, so ``persisted``
    stays true when transaction 1 landed. Failed slots ride in
    ``slot_errors`` only; the ledger never holds a child without a payload.
    """
    ledger = _ledger()
    if ledger is None:
        return {}
    where = "run-many"
    try:
        children: list = []
        slot_errors: dict = {}
        for job in jobs:
            sid = str(job["id"])
            payload = results.get(sid)
            if isinstance(payload, dict) and "error" in payload:
                slot_errors[sid] = {"error": payload["error"]}
                continue
            if isinstance(payload, dict):
                children.append((sid, payload))

        def cls_for(name: str):
            try:
                resolved = _resolve_strategy(name)
            except Exception:  # noqa: BLE001 — fingerprint is best-effort
                return None
            return resolved if not isinstance(resolved, str) else None

        out = ledger.save_compare(
            config_snapshot={"shared": data.get("shared") or {}, "slots": data.get("slots") or []},
            provenance=shared_provenance,
            comparison_block=comparison,
            slot_errors=slot_errors,
            children=children,
            comparison_mode=shared_provenance.get("comparison_mode") or "strategies",
            strategy_cls_for=cls_for,
        )
        run_ids = out["child_run_ids"]
        for sid, payload in children:
            try:
                ledger.save_series(run_ids[sid], payload)
            except Exception as exc:  # noqa: BLE001 — series is best-effort
                log.warning("[%s] series write failed slot=%s: %s", where, sid, exc)
        return {"persisted": True, "compare_id": out["compare_id"], "persist_error": None}
    except Exception as exc:  # noqa: BLE001 — the comparison itself succeeded
        fields = ledger.fail_persist(where, exc)
        fields.pop("run_id", None)
        fields["compare_id"] = None
        return fields


def _source() -> Any:
    # No default: the source comes from the deployment config alone. A missing
    # key is a misconfiguration and must raise, not quietly become synthetic.
    name = current_app.config["BACKTEST_SOURCE"]
    log.debug("[data] building source %r", name)
    return build_source(name)


def _candles(symbol: str, from_date: str, to_date: str, interval: str):
    """Fetch candles for a symbol at an already-resolved ``interval``.

    Returns ``(source, candles)`` — the source object rides along because the
    provenance stamp may need to ask it when the data was last fetched
    (PRD backTest-enhance §1.2); building a second source to ask would open
    a second connection for one number.
    """
    source = _source()
    return source, source.get_candles(symbol, from_date, to_date, interval)


def _provenance(
    candles,
    *,
    source_name: str,
    engine: str,
    symbol: str,
    timeframe: str,
    from_date: str,
    to_date: str,
    source_obj: Any = None,
) -> dict[str, Any]:
    """Provenance block for one result (engine + data + coverage)."""
    from backtest.data.provenance import build_provenance

    first = last = None
    if candles is not None and len(candles):
        first, last = candles.index[0], candles.index[-1]
    return build_provenance(
        source=source_name,
        engine=engine,
        symbol=symbol,
        timeframe=timeframe,
        start_date=from_date,
        end_date=to_date,
        # None (not 0) when there are no candles of their own — a Compare
        # shared block describes conditions, not a run.
        bars=len(candles) if candles is not None else None,
        data_from=first,
        data_to=last,
        source_obj=source_obj,
    )


def _check_params(strategy_cls: Any, params: dict, where: str) -> list[str]:
    """Diagnostic: check ``params`` against the strategy's declared schema.

    Returns a list of human-readable problems (empty = fine). The UI's number
    inputs already carry ``min``/``max``, but nothing stopped a caller from
    posting any value it liked, which produced a silently vacuous backtest.
    Only *logs* for now — turning these into a 400 is gap G11.
    """
    problems: list[str] = []
    schema = strategy_cls.param_schema()
    for key, value in params.items():
        spec = schema.get(key)
        if spec is None:
            continue  # unknown keys are rejected by Strategy.__init__
        lo, hi, ptype = spec.get("min"), spec.get("max"), spec.get("type")
        if value is None:
            continue
        if ptype in ("int", "float") and isinstance(value, (int, float)):
            if lo is not None and value < lo:
                problems.append(f"{key}={value} is below min {lo}")
            if hi is not None and value > hi:
                problems.append(f"{key}={value} is above max {hi}")
    if problems:
        log.warning("[params] %s rejected: %s", where, "; ".join(problems))
    return problems


def _summarise(payload: dict, label: str, params: dict | None = None) -> None:
    cfg, m = payload.get("config", {}), payload.get("metrics", {})
    log.info(
        "[result] %s bars=%s trades=%s return=%.2f%% sharpe=%.2f maxDD=%.2f%% equity=%.2f",
        label,
        cfg.get("bars"),
        m.get("total_trades"),
        m.get("total_return_pct", 0.0),
        m.get("sharpe", 0.0),
        m.get("max_drawdown_pct", 0.0),
        m.get("final_equity", 0.0),
    )
    if not payload.get("trades"):
        log.warning(
            "[result] %s produced 0 trades over %s bars — check that the date range is "
            "longer than the strategy's warmup (params=%s)",
            label,
            cfg.get("bars"),
            params if params is not None else cfg.get("strategy_params"),
        )


def _resolve_strategy(name: str):
    """Return the strategy class or a (error_message) string."""
    if not name:
        return "strategy is required"
    try:
        return get_strategy(name)
    except KeyError as exc:
        return str(exc)


#: The one question a backtest asks about a strategy before running it.
#:
#: Options are forward-only in this deployment (operator rule, 2026-09-30):
#: ``market_data_cache`` stores **bars**, not historical chains, so an option
#: "backtest" could only ever be Black-Scholes off a generated chain — a P&L
#: curve nobody can check against a chart. The option engines
#: (:class:`~backtest.engine.option_backtest_driver.OptionBacktestDriver`) stay
#: in the tree for the forward/paper paths, where the chain comes from mStock
#: live, but a backtest or compare run that names an option strategy is refused
#: with the reason rather than answered with a synthetic number.
OPTION_IN_BACKTEST_MESSAGE = (
    "'{name}' is an options strategy, and options cannot be backtested here: the data "
    "source for backtests is DB candles, and the DB holds no historical option chains — a "
    "backtested option P&L would be Black-Scholes off a generated chain, not real prices. "
    "Run it in Forward Testing or the Portfolio instead, where the chain comes from mStock "
    "live. Equity strategies are unaffected."
)


def _refuse_option_in_backtest(cls, where: str = "backtest") -> str | None:
    """Message when ``cls`` is an option strategy, else ``None``."""
    if cls is None:
        return None
    if signal_kind(cls) != "option":
        return None
    name = getattr(cls, "name", None) or getattr(cls, "__name__", "strategy")
    log.warning("[%s] refused options strategy %s — backtests are equity-only", where, name)
    return OPTION_IN_BACKTEST_MESSAGE.format(name=name)


def _provenance_log(prov: dict, label: str) -> None:
    """One INFO line naming the engine + data behind a result (PRD §1.1/§1.2).

    A number and its provenance belong in the SAME log line: grepping the log
    for a run has to answer "which engine, which data" without a second query
    against the run record.
    """
    log.info(
        "[prov] %s engine=%s (%s) data=%s (%s) symbol=%s tf=%s %s..%s bars=%s fetched=%s",
        label,
        prov.get("engine_used"),
        prov.get("engine_label"),
        prov.get("data_source"),
        prov.get("data_source_label"),
        prov.get("symbol"),
        prov.get("timeframe"),
        prov.get("data_from"),
        prov.get("data_to"),
        prov.get("bars_count"),
        prov.get("data_fetch_date"),
    )
    for warning in prov.get("warnings") or []:
        log.warning("[prov] %s %s: %s", label, warning.get("level"), warning.get("message"))


# ---------------------------------------------------------------------------
# Engines
# ---------------------------------------------------------------------------

#: Request mode that keeps the legacy vectorized path (fast rough filter only).
QUICK_SCREEN = "quick_screen"


#: ``_run_driver`` is the DEFAULT engine path — the canonical entry in
#: :mod:`backtest.engine.backtest_runner` (imported above). The alias name is
#: kept for import compatibility (tests/e2e import it from this module).
#: Request mode that keeps the legacy vectorized path (fast rough filter only).
QUICK_SCREEN = "quick_screen"


# ---------------------------------------------------------------------------
# Cost shock (PRD §3.2)
# ---------------------------------------------------------------------------


def _cost_shock(
    candles: Any,
    strategy: str,
    params: dict,
    symbol: str,
    capital: float,
    timeframe: str,
    engine: str,
    metrics: dict,
) -> dict:
    """PRD §3.2 table, or a well-formed "not available" block.

    Quick-screen is the legacy vectorized path with a built-in cost model and
    no slippage argument, so it is reported as unavailable with a reason rather
    than silently skipped — a stress test that is quietly absent is
    indistinguishable from one that passed.

    Two extra engine runs, ~60ms each on 800 bars. The whole block is wrapped
    so a failure degrades the page rather than failing the user's backtest.
    """
    if engine == QUICK_SCREEN:
        return {
            "available": False,
            "reason": "cost shock runs on the canonical engine, not Fast Preview",
            "scenarios": [],
        }
    try:
        block = run_cost_shock(
            candles,
            strategy,
            params,
            symbol,
            capital,
            timeframe,
            _run_driver,
            actual_metrics=metrics,
        )
    except Exception as exc:  # noqa: BLE001 — a diagnostic must not fail the run
        log.warning("[cost-shock] %s/%s failed: %s", strategy, symbol, exc)
        return {"available": False, "reason": f"cost shock failed: {exc}", "scenarios": []}
    log.info(
        "[cost-shock] %s/%s base=%sbps(%s) status=%s rows=%d",
        strategy,
        symbol,
        block.get("base_bps"),
        block.get("base_bps_source"),
        block.get("status"),
        len(block.get("scenarios") or []),
    )
    return block


# ---------------------------------------------------------------------------
# Single backtest
# ---------------------------------------------------------------------------


@backtest_bp.post("/api/backtest/run")
def run_backtest_endpoint() -> tuple:
    _refused = guard_source()
    if _refused:
        return _refused
    data = request.get_json(silent=True) or {}

    strategy = data.get("strategy")
    resolved = _resolve_strategy(strategy)
    if isinstance(resolved, str):
        log.warning("[run] rejected: %s (body keys=%s)", resolved, sorted(data))
        return jsonify({"error": resolved}), 400
    _option_refusal = _refuse_option_in_backtest(resolved, "run")
    if _option_refusal:
        return jsonify({"error": _option_refusal}), 400

    symbol = data.get("symbol", "DEMO")
    from_date = data.get("from_date") or data.get("from")
    to_date = data.get("to_date") or data.get("to")
    if not from_date or not to_date:
        log.warning("[run] rejected: from_date/to_date missing (body keys=%s)", sorted(data))
        return jsonify({"error": "from_date and to_date are required"}), 400
    if from_date > to_date:
        log.warning("[run] rejected: from_date %s > to_date %s", from_date, to_date)
        return jsonify({"error": "from_date must be <= to_date"}), 400

    try:
        capital = float(data.get("capital", 100_000))
    except (TypeError, ValueError):
        log.warning("[run] rejected: capital=%r is not a number", data.get("capital"))
        return jsonify({"error": "capital must be a number"}), 400

    params = data.get("params") or {}
    timeframe = data.get("timeframe", "1D")
    interval = resolve_interval(timeframe)
    mode = str(data.get("mode", "")).strip().lower()
    log.info(
        "[run] strategy=%s symbol=%s timeframe=%s→%s range=%s..%s capital=%s mode=%s params=%s",
        strategy,
        symbol,
        timeframe,
        interval,
        from_date,
        to_date,
        capital,
        mode or "driver",
        params,
    )
    problems = _check_params(resolved, params, f"run/{strategy}")
    if problems:
        log.warning("[run] continuing despite out-of-range params: %s", "; ".join(problems))

    # Calculate warmup start date (extra bars before from_date for strategy warmup)
    warmup_start = resolve_warmup_start(from_date, WARMUP_BARS, log_prefix="[run]")

    try:
        with timed(log, f"[data] fetch {symbol} {warmup_start}..{to_date}", logging.DEBUG) as t:
            source, candles_full = _candles(symbol, warmup_start, to_date, interval)
    except Exception as exc:  # noqa: BLE001
        log.warning("[run] data error for %s: %s", symbol, exc)
        return jsonify({"error": f"data error: {exc}"}), 400
    log.debug("[data] %s → %d bars in %.1f ms", symbol, len(candles_full), t.elapsed_ms)

    try:
        if mode == QUICK_SCREEN:
            # Legacy vectorized quick filter (prev-close fills, built-in costs).
            with timed(log, f"[run] {strategy} on {symbol} (quick_screen)", logging.DEBUG):
                result = run_quick_screen(
                    candles_full,
                    strategy,
                    params,
                    symbol,
                    capital,
                    from_date,
                    to_date,
                    timeframe,
                )
            engine = "quick_screen"
        else:
            # Canonical: BacktestDriver over simulator/ (next-bar-open fills).
            # It runs exactly the fetched range (WARMUP_BARS=0), so no trim.
            with timed(log, f"[run] {strategy} on {symbol} (driver)", logging.DEBUG):
                result = _run_driver(
                    candles_full, strategy, params, symbol, capital, timeframe=timeframe
                )
            engine = "backtest_driver"
    except ValueError as exc:
        log.warning("[run] %s rejected input: %s", strategy, exc)
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:  # noqa: BLE001
        log.exception("[run] %s crashed on %s", strategy, symbol)
        return jsonify({"error": f"backtest failed: {exc}"}), 500

    payload = BacktestAdapter(result).to_all()
    payload["config"].update(
        {"timeframe": timeframe, "from_date": from_date, "to_date": to_date, "engine": engine}
    )
    payload["cost_shock"] = _cost_shock(
        candles_full, strategy, params, symbol, capital, timeframe, engine, result.metrics
    )
    payload["provenance"] = _provenance(
        candles_full,
        source_name=current_app.config["BACKTEST_SOURCE"],
        engine=engine,
        symbol=symbol,
        timeframe=timeframe,
        from_date=from_date,
        to_date=to_date,
        source_obj=source,
    )
    _provenance_log(payload["provenance"], f"run/{strategy}")
    # §5: the advisory traffic light. Computed from the payload that is about
    # to be returned, so it can never describe a different run than the one
    # rendered above it.
    payload["readiness"] = build_readiness(payload)
    _summarise(payload, f"run/{strategy}", params)
    payload.update(_persist_run(payload, strategy_cls=resolved, where=f"run/{strategy}"))
    return jsonify(payload), 200


@backtest_bp.post("/api/backtest/monte-carlo")
def monte_carlo_endpoint() -> tuple:
    """PRD §3.3 — resample a finished result's trades.

    ``/api/backtest/run`` already returns a ``monte_carlo`` block, so this
    endpoint exists for the two cases that need one: re-running with a
    different simulation count, and re-running against a result that is in the
    client's hands rather than the server's. Both call the same
    :func:`~backtest.engine.monte_carlo.monte_carlo_trade_order`, so the two
    paths cannot drift.

    Accepts either an inline ``trades`` list or a ``trades`` payload in the
    same shape ``to_all()`` returns.
    """
    data = request.get_json(silent=True) or {}
    try:
        simulations = int(data.get("simulations", 1000))
    except (TypeError, ValueError):
        log.warning("[mc] rejected: simulations=%r is not an integer", data.get("simulations"))
        return jsonify({"error": "simulations must be an integer"}), 400
    if simulations < 2 or simulations > 50_000:
        log.warning("[mc] rejected: simulations=%s out of range", simulations)
        return jsonify({"error": "simulations must be between 2 and 50000"}), 400

    payload = data.get("result")
    if isinstance(payload, dict) and "trades" in payload:
        raw = payload["trades"]
    else:
        raw = data.get("trades")
    if not isinstance(raw, list):
        return jsonify({"error": "trades (a list) or a result payload is required"}), 400

    try:
        capital = float(data.get("capital", 100_000))
    except (TypeError, ValueError):
        return jsonify({"error": "capital must be a number"}), 400

    pnls = []
    for row in raw:
        if isinstance(row, dict):
            if row.get("is_open"):
                continue  # an open trade has not happened yet
            pnl = row.get("pnl")
        else:
            pnl = row
        try:
            pnls.append(float(pnl))
        except (TypeError, ValueError):
            return jsonify({"error": f"non-numeric trade pnl: {pnl!r}"}), 400

    block = monte_carlo_trade_order(pnls, capital, simulations=simulations)
    log.info(
        "[mc] %d closed trades, %d simulations, P(profit)=%s%%",
        len(pnls),
        simulations,
        (block.get("bootstrap") or {}).get("profit_probability_pct", "n/a"),
    )
    return jsonify(block), 200


# ---------------------------------------------------------------------------
# Cross-strategy comparison (PRD §4.3 / §4.4)
# ---------------------------------------------------------------------------


def _slot_label(job: dict, payload: dict) -> str:
    """A human label that stays unique when slots share a strategy.

    Two slots on the same strategy are common (that is how a parameter sweep
    works), and a correlation matrix keyed on a duplicated name would silently
    collapse them into one row.
    """
    name = str(job.get("strategy") or "?")
    symbol = str(job.get("symbol") or "")
    params = job.get("params") or {}
    detail = ",".join(f"{k}={v}" for k, v in sorted(params.items())) if params else ""
    base = f"{name} · {symbol}" if symbol else name
    return f"{base} ({detail})" if detail else base


def _comparison_block(
    results: dict[str, Any], jobs: list[dict], source_name: str
) -> dict[str, Any]:
    """§4.3 correlation heatmap + §4.4 significance, from the slot payloads.

    Reads the per-bar returns each slot already returns, so the matrix is built
    from the same numbers the table above it is showing. A slot that failed is
    left out of the maths and named in ``excluded`` — it is not silently
    dropped, and it is not given a row of zeros either.
    """
    from backtest.data.base import periods_per_year as annualisation

    returns: dict[str, pd.Series] = {}
    excluded: list[dict[str, str]] = []
    ppy_used = 0.0
    labels_by_id: dict[str, str] = {}

    for job in jobs:
        sid = str(job.get("id"))
        payload = results.get(sid)
        if not isinstance(payload, dict) or "error" in payload:
            excluded.append(
                {
                    "slot": sid,
                    "label": _slot_label(job, {}),
                    "reason": (payload or {}).get("error", "no result"),
                }
            )
            continue
        equity = payload.get("equity") or {}
        values = equity.get("values") or []
        dates = equity.get("dates") or []
        if not values or len(values) != len(dates):
            excluded.append(
                {
                    "slot": sid,
                    "label": _slot_label(job, payload),
                    "reason": "no equity curve",
                }
            )
            continue
        label = _slot_label(job, payload)
        # Disambiguate the rare case of two slots producing the same label.
        if label in returns:
            label = f"{label} (#{sid})"
        labels_by_id[sid] = label
        returns[label] = pd.Series(
            [float(v) for v in values],
            index=pd.Index(dates),
            dtype="float64",
        ).pct_change()
        tf = (payload.get("config") or {}).get("timeframe") or ""
        if tf:
            try:
                ppy_used = max(ppy_used, float(annualisation(tf)))
            except Exception:  # noqa: BLE001 — an unknown timeframe is not fatal
                pass

    # Daily is the engine's own fallback when a run reports no timeframe.
    if ppy_used <= 0:
        ppy_used = float(annualisation("1day"))

    correlation = correlation_matrix(returns)
    significance = sharpe_significance(returns, ppy_used)
    log.info(
        "[run-many] comparison: %d comparable, %d excluded, max |corr|=%s",
        len(returns),
        len(excluded),
        correlation.get("max_correlation"),
    )
    return {
        "labels_by_slot": labels_by_id,
        "periods_per_year": ppy_used,
        "source_name": source_name,
        "correlation": correlation,
        "significance": significance,
        "excluded": excluded,
    }


# ---------------------------------------------------------------------------
# Parallel multi-slot backtest
# ---------------------------------------------------------------------------


@backtest_bp.post("/api/backtest/run-many")
def run_many() -> tuple:
    _refused = guard_source()
    if _refused:
        return _refused
    data = request.get_json(silent=True) or {}
    shared = data.get("shared", {}) or {}
    slots = data.get("slots", []) or []
    if not slots:
        log.warning("[run-many] rejected: no slots (body keys=%s)", sorted(data))
        return jsonify({"error": "at least one slot is required"}), 400
    if len(slots) > 4:
        log.warning("[run-many] rejected: %d slots (max 4)", len(slots))
        return jsonify({"error": "a maximum of 4 slots is supported"}), 400

    symbol = shared.get("symbol", "DEMO")
    from_date = shared.get("from_date") or shared.get("from")
    to_date = shared.get("to_date") or shared.get("to")
    if not from_date or not to_date:
        log.warning("[run-many] rejected: shared.from_date/to_date missing (shared=%s)", shared)
        return jsonify({"error": "shared.from_date and shared.to_date are required"}), 400
    try:
        capital = float(shared.get("capital", 100_000))
    except (TypeError, ValueError):
        log.warning("[run-many] rejected: shared.capital=%r", shared.get("capital"))
        return jsonify({"error": "shared.capital must be a number"}), 400
    log.info(
        "[run-many] %d slots on %s %s..%s capital=%s — %s",
        len(slots),
        symbol,
        from_date,
        to_date,
        capital,
        ", ".join(
            f"#{sl.get('id')}:{sl.get('strategy')}@{sl.get('timeframe', '1D')}" for sl in slots
        ),
    )

    source_name = current_app.config["BACKTEST_SOURCE"]

    # Calculate warmup start date
    warmup_start = resolve_warmup_start(
        from_date,
        WARMUP_BARS,
        log_prefix="[run-many]",
        label="shared.from_date",
    )

    # One PLAIN-DICT job per slot (P2.3): the work runs in a process pool,
    # so job params must be picklable plain data — no source objects, no
    # closures, no lambdas. Each worker process rebuilds its own source by
    # name (sources are deterministic/plain-constructor, so this is exact).
    # §4.2 "Test Generalization": one strategy, up to four SYMBOLS. The shared
    # symbol stays the default for every slot, so the ordinary
    # "different strategies, same data" mode is byte-identical to before; only
    # a slot that names its own symbol opts out. Everything else — dates,
    # capital, engine, timeframe — stays shared, because a comparison across
    # different date ranges or engines is not a comparison.
    slot_symbols = {
        str(slot.get("id")): str(slot.get("symbol") or symbol).strip().upper() for slot in slots
    }
    jobs = [
        {
            "id": slot.get("id"),
            "strategy": slot.get("strategy"),
            "params": slot.get("params") or {},
            "timeframe": slot.get("timeframe", "1D"),
            "mode": str(slot.get("mode", "")).strip().lower(),
            "symbol": slot_symbols[str(slot.get("id"))],
            "from_date": from_date,
            "to_date": to_date,
            "warmup_start": warmup_start,
            "capital": capital,
            "source_name": source_name,
        }
        for slot in slots
    ]

    max_workers = min(4, len(slots))
    with timed(log, f"[run-many] {len(slots)} slots (process pool)", logging.INFO) as t:
        with ProcessPoolExecutor(max_workers=max_workers) as pool:
            # chunksize=1: each slot is its own task, so one crashing job
            # cannot swallow its neighbours' results.
            payloads = list(pool.map(run_single_backtest, jobs, chunksize=1))

    # Per-slot logging happens HERE, in the web process: worker-process log
    # records do not surface in the web process's log capture (process
    # boundary), so the endpoint re-emits the canonical slot lines — with
    # the request id — and the worker's traceback rides back in the payload.
    results: dict[str, Any] = {}
    for job, payload in zip(jobs, payloads):
        sid = str(job["id"])
        results[sid] = payload
        if isinstance(payload, dict) and "error" in payload:
            traceback_text = payload.pop("traceback", None)
            log.warning("[slot %s] failed: %s", sid, payload["error"])
            if traceback_text:
                log.debug("[slot %s] traceback:\n%s", sid, traceback_text)
        else:
            _summarise(
                payload,
                f"slot {sid}/{job.get('strategy')}@{job.get('timeframe', '1D')}",
                job.get("params") or {},
            )
    failed = [k for k, v in results.items() if isinstance(v, dict) and "error" in v]
    log.info(
        "[run-many] done in %.1f ms: %d ok, %d failed%s",
        t.elapsed_ms,
        len(results) - len(failed),
        len(failed),
        f" (slots {', '.join(failed)})" if failed else "",
    )

    # One provenance block for the conditions every slot SHARES, so a
    # comparison page can badge the run without re-deriving the source from
    # four payloads. Per-slot engines are compared here: slots that disagree
    # are stamped "mixed" and warned about, because their numbers are not
    # like-for-like (PRD §1.1 / §4.1).
    slot_engines = {str(job.get("mode", "")).strip().lower() or ENGINE_FILL_EXACT for job in jobs}
    shared_engine = next(iter(slot_engines)) if len(slot_engines) == 1 else ENGINE_MIXED
    # In generalization mode the slots deliberately run DIFFERENT symbols, so
    # the shared badge must name that rather than the (unused) shared symbol.
    distinct_symbols = sorted({job["symbol"] for job in jobs})
    shared_provenance = _provenance(
        None,
        source_name=source_name,
        engine=shared_engine,
        symbol=symbol if len(distinct_symbols) == 1 else ", ".join(distinct_symbols),
        timeframe=",".join(sorted({str(job.get("timeframe", "1D")) for job in jobs})),
        from_date=from_date,
        to_date=to_date,
    )
    shared_provenance["engines_used"] = sorted(slot_engines)
    shared_provenance["symbols_used"] = distinct_symbols
    # NOT "mode": that key is the ENGINE mode (fill-exact / quick_screen) and
    # is read by the provenance badge. Naming the comparison mode "mode" would
    # silently relabel every run as a strategy comparison.
    shared_provenance["comparison_mode"] = (
        "generalization" if len(distinct_symbols) > 1 else "strategies"
    )
    _provenance_log(shared_provenance, "run-many")

    comparison = _comparison_block(
        results, jobs, current_app.config["BACKTEST_SOURCE"]
    )
    payload = {"results": results, "provenance": shared_provenance, "comparison": comparison}
    payload.update(_persist_compare(data, jobs, results, shared_provenance, comparison))
    return (jsonify(payload), 200)


# ---------------------------------------------------------------------------
# Process-pool worker (ticket P2.3)
# ---------------------------------------------------------------------------


def run_single_backtest(params: dict) -> dict:
    """Run ONE backtest slot — the top-level, picklable worker for the pool.

    Takes plain data only (strings/numbers/dicts — no source objects, no
    closures, no lambdas) and returns the plain-JSON slot payload, or
    ``{"error": ...}`` — one bad job must never poison the others. The
    worker rebuilds its source by name, so no unpicklable state crosses the
    process boundary. (Worker log lines drop the request id: contextvars do
    not cross processes; the endpoint's own log lines carry it.)
    """
    # Plugin strategies (U6.3) register in the WEB process at startup; pool
    # workers are fresh processes whose registries are empty, so "unknown
    # strategy: atm_instant_buy" (2026-09-24) hit every plugin slot run via
    # /compare. Discovery is idempotent + mtime-cached, and a missing
    # plugins dir just returns [] — never a worker crash.
    try:
        from backtest.plugins import discover_plugins

        discover_plugins()
    except Exception:  # noqa: BLE001 — plugin failure must not kill the slot
        log.warning("[slot %s] plugin discovery failed in worker", params.get("id"))
    sid = params.get("id")
    strategy = params.get("strategy")
    symbol = str(params.get("symbol", "DEMO"))
    from_date = str(params["from_date"])
    to_date = str(params["to_date"])
    capital = float(params.get("capital", 100_000))
    try:
        resolution = _resolve_strategy(strategy)
        if isinstance(resolution, str):
            raise ValueError(resolution)
        _option_refusal = _refuse_option_in_backtest(resolution, f"slot {sid}")
        if _option_refusal:
            raise ValueError(_option_refusal)
        slot_params = params.get("params") or {}
        timeframe = params.get("timeframe", "1D")
        interval = resolve_interval(timeframe)
        mode = str(params.get("mode", "")).strip().lower()
        _check_params(resolution, slot_params, f"slot {sid}")

        source = build_source(str(params["source_name"]))
        candles_full = source.get_candles(symbol, str(params["warmup_start"]), to_date, interval)
        log.debug(
            "[slot %s] %s: %d bars @ %s (%s)",
            sid,
            strategy,
            len(candles_full),
            interval,
            mode or "driver",
        )

        if mode == QUICK_SCREEN:
            result = run_quick_screen(
                candles_full,
                strategy,
                slot_params,
                symbol,
                capital,
                from_date,
                to_date,
                timeframe,
            )
            engine = "quick_screen"
        else:
            result = _run_driver(
                candles_full, strategy, slot_params, symbol, capital, timeframe=timeframe
            )
            engine = "backtest_driver"

        payload = BacktestAdapter(result).to_all()
        payload["cost_shock"] = _cost_shock(
            candles_full, strategy, slot_params, symbol, capital, timeframe, engine, result.metrics
        )
        payload["config"].update(
            {
                "timeframe": timeframe,
                "from_date": from_date,
                "to_date": to_date,
                "engine": engine,
                # Worker-process pid: proves the slot ran in its own process
                # (P2.3) and is handy for cross-referencing worker logs.
                "worker_pid": os.getpid(),
            }
        )
        payload["provenance"] = _provenance(
            candles_full,
            source_name=str(params["source_name"]),
            engine=engine,
            symbol=symbol,
            timeframe=timeframe,
            from_date=from_date,
            to_date=to_date,
            source_obj=source,
        )
        _provenance_log(payload["provenance"], f"slot {sid}")
        # §5: readiness is per-strategy, so each slot carries its own. It is
        # computed in the worker so the web process does no extra engine work.
        payload["readiness"] = build_readiness(payload)
        # NOTE: the [result]/[slot ...] INFO lines are emitted by the ENDPOINT
        # (web process) after the pool returns — worker-process log records
        # do not surface in the web process's log capture.
        return payload
    except Exception as exc:  # noqa: BLE001 — one bad job must not poison others
        traceback_text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        log.debug("[slot %s] failed: %s", sid, exc)  # child-side only
        return {"error": f"{exc.__class__.__name__}: {exc}", "traceback": traceback_text}


# ---------------------------------------------------------------------------
# Run ledger reads (PRD R4) — history list, health stats, full-run detail
# ---------------------------------------------------------------------------


def _ledger_or_503() -> tuple[object, int] | None:
    """``(json, status)`` error response when the ledger is offline, else None."""
    if _ledger() is not None:
        return None
    return jsonify({"error": "run ledger database unavailable"}), 503


@backtest_bp.get("/api/backtest/runs")
def list_runs_endpoint() -> tuple:
    """Server-backed backtest history (replaces the localStorage list, R4)."""
    err = _ledger_or_503()
    if err:
        return err
    args = request.args
    try:
        limit = max(1, min(200, int(args.get("limit", 50))))
        offset = max(0, int(args.get("offset", 0)))
    except ValueError:
        return jsonify({"error": "limit and offset must be integers"}), 400
    min_sharpe = args.get("min_sharpe")
    if min_sharpe is not None:
        try:
            min_sharpe = float(min_sharpe)
        except ValueError:
            return jsonify({"error": "min_sharpe must be a number"}), 400
    try:
        body = _ledger().list_runs(
            strategy=args.get("strategy"),
            symbol=args.get("symbol"),
            timeframe=args.get("timeframe"),
            kind=args.get("kind"),
            min_sharpe=min_sharpe,
            limit=limit,
            offset=offset,
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("[runs] list failed: %s", exc)
        return jsonify({"error": f"ledger read failed: {exc}"}), 500
    return jsonify(body), 200


#: Registered before ``/runs/<run_id>`` so the static segment wins.
@backtest_bp.get("/api/backtest/runs/stats")
def run_stats_endpoint() -> tuple:
    """Ledger health (PRD R8): sizes, counts, last persist error."""
    err = _ledger_or_503()
    if err:
        return err
    try:
        return jsonify(_ledger().stats()), 200
    except Exception as exc:  # noqa: BLE001
        log.exception("[runs] stats failed: %s", exc)
        return jsonify({"error": f"ledger read failed: {exc}"}), 500


@backtest_bp.get("/api/backtest/runs/<run_id>")
def get_run_endpoint(run_id: str) -> tuple:
    """Full run reconstruction — never re-runs anything (R3 read rule)."""
    err = _ledger_or_503()
    if err:
        return err
    try:
        record = _ledger().get_run(run_id)
    except PayloadVersionMismatch as exc:
        # PRD §5.3: a stored payload this build cannot render is refused, never
        # re-drawn through today's adapter.
        return jsonify({"error": str(exc), "code": "payload_version_mismatch"}), 422
    except Exception as exc:  # noqa: BLE001
        log.exception("[runs] detail %s failed: %s", run_id, exc)
        return jsonify({"error": f"ledger read failed: {exc}"}), 500
    if record is None:
        return jsonify({"error": f"run {run_id} not found"}), 404
    return jsonify(record), 200


@backtest_bp.get("/api/backtest/compares")
def list_compares_endpoint() -> tuple:
    """Stored comparisons, newest first (R4 — compare page reads history)."""
    err = _ledger_or_503()
    if err:
        return err
    try:
        limit = max(1, min(200, int(request.args.get("limit", 50))))
        offset = max(0, int(request.args.get("offset", 0)))
    except ValueError:
        return jsonify({"error": "limit and offset must be integers"}), 400
    try:
        body = _ledger().list_compares(limit=limit, offset=offset)
    except Exception as exc:  # noqa: BLE001
        log.exception("[compares] list failed: %s", exc)
        return jsonify({"error": f"ledger read failed: {exc}"}), 500
    return jsonify(body), 200


@backtest_bp.get("/api/backtest/compares/<compare_id>")
def get_compare_endpoint(compare_id: str) -> tuple:
    """Reconstructed comparison — read-back only, never re-runs (R3)."""
    err = _ledger_or_503()
    if err:
        return err
    try:
        record = _ledger().get_compare(compare_id)
    except PayloadVersionMismatch as exc:
        # comparison_version guards the stored block shape exactly like
        # payload_version guards a single run's.
        return jsonify({"error": str(exc), "code": "comparison_version_mismatch"}), 422
    except Exception as exc:  # noqa: BLE001
        log.exception("[compares] detail %s failed: %s", compare_id, exc)
        return jsonify({"error": f"ledger read failed: {exc}"}), 500
    if record is None:
        return jsonify({"error": f"comparison {compare_id} not found"}), 404
    return jsonify(record), 200
