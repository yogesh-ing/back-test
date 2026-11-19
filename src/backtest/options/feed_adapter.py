"""Adapt any :class:`OptionPricingFeed` to the surfaces the options backtest
driver and ``OptionPaperBroker`` consume (plan V3 Phase 2).

The broker speaks ``get_quote(token) -> {"ltp", "bid", "ask"}``; the expiry
manager speaks ``get_settlement_price(underlying)``; the driver needs a
``{strike: OptionContract}`` chain. This adapter provides all three by
delegating every price to the injected feed — it holds no pricing math of
its own, so swapping ``ModelBSFeed`` for a recorded-chain feed changes
nothing here.

Quote floors: like the exchange itself, a quotable premium never goes below
the ₹0.05 tick — a zero would read as "quote failed" to the broker's
fail-closed MTM guard and freeze the mark instead of marking the leg down.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

import pandas as pd

from backtest.instruments.base import OptionType
from backtest.instruments.option import OptionContract
from backtest.options.pricing_feed import OptionPricingFeed
from backtest.options.quote_providers import MIN_QUOTE_TICK
from backtest.timekeeping import session_date

__all__ = ["FeedQuoteAdapter"]

#: Lot sizes / strike steps per underlying until the broker instrument master
#: is wired in (Phase 3). Mirrors SyntheticChainGenerator's constants.
_LOT_SIZES = {"NIFTY": 75, "BANKNIFTY": 35}
_STRIKE_STEPS = {"NIFTY": 50, "BANKNIFTY": 100}


class FeedQuoteAdapter:
    """Feed-backed chain generator + quote provider + settlement provider."""

    def __init__(
        self,
        feed: OptionPricingFeed,
        underlying: str,
        *,
        lot_size: int | None = None,
        strike_step: int | None = None,
        strikes_each_side: int = 10,
    ) -> None:
        self.feed = feed
        self.underlying = underlying
        self.lot_size = int(lot_size or _LOT_SIZES.get(underlying, 50))
        self.strike_step = int(strike_step or _STRIKE_STEPS.get(underlying, 50))
        self.strikes_each_side = int(strikes_each_side)
        self._spots: dict[str, float] = {}
        self._contracts: dict[str, OptionContract] = {}
        self._reference: datetime | None = None
        self._session: date | None = None
        self._session_closes: dict[str, list[float]] = {}

    @property
    def source_name(self) -> str:
        return f"feed:{self.feed.source_tag}"

    # -- spot / clock ----------------------------------------------------

    def set_reference(self, reference: datetime | None) -> None:
        self._reference = reference
        if reference is not None:
            self.feed.set_reference(reference)

    def set_spot(self, underlying: str, spot: float) -> None:
        self._spots[underlying] = float(spot)
        if self._reference is None:
            return
        day = session_date(self._reference)
        if day != self._session:
            self._session = day
            self._session_closes = {}
        self._session_closes.setdefault(underlying, []).append(float(spot))

    def get_spot(self, underlying: str) -> float:
        return self._spots.get(underlying, 100.0)

    # -- chain -----------------------------------------------------------

    def generate_chain(
        self,
        underlying: str,
        expiry: date,
        option_type: str = "CE",
    ) -> dict[Decimal, OptionContract]:
        if option_type not in ("CE", "PE"):
            raise ValueError(f"option_type must be CE or PE, got {option_type!r}")
        spot = self.get_spot(underlying)
        step = self.strike_step
        atm = round(spot / step) * step
        chain: dict[Decimal, OptionContract] = {}
        for i in range(-self.strikes_each_side, self.strikes_each_side + 1):
            strike_val = atm + i * step
            if strike_val <= 0:
                continue
            strike = Decimal(str(strike_val))
            token = f"FEED-{underlying}-{strike_val}-{option_type}"
            symbol = f"{underlying}{expiry.strftime('%y%m')}{strike_val}{option_type}"
            chain[strike] = OptionContract(
                instrument_token=token,
                trading_symbol=symbol,
                underlying=underlying,
                expiry=expiry,
                strike=strike,
                option_type=OptionType.CE if option_type == "CE" else OptionType.PE,
                lot_size=self.lot_size,
                metadata={
                    "pricing_feed": self.feed.source_tag,
                    "model": not self.feed.data_source_real,
                    "spot_at_gen": spot,
                },
            )
        return chain

    def register_chain(self, chain: Any) -> None:
        contracts = chain.values() if hasattr(chain, "values") else chain
        for contract in contracts:
            self._contracts[str(contract.instrument_token)] = contract

    def register_contract(self, contract: OptionContract) -> None:
        self._contracts[str(contract.instrument_token)] = contract

    def invalidate(self, instrument_token: str | None = None) -> None:
        return None

    # -- QuoteProvider protocol ------------------------------------------

    def get_quote(self, instrument_token: str) -> dict[str, Any]:
        contract = self._contracts.get(instrument_token)
        if contract is None:
            return {"ltp": 0.0, "bid": 0.0, "ask": 0.0, "error": "unknown token"}
        option_type = (
            contract.option_type.value
            if hasattr(contract.option_type, "value")
            else str(contract.option_type)
        )
        quote = self.feed.quote(
            underlying=contract.underlying,
            strike=float(contract.strike),
            expiry=contract.expiry,
            option_type=option_type,
            spot=self.get_spot(contract.underlying),
            bar_time=self._reference,
        )
        ltp = max(float(quote.mid), MIN_QUOTE_TICK)
        return {
            "ltp": ltp,
            "bid": max(float(quote.bid), MIN_QUOTE_TICK),
            "ask": max(float(quote.ask), ltp),
            "volume": 0,
            "oi": 0,
            "pricing_feed": quote.source_tag,
            "model": not self.feed.data_source_real,
        }

    # -- SettlementPriceProvider protocol --------------------------------

    def get_settlement_price(self, underlying: str) -> float:
        closes = self._session_closes.get(underlying) or [self.get_spot(underlying)]
        return float(self.feed.expiry_settlement(pd.Series(closes, dtype=float)))