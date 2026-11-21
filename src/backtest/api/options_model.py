"""Options model backtest API (plan V3 Phase 3).

A quarantined sandbox: an equity strategy's signal is expressed as an option
structure priced by :class:`ModelBSFeed` (Black-Scholes off the underlying
candles). The result is tagged ``pricing_feed="model_bs"``, carries
``certifiable=False`` at the source, and is refused by every certify/deploy
door. It is a structure/strike/exit comparison tool, never a measurement of
historical option P&L.

Three orthogonal axes (plan §9.5): ``source`` (deployment candle config),
``mode`` (this endpoint is the options engine), ``pricing_feed`` (request
field): ``model_bs`` is the quarantined Black-Scholes sandbox; ``chain_history``
(Phase 4) replays premiums captured into ``option_chain_history`` and is
certifiable subject to the readiness checks. A chain_history run with no stored
coverage is refused with the stored range — it never falls back to the model.

Kill switch: ``OPTIONS_MODEL_BACKTEST_ENABLED`` (default true). When false the
route returns 404 and the UI hides the toggle (surfaced via ``/api/config``).
"""

from __future__ import annotations

import dataclasses
import os
from typing import Any

from flask import Blueprint, current_app, jsonify, request

from backtest.adapters.options_backtest_adapter import OptionsBacktestAdapter
from backtest.api.backtest import _candles, _persist_run, _provenance, _provenance_log
from backtest.api.data_guard import guard_source
from backtest.data.base import periods_per_year
from backtest.db.manager import DatabaseManager
from backtest.engine.backtest_runner import resolve_interval
from backtest.engine.option_backtest_driver import BacktestConfig, OptionBacktestDriver
from backtest.engine.readiness import build_readiness
from backtest.logging_config import get_logger
from backtest.options.chain_history import chain_coverage, load_chain_history
from backtest.options.chain_history_feed import ChainHistoryFeed, InMemoryChainReader
from backtest.options.execution_gate import RESEARCH_STRUCTURES
from backtest.options.model_pricing import (
    IV_WINDOWS,
    ModelBSFeed,
    ModelPricingConfig,
    compute_realized_vol,
)
from backtest.options.paper_trading import OptionPaperBroker
from backtest.options.signal_view import SignalViewStrategy
from backtest.simulator.fees import CommissionCalculator
from backtest.strategy.registry import get_strategy, signal_kind

options_model_bp = Blueprint("options_model_api", __name__)
log = get_logger(__name__)

#: Feed configuration keys a client may set. Anything else is rejected so a
#: typo cannot silently ride into the model as a default.
_MODEL_PARAM_KEYS = {
    "iv_source", "iv_constant", "iv_premium", "skew_pts",
    "spread_pct", "spread_floor", "risk_free",
}

_SENSITIVITY_PREMIUMS = (0.0, 0.02, 0.04)


def model_backtest_enabled() -> bool:
    """The emergency off-switch (plan §5). Default on; false hides everything."""
    raw = str(os.environ.get("OPTIONS_MODEL_BACKTEST_ENABLED", "true")).strip().lower()
    return raw not in ("false", "0", "no", "off")


def _build_model_config(model_params: dict) -> ModelPricingConfig:
    unknown = set(model_params) - _MODEL_PARAM_KEYS
    if unknown:
        raise ValueError(f"unknown model_params keys: {sorted(unknown)}")
    return ModelPricingConfig(**model_params)


def _iv_bars(candles, model_cfg: ModelPricingConfig, timeframe: str):
    window = IV_WINDOWS.get(model_cfg.iv_source)
    if window is None:  # constant IV — no realized series needed
        return None
    return compute_realized_vol(
        candles["close"], window=window,
        periods_per_year=float(periods_per_year(timeframe)),
    )


def _run_once(
    candles, *, feed, strategy_cls, params, symbol, capital, timeframe,
    exits, confidence, selector_type, selector_kwargs, max_open,
):
    instance = strategy_cls(**params)
    view_strategy = SignalViewStrategy(
        instance, candles, confidence=confidence, underlying=symbol
    )
    # Full Indian fee stack, booked once: brokerage rides inside the
    # calculator's statutory total (commission_per_lot=0), so it is not
    # double-counted the way the forward path does. The driver itself stays
    # fee-agnostic; this policy lives at the API boundary.
    broker = OptionPaperBroker(
        capital=capital,
        slippage_pct=0.001,
        commission_per_lot=0.0,
        fee_calculator=CommissionCalculator.for_broker("mstock"),
        allowed_structures=RESEARCH_STRUCTURES,
    )
    cfg = BacktestConfig(
        capital=capital,
        selector_type=selector_type,
        selector_kwargs=selector_kwargs,
        stop_loss_pct=exits.get("stop_loss_pct"),
        take_profit_pct=exits.get("take_profit_pct"),
        exit_on_signal_flip=bool(exits.get("exit_on_signal_flip", False)),
        max_open_structures=max_open,
    )
    driver = OptionBacktestDriver(
        candles, config=cfg, underlying=symbol,
        strategy=view_strategy, broker=broker, pricing_feed=feed,
    )
    return driver.run()


def _vol_regime(candles, model_cfg: ModelPricingConfig, timeframe: str) -> dict[str, Any]:
    if model_cfg.iv_source == "constant":
        base = model_cfg.iv_constant
    else:
        series = _iv_bars(candles, model_cfg, timeframe).dropna()
        base = float(series.mean()) if len(series) else model_cfg.iv_constant
    avg_iv = base + model_cfg.iv_premium
    if avg_iv < 0.15:
        regime, clause = "low", "Theta effects amplified; vega effects muted."
    elif avg_iv <= 0.25:
        regime, clause = "normal", "Theta and vega effects are balanced."
    else:
        regime, clause = "high", "Vega effects amplified; theta rich."
    return {
        "avg_iv_pct": round(avg_iv * 100.0, 2),
        "regime": regime,
        "note": f"Avg IV during backtest: {avg_iv * 100.0:.1f}% ({regime}-vol regime). {clause}",
    }


def _chain_vol_regime(rows: list[dict]) -> dict[str, Any]:
    """Vol regime from the recorded chain's own IV (no realized+premium guess)."""
    ivs = [float(r["iv"]) for r in rows if r.get("iv") is not None and float(r["iv"]) > 0]
    if not ivs:
        return {
            "avg_iv_pct": None,
            "regime": "unknown",
            "note": (
                "Recorded chain carries no IV; vol regime unavailable "
                "(premiums are real, IV was not published)."
            ),
        }
    avg_iv = sum(ivs) / len(ivs)
    if avg_iv < 0.15:
        regime, clause = "low", "Theta effects amplified; vega effects muted."
    elif avg_iv <= 0.25:
        regime, clause = "normal", "Theta and vega effects are balanced."
    else:
        regime, clause = "high", "Vega effects amplified; theta rich."
    return {
        "avg_iv_pct": round(avg_iv * 100.0, 2),
        "regime": regime,
        "note": (
            f"Avg recorded IV during backtest: {avg_iv * 100.0:.1f}% "
            f"({regime}-vol regime). {clause}"
        ),
    }


@options_model_bp.post("/api/backtest/options-model")
def run_options_model() -> tuple:
    if not model_backtest_enabled():
        return jsonify({"error": "not found"}), 404
    refused = guard_source()
    if refused:
        return refused
    data = request.get_json(silent=True) or {}

    pricing_feed = str(data.get("pricing_feed", "model_bs")).strip().lower()
    if pricing_feed not in ("model_bs", "chain_history"):
        return jsonify({
            "error": f"pricing_feed '{pricing_feed}' is not supported; "
                     "use 'model_bs' (Black-Scholes sandbox) "
                     "or 'chain_history' (recorded premiums)."
        }), 400
    if pricing_feed == "chain_history" and (data.get("model_params") or {}):
        return jsonify({
            "error": "model_params apply only to 'model_bs'; chain_history replays recorded "
                     "premiums and IV, so iv_premium/skew/spread knobs have no meaning here."
        }), 400

    strategy = data.get("strategy")
    if not strategy:
        return jsonify({"error": "strategy is required"}), 400
    try:
        strategy_cls = get_strategy(strategy)
    except KeyError as exc:
        return jsonify({"error": str(exc)}), 400
    if signal_kind(strategy_cls) == "option":
        return jsonify({
            "error": f"'{strategy}' is an options strategy; the model sandbox expresses "
                     "an EQUITY strategy's signal as an option structure. Pick an equity strategy."
        }), 400

    symbol = str(data.get("symbol") or "NIFTY").upper()
    from_date = data.get("from_date") or data.get("from")
    to_date = data.get("to_date") or data.get("to")
    if not from_date or not to_date:
        return jsonify({"error": "from_date and to_date are required"}), 400
    if from_date > to_date:
        return jsonify({"error": "from_date must be <= to_date"}), 400
    try:
        capital = float(data.get("capital", 1_000_000))
        confidence = float(data.get("confidence", 0.8))
    except (TypeError, ValueError):
        return jsonify({"error": "capital and confidence must be numbers"}), 400

    params = data.get("params") or {}
    timeframe = data.get("timeframe", "1D")
    interval = resolve_interval(timeframe)
    exits = data.get("exits") or {}
    selector_type = str(data.get("selector_type", "atm"))
    selector_kwargs = data.get("selector_kwargs") or {}
    max_open = data.get("max_open_structures")

    model_cfg = None
    if pricing_feed == "model_bs":
        try:
            model_cfg = _build_model_config(data.get("model_params") or {})
        except (ValueError, TypeError) as exc:
            return jsonify({"error": f"model_params: {exc}"}), 400

    log.info(
        "[options-model] strategy=%s symbol=%s timeframe=%s range=%s..%s capital=%s feed=%s",
        strategy, symbol, timeframe, from_date, to_date, capital, pricing_feed,
    )

    try:
        source, candles = _candles(symbol, from_date, to_date, interval)
    except Exception as exc:  # noqa: BLE001 — stored-range message rides through
        log.warning("[options-model] data error for %s: %s", symbol, exc)
        return jsonify({"error": f"data error: {exc}"}), 400
    if candles.empty:
        return jsonify({"error": f"data error: no candles for {symbol} in range"}), 400

    chain_rows: list[dict] = []
    if pricing_feed == "model_bs":
        feed = ModelBSFeed(model_cfg, iv_bars=_iv_bars(candles, model_cfg, timeframe))
    else:
        # chain_history replays captured premiums only — never falls back to the
        # model. No coverage in the range is a refusal with the stored range,
        # not a silent switch to Black-Scholes.
        try:
            manager = DatabaseManager.from_env()
            chain_rows = load_chain_history(manager, symbol, from_date, to_date)
            coverage = chain_coverage(manager, symbol)
        except Exception as exc:  # noqa: BLE001 — surface the DB fault, don't fake a feed
            log.warning("[options-model] chain history load failed for %s: %s", symbol, exc)
            return jsonify({"error": f"chain history unavailable: {exc}"}), 503
        if not chain_rows:
            if coverage["count"] == 0:
                detail = (
                    f"no recorded option chain for {symbol} at all. Capture chains first "
                    "(chain snapshot recorder / ENABLE_FEED depth tape) before replaying."
                )
            else:
                detail = (
                    f"no recorded option chain for {symbol} in {from_date}..{to_date}. "
                    f"Stored coverage: {coverage['from']}..{coverage['to']} "
                    f"({coverage['count']} rows). "
                    "Widen the range to the stored sessions or capture the missing days."
                )
            return jsonify(
                {"error": detail, "coverage": coverage, "pricing_feed": "chain_history"}
            ), 400
        feed = ChainHistoryFeed(InMemoryChainReader(chain_rows))

    run_kwargs = dict(
        feed=feed, strategy_cls=strategy_cls, params=params, symbol=symbol, capital=capital,
        timeframe=timeframe, exits=exits, confidence=confidence,
        selector_type=selector_type, selector_kwargs=selector_kwargs, max_open=max_open,
    )
    try:
        result = _run_once(candles, **run_kwargs)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:  # noqa: BLE001
        log.exception("[options-model] %s crashed on %s", strategy, symbol)
        return jsonify({"error": f"options model backtest failed: {exc}"}), 500

    config_info = {
        "strategy": strategy, "symbol": symbol, "capital": capital,
        "timeframe": timeframe, "from_date": from_date, "to_date": to_date,
        "strategy_params": params,
        "pricing_feed": pricing_feed,
        "model_params": dataclasses.asdict(model_cfg) if model_cfg is not None else {},
        "exits": exits,
    }
    payload = OptionsBacktestAdapter(result, config_info=config_info).to_all()

    if pricing_feed == "model_bs":
        # IV sensitivity (plan §3 correction 2): the same bars at 0/2/4 vol pts
        # of premium, so a single-IV conclusion cannot masquerade as robust.
        sensitivity = []
        for premium in _SENSITIVITY_PREMIUMS:
            cfg = dataclasses.replace(model_cfg, iv_premium=premium)
            sens_feed = ModelBSFeed(cfg, iv_bars=_iv_bars(candles, cfg, timeframe))
            try:
                r = _run_once(candles, **{**run_kwargs, "feed": sens_feed})
                sensitivity.append({
                    "iv_premium_pts": round(premium * 100.0, 1),
                    "net_realized_pnl": r.metrics.get("net_realized_pnl"),
                    "return_on_premium": r.metrics.get("return_on_premium"),
                    "total_trades": r.metrics.get("total_trades"),
                    "final_equity": r.metrics.get("final_equity"),
                })
            except Exception as exc:  # noqa: BLE001 — sensitivity is best-effort
                log.warning("[options-model] sensitivity iv_premium=%s failed: %s", premium, exc)
                sensitivity.append({"iv_premium_pts": round(premium * 100.0, 1), "error": str(exc)})
        payload["iv_sensitivity"] = sensitivity
        payload["vol_regime"] = _vol_regime(candles, model_cfg, timeframe)
    else:
        # Recorded IV is a market observation, not a knob — there is no premium
        # to vary, so the model-only sensitivity ladder does not apply.
        payload["iv_sensitivity"] = []
        payload["vol_regime"] = _chain_vol_regime(chain_rows)

    payload["provenance"] = _provenance(
        candles, source_name=current_app.config["BACKTEST_SOURCE"], engine="options",
        symbol=symbol, timeframe=timeframe, from_date=from_date, to_date=to_date,
        source_obj=source,
    )
    _provenance_log(payload["provenance"], f"options-model/{strategy}")
    payload["readiness"] = build_readiness(payload)
    payload.update(
        _persist_run(
            payload, strategy_cls=strategy_cls, where=f"options-model/{strategy}"
        )
    )
    return jsonify(payload), 200
