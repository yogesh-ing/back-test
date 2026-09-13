"""Base instrument abstractions and enums.

All instrument types (equity, option, future) share a common base class
and these enumerations. V1 only supports European cash-settled index options
(NIFTY, BANKNIFTY).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any


class InstrumentType(Enum):
    """Type of tradable instrument."""

    EQUITY = "equity"
    OPTION = "option"
    FUTURE = "future"


class OptionType(Enum):
    """Option right — call or put."""

    CE = "CE"  # Call European
    PE = "PE"  # Put European


class SettlementType(Enum):
    """How the option is settled at expiry."""

    CASH = "cash"
    PHYSICAL = "physical"


class ExerciseType(Enum):
    """Exercise style — European (expiry only) or American (any time)."""

    EUROPEAN = "european"
    AMERICAN = "american"


@dataclass(frozen=True)
class BaseInstrument(ABC):
    """Abstract base for all instruments.

    Every instrument carries an ``instrument_token`` (unique ID from the
    exchange/broker), a ``trading_symbol``, and exchange metadata.
    """

    instrument_token: str
    trading_symbol: str
    exchange: str
    segment: str
    instrument_type: InstrumentType

    @abstractmethod
    def validate(self) -> list[str]:
        """Return a list of validation errors (empty = valid).

        Implementations enforce type-specific invariants, e.g. V1
        options must be European cash-settled.
        """

    def is_valid(self) -> bool:
        """True if ``validate()`` returns no errors."""
        return len(self.validate()) == 0


@dataclass(frozen=True)
class InstrumentQuote:
    """L1 quote for any instrument.

    Fields mirror what mStock and most Indian brokers provide. Not all
    fields are guaranteed to be populated — check for ``None``.
    """

    instrument_token: str
    ltp: Decimal  # Last traded price
    bid: Decimal | None = None
    ask: Decimal | None = None
    bid_size: int | None = None
    ask_size: int | None = None
    volume: int | None = None
    oi: int | None = None  # Open interest
    iv: Decimal | None = None  # Implied volatility (if provided)
    timestamp: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
