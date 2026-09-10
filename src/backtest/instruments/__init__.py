"""Instruments package for options trading support.

Provides instrument models (OptionContract, EquityInstrument), validation,
and an in-memory registry for querying instruments by various criteria.

Usage::

    from backtest.instruments import OptionContract, InstrumentRegistry

    registry = InstrumentRegistry()
    chain = registry.find_contracts(underlying="NIFTY", option_type="CE")
"""

from __future__ import annotations

from backtest.instruments.base import InstrumentType, SettlementType
from backtest.instruments.option import OptionContract, OptionQuote
from backtest.instruments.equity import EquityInstrument
from backtest.instruments.registry import InstrumentRegistry

__all__ = [
    "InstrumentType",
    "SettlementType",
    "OptionContract",
    "OptionQuote",
    "EquityInstrument",
    "InstrumentRegistry",
]
