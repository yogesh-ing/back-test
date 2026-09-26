"""Turn the platform's books into :class:`ExposureLeg` rows.

Three sources, one shape:

* **runner equity positions** — ``StrategyRunner.positions`` (delta-one);
* **runner option books** — every open leg of every structure on a runner's
  :class:`~backtest.forward.options_bridge.OptionsBridge`;
* **the manual options book** — the dashboard ``OptionPaperBroker``
  (``/options`` page), which the Portfolio summary already merges.

Read-only: collectors take each runner's lock just long enough to copy the
numbers and never mutate anything.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

from backtest.intelligence.greeks import ExposureLeg

logger = logging.getLogger("backtest.intelligence")

MANUAL_BOOK_ID = "manual_book"
MANUAL_BOOK_LABEL = "Manual Options Book"


def _as_date(value: Any) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _leg_option_type(leg: Any) -> str:
    """``CE``/``PE`` for a leg, falling back to its symbol/token suffix.

    Two-sided structures (strangles) can carry the structure's side marker
    (``"BOTH"``) in ``option_type``; the contract symbol still says which
    side the leg really is.
    """
    raw = str(getattr(leg, "option_type", "") or "").upper()
    if raw in ("CE", "PE"):
        return raw
    for attr in ("trading_symbol", "instrument_token", "symbol"):
        text = str(getattr(leg, attr, "") or "").upper()
        if text.endswith("CE") or text.endswith("CALL"):
            return "CE"
        if text.endswith("PE") or text.endswith("PUT"):
            return "PE"
    return raw


def _runner_legs(runner: Any) -> List[ExposureLeg]:
    legs: List[ExposureLeg] = []
    cfg = runner.config
    label = f"{cfg.name}"
    stale = getattr(runner, "status", "") != "RUNNING"
    lock = getattr(runner, "_lock", None)

    def _collect() -> None:
        for sym, pos in (runner.positions or {}).items():
            qty = float(pos.get("qty") or 0.0)
            if qty <= 0:
                continue
            price = float(runner.last_price.get(sym, pos.get("entry_price") or 0.0) or 0.0)
            legs.append(
                ExposureLeg(
                    source_id=runner.instance_id,
                    source_label=label,
                    strategy=cfg.strategy_name,
                    mode=cfg.mode,
                    position_key=sym,
                    kind="equity",
                    underlying=str(sym).upper(),
                    units=qty,
                    sign=1 if pos.get("side", "LONG") == "LONG" else -1,
                    spot=price or None,
                    current_price=price or None,
                    stale=stale,
                )
            )
        bridge = getattr(runner, "options_bridge", None)
        if bridge is None:
            return
        for structure in bridge.option_broker.get_open_structures():
            for leg in structure.legs:
                if getattr(getattr(leg, "status", None), "value", "open") != "open":
                    continue
                try:
                    inputs = bridge.leg_pricing_inputs(leg)
                except Exception:  # noqa: BLE001 — analytics never break the book
                    inputs = {}
                legs.append(
                    ExposureLeg(
                        source_id=runner.instance_id,
                        source_label=label,
                        strategy=cfg.strategy_name,
                        mode=cfg.mode,
                        position_key=structure.structure_id,
                        kind="option",
                        underlying=str(leg.underlying or structure.underlying).upper(),
                        units=float(leg.total_quantity),
                        sign=1 if leg.is_long else -1,
                        spot=inputs.get("spot"),
                        option_type=_leg_option_type(leg),
                        strike=float(leg.strike),
                        expiry=_as_date(inputs.get("expiry")),
                        iv=inputs.get("iv"),
                        reference_date=inputs.get("reference_date"),
                        structure_type=structure.structure_type,
                        current_price=float(leg.current_price or 0.0),
                        stale=stale,
                    )
                )

    if lock is not None:
        with lock:
            _collect()
    else:
        _collect()
    return legs


def _manual_book_legs(manager: Any) -> List[ExposureLeg]:
    broker = manager._get_dashboard_book() if hasattr(manager, "_get_dashboard_book") else None
    if broker is None:
        return []
    try:
        from backtest.web.options_api import broker_spot, get_quote_provider

        quotes = get_quote_provider()
    except Exception:  # noqa: BLE001
        quotes = None
        broker_spot = None  # type: ignore[assignment]
    generator = getattr(quotes, "generator", None) or getattr(
        getattr(quotes, "inner", None), "generator", None
    )
    contracts = getattr(quotes, "_contracts", None) or getattr(
        getattr(quotes, "inner", None), "_contracts", None
    )
    reference = getattr(quotes, "reference_date", None)
    legs: List[ExposureLeg] = []
    for structure in broker.get_open_structures():
        for leg in structure.legs:
            if getattr(getattr(leg, "status", None), "value", "open") != "open":
                continue
            underlying = str(leg.underlying or structure.underlying).upper()
            spot: Optional[float] = None
            if quotes is not None and broker_spot is not None:
                try:
                    spot = float(broker_spot(quotes, underlying)) or None
                except Exception:  # noqa: BLE001
                    spot = None
            iv: Optional[float] = None
            if isinstance(contracts, dict):
                contract = contracts.get(str(leg.instrument_token))
                meta = getattr(contract, "metadata", None) or {}
                raw = meta.get("vol") or meta.get("implied_volatility")
                try:
                    iv = float(raw) if raw else None
                except (TypeError, ValueError):
                    iv = None
            if iv is None and generator is not None:
                iv = (getattr(generator, "VOL", {}) or {}).get(underlying)
            legs.append(
                ExposureLeg(
                    source_id=MANUAL_BOOK_ID,
                    source_label=MANUAL_BOOK_LABEL,
                    strategy=structure.strategy_name or "manual",
                    mode="paper",
                    position_key=structure.structure_id,
                    kind="option",
                    underlying=underlying,
                    units=float(leg.total_quantity),
                    sign=1 if leg.is_long else -1,
                    spot=spot,
                    option_type=_leg_option_type(leg),
                    strike=float(leg.strike),
                    expiry=_as_date(leg.expiry or structure.expiry),
                    iv=iv,
                    reference_date=_as_date(reference() if callable(reference) else reference),
                    structure_type=structure.structure_type,
                    current_price=float(leg.current_price or 0.0),
                )
            )
    return legs


def collect_legs(manager: Any, mode: Optional[str] = None) -> Tuple[List[ExposureLeg], Dict]:
    """Every open leg across the platform, optionally bucket-scoped.

    Returns ``(legs, meta)`` where ``meta`` carries the runner roster (for
    subscription / regime-fit displays) keyed by instance id.
    """
    legs: List[ExposureLeg] = []
    roster: Dict[str, Dict[str, Any]] = {}
    lock = getattr(manager, "_lock", None)
    if lock is not None:
        with lock:
            runners = list(manager._runners.values())
    else:
        runners = list(getattr(manager, "_runners", {}).values())
    for runner in runners:
        if mode and runner.config.mode != mode:
            continue
        roster[runner.instance_id] = {
            "instance_id": runner.instance_id,
            "name": runner.config.name,
            "strategy": runner.config.strategy_name,
            "mode": runner.config.mode,
            "status": runner.status,
            "symbols": list(runner.config.symbols),
            "source": runner.config.source,
        }
        try:
            legs.extend(_runner_legs(runner))
        except Exception:  # noqa: BLE001 — one bad book must not blank the view
            logger.exception("[intelligence] leg collection failed for %s", runner.instance_id[:8])
    if mode in (None, "paper"):
        try:
            legs.extend(_manual_book_legs(manager))
        except Exception:  # noqa: BLE001
            logger.debug("[intelligence] manual book collection failed", exc_info=True)
    return legs, {"roster": roster}
