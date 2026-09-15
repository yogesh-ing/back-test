"""Option instrument model with V1 validation constraints.

V1 only accepts **European cash-settled index options** on NIFTY and
BANKNIFTY. American, physical-settled, and stock options are rejected
at validation time.

The ``OptionContract`` is immutable (``frozen=True``) — once created
it never mutates, which keeps it safe as a dict key / set member.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from backtest.instruments.base import (
    BaseInstrument,
    ExerciseType,
    InstrumentQuote,
    InstrumentType,
    OptionType,
    SettlementType,
)

# V1: only these underlyings are supported
SUPPORTED_UNDERLYINGS: frozenset[str] = frozenset({"NIFTY", "BANKNIFTY"})


@dataclass(frozen=True)
class OptionContract(BaseInstrument):
    """A single option contract (European cash-settled index option).

    Parameters
    ----------
    instrument_token:
        Unique ID from the exchange/broker.
    trading_symbol:
        Human-readable symbol, e.g. ``"NIFTY24DEC24500CE"``.
    underlying:
        The underlying index — ``"NIFTY"`` or ``"BANKNIFTY"`` in V1.
    exchange:
        Exchange — ``"NSE"``.
    segment:
        Segment — ``"NFO"`` (derivatives).
    expiry:
        Expiry date of the contract.
    strike:
        Strike price.
    option_type:
        Call (``"CE"``) or Put (``"PE"``).
    lot_size:
        Minimum order quantity (e.g. 25 for NIFTY).
    tick_size:
        Minimum price movement (typically ₹0.05).
    contract_type:
        Exercise style — ``"european"`` in V1.
    settlement_type:
        Settlement method — ``"cash"`` in V1.
    """

    instrument_token: str = ""
    trading_symbol: str = ""
    underlying: str = ""
    exchange: str = "NSE"
    segment: str = "NFO"
    instrument_type: InstrumentType = InstrumentType.OPTION
    expiry: date | None = None
    strike: Decimal = Decimal("0")
    option_type: OptionType | str = OptionType.CE
    lot_size: int = 0
    tick_size: Decimal = Decimal("0.05")
    contract_type: ExerciseType | str = ExerciseType.EUROPEAN
    settlement_type: SettlementType | str = SettlementType.CASH
    active: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> list[str]:
        """Validate V1 option constraints.

        Returns a list of human-readable error strings. Empty list means valid.
        """
        errors: list[str] = []

        # Required fields
        if not self.instrument_token:
            errors.append("instrument_token is required")
        if not self.trading_symbol:
            errors.append("trading_symbol is required")
        if not self.underlying:
            errors.append("underlying is required")

        # V1: only supported underlyings
        if self.underlying and self.underlying not in SUPPORTED_UNDERLYINGS:
            errors.append(
                f"V1 only supports {SUPPORTED_UNDERLYINGS}, got '{self.underlying}'"
            )

        # Exchange / segment
        if self.exchange != "NSE":
            errors.append(f"V1 only supports NSE, got '{self.exchange}'")
        if self.segment != "NFO":
            errors.append(f"V1 only supports NFO segment, got '{self.segment}'")

        # Expiry
        if self.expiry is None:
            errors.append("expiry is required")
        elif self.expiry < date.today():
            errors.append(f"expiry {self.expiry} is in the past")

        # Strike
        if self.strike <= 0:
            errors.append(f"strike must be > 0, got {self.strike}")

        # Option type
        ot = self.option_type
        if isinstance(ot, str):
            if ot not in ("CE", "PE"):
                errors.append(f"option_type must be CE or PE, got '{ot}'")
        elif not isinstance(ot, OptionType):
            errors.append(f"option_type must be OptionType or str, got {type(ot)}")

        # Lot size
        if self.lot_size < 1:
            errors.append(f"lot_size must be >= 1, got {self.lot_size}")

        # V1: European cash-settled only
        ct = self.contract_type
        if isinstance(ct, str) and ct != "european":
            errors.append(
                f"V1 only supports european options, got '{ct}'"
            )
        elif isinstance(ct, ExerciseType) and ct != ExerciseType.EUROPEAN:
            errors.append(
                f"V1 only supports european options, got {ct.value}"
            )

        st = self.settlement_type
        if isinstance(st, str) and st != "cash":
            errors.append(
                f"V1 only supports cash settlement, got '{st}'"
            )
        elif isinstance(st, SettlementType) and st != SettlementType.CASH:
            errors.append(
                f"V1 only supports cash settlement, got {st.value}"
            )

        return errors

    def __str__(self) -> str:
        option_type = (
            self.option_type
            if isinstance(self.option_type, str)
            else self.option_type.value
        )
        return (
            f"{self.underlying} {self.strike}{option_type} "
            f"exp={self.expiry}"
        )


@dataclass(frozen=True)
class OptionQuote(InstrumentQuote):
    """L1 quote for an option contract.

    Extends ``InstrumentQuote`` with option-specific fields. IV may not
    be available from all brokers — check for ``None``.
    """

    iv: Decimal | None = None
    oi: int | None = None  # Open interest (more meaningful for options)
