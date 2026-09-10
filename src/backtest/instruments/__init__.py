"""Instruments package for options trading support.

Provides instrument models (OptionContract, EquityInstrument), validation,
an in-memory registry for querying instruments by various criteria, and an
expiry calendar for monthly index options.

Usage::

    from backtest.instruments import OptionContract, InstrumentRegistry, ExpiryCalendar

    registry = InstrumentRegistry()
    chain = registry.find_contracts(underlying="NIFTY", option_type="CE")

    cal = ExpiryCalendar()
    next_exp = cal.next_expiry("NIFTY")
"""

from __future__ import annotations

from backtest.instruments.base import InstrumentType, SettlementType
from backtest.instruments.equity import EquityInstrument
from backtest.instruments.expiry_calendar import ExpiryCalendar, last_thursday
from backtest.instruments.option import OptionContract, OptionQuote
from backtest.instruments.registry import InstrumentRegistry

__all__ = [
    "ExpiryCalendar",
    "InstrumentRegistry",
    "InstrumentType",
    "OptionContract",
    "OptionQuote",
    "EquityInstrument",
    "SettlementType",
    "last_thursday",
]
