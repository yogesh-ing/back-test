"""Equity instrument model.

Represents a plain equity share (e.g. RELIANCE, TCS). Equity instruments
are used by the existing backtest and forward-testing engines — this model
exists so the instruments registry can hold both equity and option
contracts under a common interface.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from backtest.instruments.base import BaseInstrument, InstrumentType


@dataclass(frozen=True)
class EquityInstrument(BaseInstrument):
    """A single equity share on NSE/BSE.

    Parameters
    ----------
    instrument_token:
        Unique ID from the exchange/broker (e.g. ``"11536"`` for RELIANCE).
    trading_symbol:
        Display symbol (e.g. ``"RELIANCE"``).
    exchange:
        Exchange identifier — ``"NSE"`` or ``"BSE"``.
    segment:
        Segment within the exchange — ``"CASH"`` or ``"CNC"``.
    lot_size:
        Minimum order quantity (always 1 for equities).
    tick_size:
        Minimum price movement (typically ₹0.05 or ₹0.01).
    isin:
        International Securities Identification Number (optional).
    """

    instrument_token: str = ""
    trading_symbol: str = ""
    exchange: str = "NSE"
    segment: str = "CASH"
    instrument_type: InstrumentType = InstrumentType.EQUITY
    lot_size: int = 1
    tick_size: float = 0.05
    isin: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> list[str]:
        """Validate equity-specific constraints."""
        errors: list[str] = []
        if not self.instrument_token:
            errors.append("instrument_token is required")
        if not self.trading_symbol:
            errors.append("trading_symbol is required")
        if self.exchange not in ("NSE", "BSE"):
            errors.append(f"unsupported exchange: {self.exchange}")
        if self.lot_size < 1:
            errors.append(f"lot_size must be >= 1, got {self.lot_size}")
        if self.tick_size <= 0:
            errors.append(f"tick_size must be > 0, got {self.tick_size}")
        return errors
