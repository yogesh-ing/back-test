"""In-memory instrument registry.

Stores registered instruments (options, equities) and provides lookup
methods by token, underlying, expiry, strike, and option type.

V1 uses an in-memory dict — no DB persistence. The registry is
populated at startup by the broker's ``get_option_chain()`` call and
by any manual registration.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Sequence

from backtest.instruments.equity import EquityInstrument
from backtest.instruments.option import OptionContract


class InstrumentRegistry:
    """In-memory registry of tradable instruments.

    Thread-safety: not thread-safe. All access should happen on the
    same thread as the forward-testing engine or portfolio manager.
    """

    def __init__(self) -> None:
        self._by_token: dict[str, OptionContract | EquityInstrument] = {}

    def register(self, instrument: OptionContract | EquityInstrument) -> None:
        """Register a single instrument (overwrite if token already exists)."""
        if not instrument.is_valid():
            raise ValueError(
                f"Invalid instrument: {instrument.validate()}"
            )
        self._by_token[instrument.instrument_token] = instrument

    def register_many(
        self, instruments: Sequence[OptionContract | EquityInstrument]
    ) -> int:
        """Register multiple instruments. Returns count successfully registered."""
        count = 0
        for inst in instruments:
            try:
                self.register(inst)
                count += 1
            except ValueError:
                pass  # Skip invalid instruments silently
        return count

    def get_by_token(self, token: str) -> OptionContract | EquityInstrument | None:
        """Look up an instrument by its unique token."""
        return self._by_token.get(token)

    def find_options(
        self,
        underlying: str | None = None,
        expiry: date | None = None,
        option_type: str | None = None,
        strike: Decimal | None = None,
        active_only: bool = True,
    ) -> list[OptionContract]:
        """Find option contracts matching the given filters.

        All filters are optional — passing none returns all options.
        """
        results: list[OptionContract] = []
        for inst in self._by_token.values():
            if not isinstance(inst, OptionContract):
                continue
            if active_only and not inst.active:
                continue
            if underlying is not None and inst.underlying != underlying:
                continue
            if expiry is not None and inst.expiry != expiry:
                continue
            if option_type is not None:
                inst_ot = (
                    inst.option_type.value
                    if hasattr(inst.option_type, 'value')
                    else inst.option_type
                )
                if inst_ot != option_type:
                    continue
            if strike is not None and inst.strike != strike:
                continue
            results.append(inst)
        return results

    def find_equities(
        self,
        exchange: str | None = None,
        active_only: bool = True,
    ) -> list[EquityInstrument]:
        """Find equity instruments matching the given filters."""
        results: list[EquityInstrument] = []
        for inst in self._by_token.values():
            if not isinstance(inst, EquityInstrument):
                continue
            if exchange is not None and inst.exchange != exchange:
                continue
            results.append(inst)
        return results

    def get_expiry_dates(self, underlying: str) -> list[date]:
        """Return sorted unique expiry dates for an underlying."""
        dates = {
            inst.expiry
            for inst in self._by_token.values()
            if isinstance(inst, OptionContract)
            and inst.underlying == underlying
            and inst.expiry is not None
        }
        return sorted(dates)

    def get_strikes(
        self, underlying: str, expiry: date, option_type: str | None = None
    ) -> list[Decimal]:
        """Return sorted unique strikes for a given underlying/expiry."""
        strikes = {
            inst.strike
            for inst in self._by_token.values()
            if isinstance(inst, OptionContract)
            and inst.underlying == underlying
            and inst.expiry == expiry
            and (option_type is None or inst.option_type == option_type)
        }
        return sorted(strikes)

    @property
    def count(self) -> int:
        """Total number of registered instruments."""
        return len(self._by_token)

    @property
    def option_count(self) -> int:
        """Number of registered option contracts."""
        return sum(
            1 for i in self._by_token.values() if isinstance(i, OptionContract)
        )

    @property
    def equity_count(self) -> int:
        """Number of registered equity instruments."""
        return sum(
            1 for i in self._by_token.values() if isinstance(i, EquityInstrument)
        )

    def clear(self) -> None:
        """Remove all instruments from the registry."""
        self._by_token.clear()
