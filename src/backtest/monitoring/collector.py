"""Collect every open exposure from the command center into normalized inputs.

The only module in :mod:`backtest.monitoring` that knows the shape of a
runner, an options bridge or the dashboard (manual) options book. Two books
are merged, matching the manager's "honest totals" rule:

* **runner books** — equity positions (``runner.positions``) and option legs
  (``runner.options_bridge.option_broker``) for every runner in scope;
* **dashboard book** — the manual ``/options`` broker, when it exists (it is
  counted under ``mode="paper"`` — it is a simulated book).

Implied vol per leg, in order of confidence: the chain contract's own vol
(synthetic chains stamp it), an IV solved from the leg's current premium
(live quotes), then the config default — each labelled via ``iv_source``.

Days to expiry run on the **book's own clock** (the bar timestamp for a
runner, wall-clock for the dashboard book) and to the 15:30 IST close of the
expiry date, so expiry-day legs keep their (large) gamma instead of
collapsing to zero at midnight.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from backtest.monitoring.config import MonitorConfig
from backtest.monitoring.models import (
    INSTRUMENT_EQUITY,
    INSTRUMENT_OPTION,
    IV_SOURCE_CONTRACT,
    IV_SOURCE_DEFAULT,
    IV_SOURCE_IMPLIED,
    SIDE_LONG,
    SIDE_SHORT,
    MonitorPosition,
    PortfolioInputs,
    StrategyBook,
)
from backtest.options.greeks import BlackScholes

logger = logging.getLogger("backtest.monitoring.collector")

IST = timezone(timedelta(hours=5, minutes=30))
EXPIRY_CLOSE = time(15, 30)
DASHBOARD_ID = "dashboard"

#: Solved IVs outside this band are treated as a failed solve (bad quote).
IV_SANE_RANGE = (0.02, 3.0)


def _as_ist(moment: Any) -> datetime:
    if isinstance(moment, datetime):
        if moment.tzinfo is None:
            return moment.replace(tzinfo=IST)  # market-local bar clock
        return moment.astimezone(IST)
    if isinstance(moment, date):
        return datetime.combine(moment, time(9, 15), tzinfo=IST)
    return datetime.now(IST)


def days_to_expiry(expiry: Any, as_of: Any = None) -> Optional[float]:
    """Calendar days from ``as_of`` to the expiry-date close (≥ 0)."""
    if expiry is None:
        return None
    if isinstance(expiry, datetime):
        expiry = expiry.date()
    if isinstance(expiry, str):
        try:
            expiry = date.fromisoformat(expiry[:10])
        except ValueError:
            return None
    if not isinstance(expiry, date):
        return None
    close = datetime.combine(expiry, EXPIRY_CLOSE, tzinfo=IST)
    delta = (close - _as_ist(as_of)).total_seconds() / 86400.0
    return max(delta, 0.0)


def _side(leg: Any) -> str:
    is_long = getattr(leg, "is_long", None)
    if is_long is None:
        is_long = str(getattr(leg, "side", "BUY")).upper() in ("BUY", "LONG")
    return SIDE_LONG if is_long else SIDE_SHORT


def _shared_strike_spots(legs: List[Any]) -> Dict[str, float]:
    """Last-resort spot per underlying: the mean strike of the book's legs.

    Only used when no price for the underlying exists anywhere in the
    portfolio. Shared by every leg of that underlying, so a spread's legs are
    priced against ONE spot (a per-leg "spot = own strike" guess would make a
    bull call spread read as delta-neutral or even short).
    """
    strikes: Dict[str, List[float]] = {}
    for leg in legs:
        und = str(getattr(leg, "underlying", "") or "").upper()
        strike = float(getattr(leg, "strike", 0) or 0)
        if und and strike > 0:
            strikes.setdefault(und, []).append(strike)
    return {und: sum(v) / len(v) for und, v in strikes.items()}


class PositionCollector:
    def __init__(self, config: Optional[MonitorConfig] = None) -> None:
        self.config = config or MonitorConfig()
        self._bs = BlackScholes(
            risk_free_rate=self.config.risk_free_rate, volatility=self.config.default_iv
        )

    # ------------------------------------------------------------------ #
    # IV resolution
    # ------------------------------------------------------------------ #

    def resolve_iv(
        self,
        contract_vol: Optional[float],
        premium: float,
        spot: float,
        strike: float,
        dte_days: Optional[float],
        option_type: str,
    ) -> Tuple[float, str]:
        if contract_vol and contract_vol > 0:
            return float(contract_vol), IV_SOURCE_CONTRACT
        if premium and premium > 0 and spot > 0 and strike > 0 and dte_days and dte_days > 0:
            years = dte_days / 365.0
            intrinsic = max(spot - strike, 0.0) if option_type == "CE" else max(strike - spot, 0.0)
            if premium > intrinsic:  # no time value → IV undefined
                try:
                    iv = self._bs.implied_volatility(
                        market_price=premium, spot=spot, strike=strike,
                        expiry_years=years, option_type=option_type,
                    )
                    if IV_SANE_RANGE[0] <= iv <= IV_SANE_RANGE[1]:
                        return float(iv), IV_SOURCE_IMPLIED
                except (ValueError, ZeroDivisionError, OverflowError):
                    pass
        return float(self.config.default_iv), IV_SOURCE_DEFAULT

    # ------------------------------------------------------------------ #
    # Leg → MonitorPosition
    # ------------------------------------------------------------------ #

    def _option_position(
        self,
        leg: Any,
        *,
        strategy_id: str,
        strategy_name: str,
        strategy_kind: str,
        book: str,
        mode: str,
        spot: Optional[float],
        as_of: Any,
        contract_vol: Optional[float],
        structure_types: Dict[str, str],
        fallback_expiry: Any = None,
    ) -> Optional[MonitorPosition]:
        strike = float(getattr(leg, "strike", 0) or 0)
        option_type = str(getattr(leg, "option_type", "CE") or "CE").upper()
        if option_type not in ("CE", "PE"):
            return None
        underlying = str(getattr(leg, "underlying", "") or "").upper() or "UNKNOWN"
        spot_f = float(spot) if spot else strike  # ATM approximation, as elsewhere
        if spot_f <= 0 or strike <= 0:
            logger.debug("[monitor] skipping leg %s: no spot/strike",
                         getattr(leg, "trading_symbol", "?"))
            return None
        expiry = getattr(leg, "expiry", None) or fallback_expiry
        if isinstance(expiry, datetime):
            expiry = expiry.date()
        dte = days_to_expiry(expiry, as_of)
        if dte is None:
            dte = 30.0  # PortfolioGreeksCalculator's documented fallback
        premium = float(getattr(leg, "current_price", 0) or 0)
        iv, iv_source = self.resolve_iv(contract_vol, premium, spot_f, strike, dte, option_type)
        lot_size = int(getattr(leg, "lot_size", 1) or 1)
        units = float(getattr(leg, "total_quantity", 0) or 0)
        if units <= 0:
            return None
        structure_id = str(getattr(leg, "structure_id", "") or "") or None
        return MonitorPosition(
            position_id=str(getattr(leg, "position_id", "") or id(leg)),
            strategy_id=strategy_id,
            strategy_name=strategy_name,
            strategy_kind=strategy_kind,
            book=book,
            mode=mode,
            instrument_type=INSTRUMENT_OPTION,
            symbol=str(
                getattr(leg, "trading_symbol", "") or f"{underlying}{strike:g}{option_type}"
            ),
            underlying=underlying,
            side=_side(leg),
            units=units,
            underlying_price=spot_f,
            current_price=premium,
            entry_price=float(getattr(leg, "entry_price", 0) or 0),
            lot_size=lot_size,
            option_type=option_type,
            strike=strike,
            expiry=expiry if isinstance(expiry, date) else None,
            dte_days=dte,
            iv=iv,
            iv_source=iv_source,
            structure_id=structure_id,
            structure_type=structure_types.get(structure_id or ""),
        )

    # ------------------------------------------------------------------ #
    # Books
    # ------------------------------------------------------------------ #

    def _runner_inputs(
        self, runner: Any, market: Optional[Dict[str, float]] = None
    ) -> Tuple[List[MonitorPosition], StrategyBook, Dict[str, List[Dict[str, Any]]]]:
        cfg = runner.config
        instrument = dict(getattr(cfg, "instrument", {}) or {})
        expression = dict(instrument.get("expression") or {})
        is_option = str(instrument.get("type", "equity")) == "option"
        mode = str(getattr(cfg, "mode", "paper") or "paper")
        positions: List[MonitorPosition] = []
        lock = getattr(runner, "_lock", None)

        def _read() -> Tuple[Dict[str, Any], Dict[str, float], List[Dict[str, Any]],
                             Dict[str, List[Dict[str, Any]]]]:
            pos = dict(runner.positions)
            last = dict(getattr(runner, "last_price", {}) or {})
            curve = list(getattr(runner, "equity_curve", []) or [])
            bars = {s: list(b) for s, b in (getattr(runner, "_bars", {}) or {}).items()}
            return pos, last, curve, bars

        if lock is not None:
            with lock:
                eq_positions, last_price, curve, bars = _read()
        else:
            eq_positions, last_price, curve, bars = _read()

        for sym, pos in eq_positions.items():
            qty = abs(float(pos.get("qty") or 0))
            if qty <= 0:
                continue
            price = float(last_price.get(sym, pos.get("entry_price") or 0) or 0)
            positions.append(
                MonitorPosition(
                    position_id=f"{runner.instance_id}:{sym}",
                    strategy_id=runner.instance_id,
                    strategy_name=cfg.name,
                    strategy_kind=cfg.strategy_name,
                    book="runner",
                    mode=mode,
                    instrument_type=INSTRUMENT_EQUITY,
                    symbol=sym,
                    underlying=str(sym).upper(),
                    side=SIDE_LONG if str(pos.get("side", "LONG")).upper() == "LONG"
                    else SIDE_SHORT,
                    units=qty,
                    underlying_price=price,
                    current_price=price,
                    entry_price=float(pos.get("entry_price") or 0),
                )
            )

        margin = sum(p.units * p.current_price for p in positions)
        bridge = getattr(runner, "options_bridge", None)
        if bridge is not None:
            try:
                ctx = bridge.pricing_context()
                broker = bridge.option_broker
                structures = {s.structure_id: s.structure_type
                              for s in broker.get_open_structures()}
                ctx_und = str(ctx.get("underlying") or "").upper()
                open_legs = list(broker.get_open_positions())
                shared = _shared_strike_spots(open_legs)
                for leg in open_legs:
                    und = str(getattr(leg, "underlying", "") or "").upper()
                    # One spot per underlying for the whole book — never a
                    # per-leg guess, or a spread's legs price inconsistently.
                    leg_spot = (
                        (ctx.get("spot") if und == ctx_und else None)
                        or last_price.get(und)
                        or (market or {}).get(und)
                        or shared.get(und)
                    )
                    mp = self._option_position(
                        leg,
                        strategy_id=runner.instance_id,
                        strategy_name=cfg.name,
                        strategy_kind=cfg.strategy_name,
                        book="runner",
                        mode=mode,
                        spot=leg_spot,
                        as_of=ctx.get("as_of"),
                        contract_vol=bridge.leg_contract_vol(leg),
                        structure_types=structures,
                        fallback_expiry=ctx.get("structure_expiry"),
                    )
                    if mp is not None:
                        positions.append(mp)
                margin += float(broker.total_margin_used)
            except Exception:  # noqa: BLE001 — one bad book must not blank the monitor
                logger.exception("[monitor] option book read failed for %s",
                                 runner.instance_id[:8])

        try:
            equity = float(runner.equity())
        except Exception:  # noqa: BLE001
            equity = float(cfg.allocated_capital)
        try:
            daily = float(runner.daily_pnl())
        except Exception:  # noqa: BLE001
            daily = 0.0
        book = StrategyBook(
            strategy_id=runner.instance_id,
            strategy_name=cfg.name,
            strategy_kind=cfg.strategy_name,
            book="runner",
            mode=mode,
            status=str(getattr(runner, "status", "")),
            instrument_type=INSTRUMENT_OPTION if is_option else INSTRUMENT_EQUITY,
            structure_type=str(expression.get("type")) if is_option and expression.get("type")
            else None,
            symbols=list(cfg.symbols),
            capital=float(cfg.allocated_capital),
            equity=equity,
            margin_used=margin,
            daily_pnl=daily,
            equity_curve=curve,
        )
        return positions, book, bars

    def _dashboard_inputs(
        self, manager: Any, market: Optional[Dict[str, float]] = None
    ) -> Tuple[List[MonitorPosition], Optional[StrategyBook]]:
        getter = getattr(manager, "_get_dashboard_book", None)
        broker = getter() if callable(getter) else None
        if broker is None:
            return [], None
        positions: List[MonitorPosition] = []
        try:
            from backtest.web.options_api import broker_spot, get_quote_provider

            quotes = get_quote_provider()
            open_legs = list(broker.get_open_positions())
            structures = {s.structure_id: s.structure_type for s in broker.get_open_structures()}
            contracts = getattr(quotes, "_contracts", None) or getattr(
                getattr(quotes, "inner", None), "_contracts", None
            )
            spots: Dict[str, Optional[float]] = {}
            shared = _shared_strike_spots(open_legs)
            for leg in open_legs:
                und = str(getattr(leg, "underlying", "") or "").upper()
                if und not in spots:
                    try:
                        spots[und] = float(broker_spot(quotes, und))
                    except Exception:  # noqa: BLE001
                        spots[und] = None
                vol = None
                token = str(getattr(leg, "instrument_token", "") or "")
                if token and isinstance(contracts, dict) and token in contracts:
                    meta = getattr(contracts[token], "metadata", None) or {}
                    raw = meta.get("vol") or meta.get("implied_volatility")
                    try:
                        vol = float(raw) if raw else None
                    except (TypeError, ValueError):
                        vol = None
                mp = self._option_position(
                    leg,
                    strategy_id=DASHBOARD_ID,
                    strategy_name="Manual options book",
                    strategy_kind="manual",
                    book="dashboard",
                    mode="paper",
                    spot=spots.get(und) or (market or {}).get(und) or shared.get(und),
                    as_of=None,
                    contract_vol=vol,
                    structure_types=structures,
                )
                if mp is not None:
                    positions.append(mp)
            book = StrategyBook(
                strategy_id=DASHBOARD_ID,
                strategy_name="Manual options book",
                strategy_kind="manual",
                book="dashboard",
                mode="paper",
                instrument_type=INSTRUMENT_OPTION,
                symbols=sorted({p.underlying for p in positions}),
                capital=float(broker.capital),
                equity=float(broker.total_equity),
                margin_used=float(broker.total_margin_used),
            )
        except Exception:  # noqa: BLE001
            logger.exception("[monitor] dashboard book read failed")
            return [], None
        if not positions:
            return [], None
        return positions, book

    # ------------------------------------------------------------------ #
    # Public
    # ------------------------------------------------------------------ #

    @staticmethod
    def _market_spots(manager: Any) -> Dict[str, float]:
        """Latest price per symbol across EVERY runner (all buckets — a price
        is a price). Spot fallback for option books whose own bridge has not
        seen a bar yet, e.g. a runner that was just spawned or is paused."""
        with manager._lock:
            runners = list(manager._runners.values())
        market: Dict[str, float] = {}
        for runner in runners:
            try:
                for sym, price in dict(getattr(runner, "last_price", {}) or {}).items():
                    if price and float(price) > 0:
                        market[str(sym).upper()] = float(price)
                bridge = getattr(runner, "options_bridge", None)
                if bridge is not None:
                    ctx = bridge.pricing_context()
                    if ctx.get("spot") and ctx.get("underlying"):
                        market.setdefault(str(ctx["underlying"]).upper(), float(ctx["spot"]))
            except Exception:  # noqa: BLE001 — a fallback map, best effort
                continue
        return market

    def collect(self, manager: Any, mode: Optional[str] = None) -> PortfolioInputs:
        """Snapshot every open exposure in scope (``None`` = all buckets)."""
        scope = mode or "all"
        with manager._lock:
            runners = [
                r for r in manager._runners.values()
                if mode is None or manager._runner_bucket(r) == mode
            ]
        positions: List[MonitorPosition] = []
        strategies: List[StrategyBook] = []
        bars: Dict[str, List[Dict[str, Any]]] = {}
        market = self._market_spots(manager)
        for runner in runners:
            try:
                pos, book, runner_bars = self._runner_inputs(runner, market)
            except Exception:  # noqa: BLE001
                logger.exception("[monitor] runner read failed for %s", runner.instance_id[:8])
                continue
            positions.extend(pos)
            strategies.append(book)
            for sym, rows in runner_bars.items():
                key = str(sym).upper()
                if len(rows) > len(bars.get(key, [])):
                    bars[key] = rows  # the longest buffer any runner holds

        if mode in (None, "paper"):
            dash_pos, dash_book = self._dashboard_inputs(manager, market)
            positions.extend(dash_pos)
            if dash_book is not None:
                strategies.append(dash_book)

        total_capital = sum(b.capital for b in strategies)
        total_equity = sum(b.equity for b in strategies)
        # Runner books only: each runner's own day anchor is flow-aware (a
        # runner added mid-session does not read as a day's profit), and the
        # daily-loss breaker the headroom check mirrors watches runners only.
        daily_pnl = sum(b.daily_pnl for b in strategies if b.book == "runner")
        daily_limit = None
        try:
            daily_limit = float(manager.supervisor.config.daily_loss_limit)
        except Exception:  # noqa: BLE001 — optional context
            pass
        return PortfolioInputs(
            positions=positions,
            strategies=strategies,
            total_capital=total_capital,
            total_equity=total_equity,
            mode=scope,
            as_of=datetime.now(timezone.utc).isoformat(),
            daily_pnl=daily_pnl,
            daily_loss_limit=daily_limit,
            bars=bars,
        )
