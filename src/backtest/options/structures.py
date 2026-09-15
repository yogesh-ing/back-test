"""Option structure builders — convert a view + strike into executable legs.

Each structure class takes a :class:`MarketView`, selected strikes, and an
option chain, and produces a :class:`TradeIntent` with the specific legs.

Usage::

    from backtest.options.structures import BullCallSpread

    structure = BullCallSpread()
    intent = structure.build(
        view=bullish_view,
        long_strike=Decimal("24800"),
        short_strike=Decimal("25000"),
        chain_data={...},
        expiry=date(2026, 9, 24),
    )

V1 scope: ``LongCall``, ``LongPut``, ``BullCallSpread``, ``BearPutSpread``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from backtest.instruments.option import OptionContract
from backtest.options.quote_providers import bs_price
from backtest.strategy.intent import (
    Direction,
    MarketView,
    OptionLeg,
    TradeIntent,
)


# ---------------------------------------------------------------------------
# Base class
# ---------------------------------------------------------------------------

class OptionStructure(ABC):
    """Base class for option structure builders.

    Subclasses implement :meth:`build` to produce a :class:`TradeIntent`
    from a view and selected strikes.
    """

    name: str = ""
    description: str = ""
    min_legs: int = 1
    max_legs: int = 1

    @abstractmethod
    def build(
        self,
        view: MarketView,
        strikes: list[Decimal],
        chain: dict[Decimal, OptionContract],
        expiry: date,
        strategy_name: str = "",
    ) -> TradeIntent:
        """Build a TradeIntent from the given parameters.

        Parameters
        ----------
        view:
            The directional view from the strategy.
        strikes:
            Selected strike(s) from the selector layer.
        chain:
            Available contracts keyed by strike (one expiry only).
        expiry:
            The expiry date for all legs.
        strategy_name:
            Name of the originating strategy (for audit).
        """
        ...

    def _get_contract(
        self,
        strike: Decimal,
        option_type: str,
        chain: dict[Decimal, OptionContract],
    ) -> OptionContract:
        """Look up a contract from the chain by strike and type.

        Raises ``ValueError`` if not found.
        """
        # Chain is keyed by strike; look for the matching option_type
        # In V1, chain is flat: {strike: contract}
        # For multi-type chains, we may need to extend this.
        contract = chain.get(strike)
        if contract is None:
            raise ValueError(
                f"No contract found for strike {strike} (option_type={option_type})"
            )
        return contract

    def _build_leg(
        self,
        contract: OptionContract,
        side: str,
        quantity: int = 1,
    ) -> OptionLeg:
        """Build an OptionLeg from a contract."""
        return OptionLeg(
            instrument_token=contract.instrument_token,
            trading_symbol=contract.trading_symbol,
            side=side,
            quantity=quantity,
            lot_size=contract.lot_size,
        )

    def _estimate_net_premium(
        self,
        priced_legs: list[tuple[OptionContract, OptionLeg]],
        view: MarketView,
        expiry: date,
    ) -> Decimal:
        """Model-estimate the structure's net premium (T0.1).

        Prices each leg with the same Black-Scholes kernel the synthetic
        quote provider uses, so :attr:`TradeIntent.estimated_premium` is a
        sensible pre-trade number instead of a hard ``0``.

        Positive = net debit (you pay), negative = net credit.

        Returns ``Decimal("0")`` — "not estimated" — when the estimate is
        impossible, rather than guessing: no spot on the view, no bar
        timestamp (dates stay deterministic — this never falls back to
        ``date.today()``), a non-positive time to expiry, or a contract
        carrying no modelled vol (e.g. a real vendor chain).
        """
        spot = float(view.spot_price or 0)
        if spot <= 0 or view.bar_timestamp is None:
            return Decimal("0")

        as_of_date = getattr(view.bar_timestamp, "date", lambda: view.bar_timestamp)()
        try:
            dte = (expiry - as_of_date).days
        except TypeError:
            return Decimal("0")
        if dte <= 0:
            return Decimal("0")
        years = dte / 365.0

        net = Decimal("0")
        for contract, leg in priced_legs:
            vol = contract.metadata.get("vol")
            if not vol:
                continue
            option_type = (
                contract.option_type.value
                if hasattr(contract.option_type, "value")
                else str(contract.option_type)
            )
            premium = bs_price(
                spot, float(contract.strike), years, float(vol), option_type
            )
            value = Decimal(str(premium)) * Decimal(str(leg.total_quantity))
            net += value if leg.side == "BUY" else -value

        return net.quantize(Decimal("0.01"))


# ---------------------------------------------------------------------------
# Long Call
# ---------------------------------------------------------------------------

class LongCall(OptionStructure):
    """Buy a call option — bullish directional trade.

    Maximum loss: premium paid.
    Maximum gain: unlimited (theoretically).
    Breakeven: strike + premium.
    """

    name = "long_call"
    description = "Long call — buy a call option for unlimited upside."
    min_legs = 1
    max_legs = 1

    def build(
        self,
        view: MarketView,
        strikes: list[Decimal],
        chain: dict[Decimal, OptionContract],
        expiry: date,
        strategy_name: str = "",
    ) -> TradeIntent:
        if not strikes:
            raise ValueError("LongCall requires at least one strike")

        strike = strikes[0]
        contract = self._get_contract(strike, "CE", chain)
        leg = self._build_leg(contract, "BUY")

        return TradeIntent(
            view=view,
            structure_type="long_call",
            estimated_premium=self._estimate_net_premium(
                [(contract, leg)], view, expiry
            ),
            legs=(leg,),
            expiry=expiry,
            strategy_name=strategy_name,
            metadata={
                "strike": str(strike),
                "option_type": "CE",
                # Per-leg strike map keyed by trading symbol — the paper
                # broker reads this to stamp strikes on each position.
                "strikes": {leg.trading_symbol: str(strike)},
            },
        )


# ---------------------------------------------------------------------------
# Long Put
# ---------------------------------------------------------------------------

class LongPut(OptionStructure):
    """Buy a put option — bearish directional trade.

    Maximum loss: premium paid.
    Maximum gain: strike - premium (stock goes to zero).
    Breakeven: strike - premium.
    """

    name = "long_put"
    description = "Long put — buy a put option for bearish exposure."
    min_legs = 1
    max_legs = 1

    def build(
        self,
        view: MarketView,
        strikes: list[Decimal],
        chain: dict[Decimal, OptionContract],
        expiry: date,
        strategy_name: str = "",
    ) -> TradeIntent:
        if not strikes:
            raise ValueError("LongPut requires at least one strike")

        strike = strikes[0]
        contract = self._get_contract(strike, "PE", chain)
        leg = self._build_leg(contract, "BUY")

        return TradeIntent(
            view=view,
            structure_type="long_put",
            estimated_premium=self._estimate_net_premium(
                [(contract, leg)], view, expiry
            ),
            legs=(leg,),
            expiry=expiry,
            strategy_name=strategy_name,
            metadata={
                "strike": str(strike),
                "option_type": "PE",
                "strikes": {leg.trading_symbol: str(strike)},
            },
        )


# ---------------------------------------------------------------------------
# Bull Call Spread
# ---------------------------------------------------------------------------

class BullCallSpread(OptionStructure):
    """Buy lower-strike call + sell higher-strike call — limited bullish trade.

    Maximum loss: net premium paid (debit).
    Maximum gain: difference between strikes - net premium.
    Breakeven: long strike + net premium.
    """

    name = "bull_call_spread"
    description = (
        "Bull call spread — buy ITM call, sell OTM call for capped upside."
    )
    min_legs = 2
    max_legs = 2

    def build(
        self,
        view: MarketView,
        strikes: list[Decimal],
        chain: dict[Decimal, OptionContract],
        expiry: date,
        strategy_name: str = "",
    ) -> TradeIntent:
        if len(strikes) < 2:
            raise ValueError("BullCallSpread requires exactly 2 strikes")

        long_strike, short_strike = sorted(strikes)[:2]

        long_contract = self._get_contract(long_strike, "CE", chain)
        short_contract = self._get_contract(short_strike, "CE", chain)

        long_leg = self._build_leg(long_contract, "BUY")
        short_leg = self._build_leg(short_contract, "SELL")

        return TradeIntent(
            view=view,
            structure_type="bull_call_spread",
            estimated_premium=self._estimate_net_premium(
                [(long_contract, long_leg), (short_contract, short_leg)],
                view,
                expiry,
            ),
            legs=(long_leg, short_leg),
            expiry=expiry,
            strategy_name=strategy_name,
            metadata={
                "long_strike": str(long_strike),
                "short_strike": str(short_strike),
                "spread_width": str(short_strike - long_strike),
                "option_type": "CE",
                "strikes": {
                    long_leg.trading_symbol: str(long_strike),
                    short_leg.trading_symbol: str(short_strike),
                },
            },
        )


# ---------------------------------------------------------------------------
# Bear Put Spread
# ---------------------------------------------------------------------------

class BearPutSpread(OptionStructure):
    """Buy higher-strike put + sell lower-strike put — limited bearish trade.

    Maximum loss: net premium paid (debit).
    Maximum gain: difference between strikes - net premium.
    Breakeven: long strike - net premium.
    """

    name = "bear_put_spread"
    description = (
        "Bear put spread — buy OTM put, sell ITM put for capped downside."
    )
    min_legs = 2
    max_legs = 2

    def build(
        self,
        view: MarketView,
        strikes: list[Decimal],
        chain: dict[Decimal, OptionContract],
        expiry: date,
        strategy_name: str = "",
    ) -> TradeIntent:
        if len(strikes) < 2:
            raise ValueError("BearPutSpread requires exactly 2 strikes")

        long_strike, short_strike = sorted(strikes, reverse=True)[:2]
        # long_strike > short_strike for puts

        long_contract = self._get_contract(long_strike, "PE", chain)
        short_contract = self._get_contract(short_strike, "PE", chain)

        long_leg = self._build_leg(long_contract, "BUY")
        short_leg = self._build_leg(short_contract, "SELL")

        return TradeIntent(
            view=view,
            structure_type="bear_put_spread",
            estimated_premium=self._estimate_net_premium(
                [(long_contract, long_leg), (short_contract, short_leg)],
                view,
                expiry,
            ),
            legs=(long_leg, short_leg),
            expiry=expiry,
            strategy_name=strategy_name,
            metadata={
                "long_strike": str(long_strike),
                "short_strike": str(short_strike),
                "spread_width": str(long_strike - short_strike),
                "option_type": "PE",
                "strikes": {
                    long_leg.trading_symbol: str(long_strike),
                    short_leg.trading_symbol: str(short_strike),
                },
            },
        )


# ---------------------------------------------------------------------------
# Structure factory
# ---------------------------------------------------------------------------

def create_structure(structure_type: str) -> OptionStructure:
    """Create a structure builder by name.

    Parameters
    ----------
    structure_type:
        One of ``"long_call"``, ``"long_put"``, ``"bull_call_spread"``,
        ``"bear_put_spread"``.
    """
    structures = {
        "long_call": LongCall,
        "long_put": LongPut,
        "bull_call_spread": BullCallSpread,
        "bear_put_spread": BearPutSpread,
    }
    cls = structures.get(structure_type)
    if cls is None:
        raise ValueError(
            f"Unknown structure_type '{structure_type}'. "
            f"Valid: {sorted(structures.keys())}"
        )
    return cls()
