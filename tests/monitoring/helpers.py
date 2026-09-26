"""Builders shared by the portfolio-monitor tests."""

from __future__ import annotations

import itertools
from datetime import date
from typing import Any, Dict, List, Optional

from backtest.monitoring.models import (
    INSTRUMENT_EQUITY,
    INSTRUMENT_OPTION,
    MonitorPosition,
    PortfolioInputs,
    StrategyBook,
)

_ids = itertools.count(1)


def option_leg(
    *,
    strategy: str = "S1",
    side: str = "SHORT",
    option_type: str = "CE",
    strike: float = 25000.0,
    spot: float = 25000.0,
    units: float = 150.0,
    dte: float = 7.0,
    iv: float = 0.15,
    underlying: str = "NIFTY",
    lot_size: int = 75,
    expiry: Optional[date] = date(2026, 10, 1),
    structure_id: Optional[str] = None,
    structure_type: Optional[str] = "short_straddle",
    premium: float = 0.0,
) -> MonitorPosition:
    return MonitorPosition(
        position_id=f"leg-{next(_ids)}",
        strategy_id=strategy,
        strategy_name=strategy,
        strategy_kind="test",
        book="runner",
        mode="paper",
        instrument_type=INSTRUMENT_OPTION,
        symbol=f"{underlying}{strike:g}{option_type}",
        underlying=underlying,
        side=side,
        units=units,
        underlying_price=spot,
        current_price=premium,
        lot_size=lot_size,
        option_type=option_type,
        strike=strike,
        expiry=expiry,
        dte_days=dte,
        iv=iv,
        iv_source="contract",
        structure_id=structure_id or f"{strategy}-struct",
        structure_type=structure_type,
    )


def equity_pos(*, strategy: str = "E1", symbol: str = "NIFTY", units: float = 10.0,
               price: float = 25000.0, side: str = "LONG") -> MonitorPosition:
    return MonitorPosition(
        position_id=f"eq-{next(_ids)}",
        strategy_id=strategy,
        strategy_name=strategy,
        strategy_kind="test",
        book="runner",
        mode="paper",
        instrument_type=INSTRUMENT_EQUITY,
        symbol=symbol,
        underlying=symbol,
        side=side,
        units=units,
        underlying_price=price,
        current_price=price,
    )


def book(name: str, *, capital: float = 500_000.0, equity: Optional[float] = None,
         curve: Optional[List[Dict[str, Any]]] = None, structure_type: Optional[str] = None,
         kind: str = "test") -> StrategyBook:
    return StrategyBook(
        strategy_id=name,
        strategy_name=name,
        strategy_kind=kind,
        book="runner",
        mode="paper",
        structure_type=structure_type,
        capital=capital,
        equity=capital if equity is None else equity,
        equity_curve=curve or [],
    )


def inputs(positions: List[MonitorPosition], strategies: Optional[List[StrategyBook]] = None,
           *, equity: float = 500_000.0, daily_pnl: float = 0.0,
           daily_loss_limit: Optional[float] = None) -> PortfolioInputs:
    if strategies is None:
        names = sorted({p.strategy_id for p in positions})
        strategies = [book(n, capital=equity / max(len(names), 1)) for n in names]
    return PortfolioInputs(
        positions=positions,
        strategies=strategies,
        total_capital=equity,
        total_equity=equity,
        mode="all",
        as_of="2026-09-26T09:30:00+00:00",
        daily_pnl=daily_pnl,
        daily_loss_limit=daily_loss_limit,
    )


def straddle(strategy: str = "S1", units: float = 150.0, **kw: Any) -> List[MonitorPosition]:
    """Short ATM straddle: the PRD's canonical short-gamma book."""
    return [option_leg(strategy=strategy, option_type="CE", units=units, **kw),
            option_leg(strategy=strategy, option_type="PE", units=units, **kw)]
