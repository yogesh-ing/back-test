"""Options trading UI & API — positions, structures, Greeks, expiry alerts.

Phase 7 of the options PRD, extended by the Gap-Analysis remediation:

* **G2.2** — the paper broker's quote provider is chosen at first use:
  ``LiveQuoteProvider`` (real mStock LTP, TTL-cached) when an authenticated
  session exists, else :class:`SyntheticQuoteProvider` (Black-Scholes off a
  simulated spot — every strike/expiry prices differently, MTM responds to
  ``set_spot``). The hardcoded ``FakeQuoteProvider`` default is gone.
* **G1.1** — ``POST /api/options/trade`` is the trade-execution driver:
  view → chain → selector → structure → intent → pre-trade risk check →
  paper broker. This was the missing piece that made the expression layer
  test-only.
* **G2.3** — every summary response carries ``quote_source`` so the
  dashboard can badge honest numbers ("synthetic:bs" vs "live:mstock").

The dashboard renders whatever the in-process
:class:`~backtest.options.paper_trading.OptionPaperBroker` holds — V1 is
paper-only, so no live credentials are touched here even when real quotes
flow in.

Usage::

    # in app.py
    from backtest.web.options_api import register_options_routes
    register_options_routes(app)
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from flask import Flask, jsonify, request

from backtest.options.paper_trading import OptionPaperBroker
from backtest.options.persistence import StructurePersistence
from backtest.options.portfolio_greeks import PortfolioGreeksCalculator
from backtest.options.expiry import ExpiryManager
from backtest.options.quote_providers import (
    CachedQuoteProvider,
    LiveQuoteProvider,
    SyntheticQuoteProvider,
)
from backtest.simulator.fees import CommissionCalculator

logger = logging.getLogger("backtest.web.options")

# ---------------------------------------------------------------------------
# Singletons for the UI (V1: one paper book + one quote feed per process)
# ---------------------------------------------------------------------------

_broker: OptionPaperBroker | None = None
_expiry_manager: ExpiryManager | None = None
_quote_provider: Any | None = None
_persistence: StructurePersistence | None = None


def _build_persistence() -> StructurePersistence | None:
    """Best-effort DB persistence (Gap G4.2).

    ``OPTIONS_PERSISTENCE=off`` disables it entirely (tests); the default
    ``auto`` attaches the configured forward-testing database when it is
    reachable and degrades to the in-memory book otherwise. A missing DB
    must never take the dashboard down.
    """
    mode = os.getenv("OPTIONS_PERSISTENCE", "auto").strip().lower()
    if mode in ("0", "off", "false", "no"):
        return None
    try:
        from backtest.db import DatabaseManager

        manager = DatabaseManager.from_env()
        manager.connect()
        persistence = StructurePersistence(manager)
        persistence.ensure_schema()
        logger.info("[options] persistence attached (%s)", manager.config.safe_url)
        return persistence
    except Exception:  # noqa: BLE001 — persistence is best-effort
        logger.warning(
            "[options] persistence unavailable — positions will not survive restarts",
            exc_info=True,
        )
        return None


def get_option_broker() -> OptionPaperBroker:
    """Return the process-wide paper option broker (create on first use).

    The dashboard book carries the full statutory fee stack (Gap G4.1) and
    mirrors every open/close into the database when persistence is
    available; open structures are rehydrated on first use so a server
    restart does not lose positions (Gap G4.2).
    """
    global _broker, _persistence
    if _broker is None:
        persistence = _build_persistence()
        _persistence = persistence

        def _on_opened(structure: Any, entry_fees: Decimal) -> None:
            if persistence is not None:
                persistence.save_open(structure, entry_fees=entry_fees)

        def _on_closed(structure: Any, realized_pnl: Decimal) -> None:
            if persistence is not None:
                all_expired = all(
                    leg.status.value == "expired" for leg in structure.legs
                )
                persistence.mark_closed(
                    structure.structure_id,
                    realized_pnl,
                    closed_at=structure.closed_at,
                    status="expired" if all_expired else "closed",
                )

        broker = OptionPaperBroker(
            capital=1_000_000.0,
            fee_calculator=CommissionCalculator.for_broker("mstock"),
            on_structure_opened=_on_opened,
            on_structure_closed=_on_closed,
        )

        if persistence is not None:
            try:
                restored = []
                for structure, entry_fees in persistence.load_open():
                    broker.restore_structure(structure, fees_paid=entry_fees)
                    restored.append(structure)
                if restored:
                    _register_restored_contracts(get_quote_provider(), restored)
            except Exception:  # noqa: BLE001 — reload must not break startup
                logger.exception("[options] failed to reload open structures from DB")

        _broker = broker
    return _broker


def _register_restored_contracts(quotes: Any, structures: list[Any]) -> None:
    """Re-register rehydrated legs with a synthetic quote feed.

    The in-process contract registry dies with the server; without this the
    first MTM refresh after a restart would mark every restored leg at 0.
    Live feeds need no registration, so this is a no-op for them.
    """
    from backtest.instruments.base import OptionType
    from backtest.instruments.option import OptionContract

    target = getattr(quotes, "inner", quotes)
    register = getattr(target, "register_contract", None)
    if register is None:
        return
    for structure in structures:
        for leg in structure.legs:
            register(
                OptionContract(
                    instrument_token=leg.instrument_token,
                    trading_symbol=leg.trading_symbol,
                    underlying=structure.underlying,
                    expiry=structure.expiry,
                    strike=leg.strike,
                    option_type=(
                        OptionType.CE if leg.option_type == "CE" else OptionType.PE
                    ),
                    lot_size=leg.lot_size,
                    metadata={"synthetic": True},
                )
            )


def get_expiry_manager() -> ExpiryManager:
    """Return the process-wide expiry manager."""
    global _expiry_manager
    if _expiry_manager is None:
        _expiry_manager = ExpiryManager(get_option_broker())
    return _expiry_manager


def get_quote_provider() -> Any:
    """Pick the quote feed once (G2.2): live mStock when authenticated.

    Falls back to the synthetic Black-Scholes provider with no credentials
    and no network. A per-token TTL cache (``CachedQuoteProvider``) guards
    the live feed from dashboard-polling spam.
    """
    global _quote_provider
    if _quote_provider is not None:
        return _quote_provider

    live = None
    try:
        from backtest.brokers.session_manager import get_session_manager

        manager = get_session_manager()
        if manager.is_authenticated():
            live = LiveQuoteProvider(manager.get_active_broker(), cache_ttl_seconds=5)
            logger.info("[options] using live mStock quotes (TTL cache 5s)")
    except Exception:  # noqa: BLE001 — quotes must never crash the app
        logger.info("[options] no live broker session available for quotes", exc_info=True)

    if live is not None:
        _quote_provider = live
    else:
        synthetic = SyntheticQuoteProvider()
        # Register a default NIFTY chain so MTM works even before the first
        # trade-driven chain registration.
        synthetic.register_chain(synthetic.generator.generate_chain("NIFTY"))
        _quote_provider = CachedQuoteProvider(synthetic, ttl=2)
        logger.info("[options] using synthetic Black-Scholes quotes (no live session)")
    return _quote_provider


def reset_option_state() -> None:
    """Reset the singleton broker/manager/quotes/persistence (used by tests)."""
    global _broker, _expiry_manager, _quote_provider, _persistence
    _broker = None
    _expiry_manager = None
    _quote_provider = None
    _persistence = None


# ---------------------------------------------------------------------------
# Trade driver (Gap G1.1) — the expression layer's missing entry point
# ---------------------------------------------------------------------------

#: structure_type → (OptionStructure, option_type side of the chain)
_STRUCTURE_REGISTRY: dict[str, tuple[Any, str]] = {}


def _structure_registry() -> dict[str, tuple[Any, str]]:
    """Lazy registry so importing this module stays cheap."""
    global _STRUCTURE_REGISTRY
    if not _STRUCTURE_REGISTRY:
        from backtest.options.structures import (
            BearPutSpread,
            BullCallSpread,
            LongCall,
            LongPut,
        )

        _STRUCTURE_REGISTRY = {
            "long_call": (LongCall(), "CE"),
            "long_put": (LongPut(), "PE"),
            "bull_call_spread": (BullCallSpread(), "CE"),
            "bear_put_spread": (BearPutSpread(), "PE"),
        }
    return _STRUCTURE_REGISTRY


def _parse_decimal(value: Any, field: str) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(f"invalid number for {field!r}: {value!r}")


def _resolve_strikes(
    structure_name: str,
    underlying: str,
    chain: dict[Decimal, Any],
    spot: Decimal,
    config: dict[str, Any],
) -> list[Decimal]:
    """Pick strike(s) from the chain using the requested selector."""
    from backtest.options.selector import ATMSelector, DeltaSelector, create_selector

    strikes_sorted = sorted(chain.keys())
    selector_type = str(config.get("strike_selection", "atm")).lower()
    if selector_type in ("atm", ""):
        selector: Any = ATMSelector()
    elif selector_type == "delta":
        target = float(config.get("delta_target", 0.35))
        selector = DeltaSelector(delta_target=target)
    else:
        selector = create_selector(selector_type)

    from backtest.strategy.intent import Direction

    direction = Direction.BULLISH
    if structure_name in ("long_put", "bear_put_spread"):
        direction = Direction.BEARISH

    if structure_name in ("long_call", "long_put"):
        picked = selector.pick_strike(spot, strikes_sorted, direction)
        return [picked] if picked is not None else []
    picked = selector.pick_strikes(spot, strikes_sorted, direction, count=2)
    return picked


def _open_trade_payload() -> tuple[dict[str, Any] | None, str | None]:
    """Validate and normalise the POST body. ``(payload, error)``."""
    data = request.get_json(silent=True) or {}

    underlying = str(data.get("underlying", "NIFTY")).upper()
    structure_type = str(data.get("structure_type", "")).lower()
    if structure_type not in _structure_registry():
        return None, (
            f"unknown structure_type {structure_type!r}. "
            f"Valid: {sorted(_structure_registry())}"
        )

    quantity = data.get("quantity", 1)
    try:
        quantity = int(quantity)
    except (TypeError, ValueError):
        return None, f"quantity must be an integer, got {quantity!r}"
    if not 1 <= quantity <= 100:
        return None, "quantity must be between 1 and 100"

    config = data.get("config") or {}
    if not isinstance(config, dict):
        return None, "config must be a JSON object"
    payload = {
        "underlying": underlying,
        "structure_type": structure_type,
        "quantity": quantity,
        "config": config,
    }
    return payload, None


def _execute_trade(payload: dict[str, Any]) -> dict[str, Any]:
    """The G1.1 flow: view → chain → strikes → intent → risk → broker."""
    from backtest.strategy.intent import Direction, MarketView
    from backtest.options.expiry_policy import NearestExpiryPolicy
    from backtest.options.margin import MarginCalculator, PreTradeRiskCheck

    structure, option_type = _structure_registry()[payload["structure_type"]]
    underlying = payload["underlying"]
    broker = get_option_broker()
    quotes = get_quote_provider()

    spot = broker_spot(quotes, underlying)

    # 1. MarketView — the payload's direction is implied by the structure
    #    (long_call/bull = bullish; long_put/bear = bearish).
    direction = (
        Direction.BULLISH
        if payload["structure_type"] in ("long_call", "bull_call_spread")
        else Direction.BEARISH
    )
    view = MarketView(
        direction=direction,
        confidence=float(payload["config"].get("confidence", 0.8)),
        underlying=underlying,
        spot_price=spot,
    )

    # 2. Option chain for the near monthly expiry (synthetic generator, or
    #    a live chain when one exists — V1 ships the generator).
    generator = getattr(quotes, "generator", None) or getattr(
        getattr(quotes, "inner", None), "generator", None
    )
    if generator is None:
        from backtest.options.quote_providers import SyntheticChainGenerator

        generator = SyntheticChainGenerator()
    expiry = NearestExpiryPolicy().select_expiry(generator.available_expiries(underlying))
    if expiry is None:
        raise ValueError(f"no available expiry for {underlying}")
    chain = generator.generate_chain(underlying, expiry=expiry, option_type=option_type)
    if hasattr(quotes, "register_chain"):
        quotes.register_chain(chain)
    elif hasattr(quotes, "inner") and hasattr(quotes.inner, "register_chain"):
        quotes.inner.register_chain(chain)

    # 3. Strike selection → TradeIntent (multiplied by requested quantity).
    strikes = _resolve_strikes(
        payload["structure_type"], underlying, chain, spot, payload["config"]
    )
    if not strikes:
        raise ValueError("no suitable strike found in the chain")
    intent = structure.build(view, strikes, chain, expiry, strategy_name="dashboard_manual")
    if payload["quantity"] > 1:
        intent = _scale_intent(intent, payload["quantity"])

    # 4. Pre-trade risk check (position count + loss cap + notional caps).
    #    For V1's four debit structures the true max loss is the net premium
    #    paid, so the loss cap is checked against the premium margin — the
    #    full short-notional ``net_margin`` is a real-world margin figure,
    #    not a loss proxy. The broker's own InsufficientMarginError remains
    #    the hard cash guard at execution.
    premiums = {leg.trading_symbol: _leg_quote_price(quotes, leg) for leg in intent.legs}
    margin_result = MarginCalculator().calculate_structure_margin(
        intent, premiums, float(spot), lot_size=intent.legs[0].lot_size
    )
    risk = PreTradeRiskCheck(capital=float(broker.capital)).check(
        margin_required=margin_result.premium_margin,
        current_margin_used=float(broker.total_margin_used),
        current_positions=len(broker.get_open_positions()),
        trade_notional=sum(
            float(premiums.get(leg.trading_symbol, 0.0)) * leg.total_quantity
            for leg in intent.legs
        ),
    )
    if not risk.allowed:
        return {"rejected": True, "reason": risk.reason}

    # 5. Execute on the paper book (atomic multi-leg).
    positions = broker.execute_structure(intent, quotes)
    return {
        "structure_id": positions[0].structure_id if positions else None,
        "positions": [_position_to_dict(p) for p in positions],
        "strikes": [str(s) for s in strikes],
        "expiry": str(expiry),
        "quote_source": quote_source_name(quotes),
    }


def _scale_intent(intent: Any, quantity: int) -> Any:
    """Rebuild the intent with ``quantity`` lots per leg."""
    from backtest.strategy.intent import OptionLeg, TradeIntent

    legs = tuple(
        OptionLeg(
            instrument_token=leg.instrument_token,
            trading_symbol=leg.trading_symbol,
            side=leg.side,
            quantity=leg.quantity * quantity,
            lot_size=leg.lot_size,
        )
        for leg in intent.legs
    )
    return TradeIntent(
        view=intent.view,
        structure_type=intent.structure_type,
        legs=legs,
        expiry=intent.expiry,
        strategy_name=intent.strategy_name,
        metadata=intent.metadata,
    )


def _leg_quote_price(quotes: Any, leg: Any) -> float:
    try:
        return float(quotes.get_quote(leg.instrument_token).get("ltp", 0.0))
    except Exception:  # noqa: BLE001
        return 0.0


def broker_spot(quotes: Any, underlying: str) -> Decimal:
    """Current underlying spot from the provider's generator (synthetic)."""
    generator = getattr(quotes, "generator", None) or getattr(
        getattr(quotes, "inner", None), "generator", None
    )
    if generator is not None and hasattr(generator, "get_spot"):
        return Decimal(str(generator.get_spot(underlying)))
    return Decimal("0")


def quote_source_name(quotes: Any) -> str:
    """G2.3 — human-readable quote-source tag for the dashboard badge."""
    return getattr(quotes, "source_name", "unknown")


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def register_options_routes(app: Flask) -> None:
    """Attach the options JSON API to the Flask app.

    2026-09-22 — GAP-3 resolved: the manual `/options` page was REMOVED
    (owner decision). This module keeps only what other systems still use:
    the book singleton (portfolio merge + emergency flatten) and the JSON
    endpoints exercised by tests. The manual trade UI lives in Portfolio →
    Playbooks via runner instances.
    """

    # ------------------------------------------------------------------
    # API — summary
    # ------------------------------------------------------------------
    @app.get("/api/options/summary")
    def options_summary() -> tuple:
        """One call the dashboard polls: positions, structures, Greeks, alerts."""
        broker = get_option_broker()
        manager = get_expiry_manager()
        quotes = get_quote_provider()

        open_positions = broker.get_open_positions()
        structures = broker.get_open_structures()

        # Refresh MTM from the live/synthetic feed on every poll — without
        # this the dashboard's unrealized P&L froze at entry values.
        try:
            broker.update_mtm(quotes)
        except Exception:  # noqa: BLE001 — a quote failure must not kill the page
            logger.warning("[options] MTM refresh failed", exc_info=True)

        greeks_calc = PortfolioGreeksCalculator()
        portfolio_greeks = greeks_calc.calculate(
            open_positions,
            spot_prices=request.args.get("spot", type=dict) or None,
        )

        return (
            jsonify(
                {
                    "capital": float(broker.capital),
                    "available_cash": float(broker.available_cash),
                    "total_equity": float(broker.total_equity),
                    "realized_pnl": float(broker.total_realized_pnl),
                    "margin_used": float(broker.total_margin_used),
                    "statutory_fees": float(broker.total_statutory_fees_paid),
                    "total_costs": float(broker.total_costs_paid),
                    "open_position_count": len(open_positions),
                    "open_structure_count": len(structures),
                    "quote_source": quote_source_name(quotes),
                    "greeks": portfolio_greeks.to_dict(),
                    "positions": [_position_to_dict(p) for p in open_positions],
                    "structures": [_structure_to_dict(s) for s in structures],
                    "alerts": [a.to_dict() for a in manager.get_alerts()],
                }
            ),
            200,
        )

    # ------------------------------------------------------------------
    # API — open a trade (Gap G1.1: the expression layer's driver)
    # ------------------------------------------------------------------
    @app.post("/api/options/trade")
    def options_open_trade() -> tuple:
        payload, error = _open_trade_payload()
        if error or payload is None:
            return jsonify({"error": error}), 400
        try:
            result = _execute_trade(payload)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:  # noqa: BLE001 — report, don't 500 the UI
            logger.exception("[options] trade execution failed")
            return jsonify({"error": f"execution failed: {exc}"}), 500
        if result.get("rejected"):
            return jsonify({"error": result["reason"], "rejected": True}), 400
        return jsonify(result), 201

    # ------------------------------------------------------------------
    # API — move the synthetic market (demo/what-if knob, no-op on live)
    # ------------------------------------------------------------------
    @app.post("/api/options/spot")
    def options_set_spot() -> tuple:
        data = request.get_json(silent=True) or {}
        underlying = str(data.get("underlying", "NIFTY")).upper()
        try:
            spot = float(data["spot"])
        except (KeyError, TypeError, ValueError):
            return jsonify({"error": "spot (number) is required"}), 400
        quotes = get_quote_provider()
        target = getattr(quotes, "set_spot", None) or getattr(
            getattr(quotes, "inner", None), "set_spot", None
        )
        if target is None:
            return jsonify({"error": "quote feed does not support spot control"}), 400
        target(underlying, spot)
        # Drop cached quotes so the new spot is visible immediately —
        # otherwise the TTL would mask the move for up to ``ttl`` seconds.
        clearer = getattr(quotes, "clear", None) or getattr(
            getattr(quotes, "inner", None), "clear", None
        )
        if callable(clearer):
            clearer()
        return jsonify({"underlying": underlying, "spot": spot}), 200

    # ------------------------------------------------------------------
    # API — positions
    # ------------------------------------------------------------------
    @app.get("/api/options/positions")
    def options_positions() -> tuple:
        broker = get_option_broker()
        status_filter = request.args.get("status", "open")
        positions = (
            broker.get_open_positions()
            if status_filter == "open"
            else list(broker._positions.values())
        )
        return (
            jsonify({"positions": [_position_to_dict(p) for p in positions]}),
            200,
        )

    # ------------------------------------------------------------------
    # API — close a structure (now at REAL/synthetic LTP, not fake ₹100)
    # ------------------------------------------------------------------
    @app.post("/api/options/structures/<structure_id>/close")
    def close_structure(structure_id: str) -> tuple:
        broker = get_option_broker()
        quotes = get_quote_provider()

        try:
            pnl = broker.close_structure(structure_id, quotes)
        except ValueError as e:
            return jsonify({"error": str(e)}), 404

        return (
            jsonify(
                {
                    "structure_id": structure_id,
                    "realized_pnl": float(pnl),
                    "quote_source": quote_source_name(quotes),
                    "closed_at": datetime.utcnow().isoformat(),
                }
            ),
            200,
        )

    # ------------------------------------------------------------------
    # API — run expiry pipeline on demand
    # ------------------------------------------------------------------
    @app.post("/api/options/expiry/process")
    def process_expiry() -> tuple:
        from backtest.options.expiry import StaticSettlementProvider

        manager = get_expiry_manager()
        quotes = get_quote_provider()
        spot = float(broker_spot(quotes, "NIFTY") or 24800.0)
        settlement = StaticSettlementProvider({"NIFTY": spot})
        summary = manager.process_expiries(quotes, settlement)
        return jsonify(summary), 200

    # ------------------------------------------------------------------
    # API — Greeks detail
    # ------------------------------------------------------------------
    @app.get("/api/options/greeks")
    def options_greeks() -> tuple:
        broker = get_option_broker()
        calc = PortfolioGreeksCalculator()
        result = calc.calculate(broker.get_open_positions())
        return jsonify(result.to_dict()), 200


# ---------------------------------------------------------------------------
# Serializers
# ---------------------------------------------------------------------------

def _position_to_dict(p: Any) -> dict[str, Any]:
    """Serialize an OptionPosition for the UI."""
    return {
        "position_id": p.position_id,
        "structure_id": p.structure_id,
        "strategy_name": p.strategy_name,
        "trading_symbol": p.trading_symbol,
        "underlying": p.underlying,
        "option_type": p.option_type,
        "strike": str(p.strike),
        "expiry": str(p.expiry) if p.expiry else None,
        "side": p.side,
        "quantity": p.quantity,
        "lot_size": p.lot_size,
        "total_quantity": p.total_quantity,
        "entry_price": float(p.entry_price),
        "current_price": float(p.current_price),
        "unrealized_pnl": float(p.unrealized_pnl),
        "realized_pnl": float(p.realized_pnl),
        "status": p.status.value,
        "opened_at": p.opened_at.isoformat(),
    }


def _structure_to_dict(s: Any) -> dict[str, Any]:
    """Serialize a StructurePosition for the UI."""
    return {
        "structure_id": s.structure_id,
        "structure_type": s.structure_type,
        "strategy_name": s.strategy_name,
        "underlying": s.underlying,
        "expiry": str(s.expiry) if s.expiry else None,
        "leg_count": len(s.legs),
        "total_entry_cost": float(s.total_entry_cost),
        "total_unrealized_pnl": float(s.total_unrealized_pnl),
        "legs": [_position_to_dict(leg) for leg in s.legs],
        "opened_at": s.opened_at.isoformat(),
    }
