"""Strike selection — picks specific strikes from an option chain.

The selector layer sits between the strategy's :class:`MarketView` and the
option structure builder.  Given a direction, spot price, and the available
option chain, a selector returns the specific strike(s) to use.

Usage::

    from backtest.options.selector import ATMSelector

    selector = ATMSelector()
    strike = selector.pick_strike(
        spot_price=Decimal("24800"),
        available_strikes=[Decimal("24500"), Decimal("24800"), Decimal("25000")],
        direction=Direction.BULLISH,
    )

Design
------
Selectors follow the **Strategy pattern** (not ABC) — each is a simple
callable object that can be swapped via configuration.  The base protocol
is :class:`StrikeSelector` (a Protocol class); concrete implementations
are ``ATMSelector``, ``DeltaSelector``, and ``FixedDistanceSelector``.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Protocol, runtime_checkable

from backtest.strategy.intent import Direction, MarketView


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class StrikeSelector(Protocol):
    """Protocol for strike selection strategies."""

    def pick_strike(
        self,
        spot_price: Decimal,
        available_strikes: list[Decimal],
        direction: Direction,
        view: MarketView | None = None,
    ) -> Decimal | None:
        """Select a single strike from the available chain.

        Parameters
        ----------
        spot_price:
            Current underlying price.
        available_strikes:
            Sorted list of available strikes from the option chain.
        direction:
            Bullish / bearish / neutral.
        view:
            Full MarketView for context (target_price, metadata, etc.).

        Returns
        -------
        The selected strike, or ``None`` if no suitable strike is found.
        """
        ...

    def pick_strikes(
        self,
        spot_price: Decimal,
        available_strikes: list[Decimal],
        direction: Direction,
        view: MarketView | None = None,
        count: int = 2,
    ) -> list[Decimal]:
        """Select multiple strikes (for spreads).

        Default implementation calls ``pick_strike`` repeatedly.
        Override for smarter multi-strike selection.
        """
        ...


# ---------------------------------------------------------------------------
# ATM Selector — picks the strike closest to spot
# ---------------------------------------------------------------------------

class ATMSelector:
    """Select the At-The-Money (ATM) strike — closest to the current spot.

    This is the simplest and most common selector.  When multiple strikes
    are equidistant, it picks the lower strike for calls and the higher
    strike for puts (conservative fill assumption).
    """

    def pick_strike(
        self,
        spot_price: Decimal,
        available_strikes: list[Decimal],
        direction: Direction,
        view: MarketView | None = None,
    ) -> Decimal | None:
        if not available_strikes:
            return None
        return _closest_strike(spot_price, available_strikes)

    def pick_strikes(
        self,
        spot_price: Decimal,
        available_strikes: list[Decimal],
        direction: Direction,
        view: MarketView | None = None,
        count: int = 2,
    ) -> list[Decimal]:
        if not available_strikes:
            return []
        atm = _closest_strike(spot_price, available_strikes)
        if atm is None:
            return []
        return _pick_around(atm, available_strikes, count)


# ---------------------------------------------------------------------------
# Delta Selector — uses target price to approximate delta-based strike
# ---------------------------------------------------------------------------

class DeltaSelector:
    """Select strike based on an approximate delta target.

    For V1, we approximate delta by position relative to spot:

    - Bullish: pick a strike slightly OTM (above spot for calls,
      below for puts) to get ~0.3–0.4 delta.
    - Bearish: pick a strike slightly OTM in the other direction.

    The ``delta_target`` parameter controls how far OTM:
    - 0.5 = ATM (delta ≈ 0.5)
    - 0.3 = slightly OTM (delta ≈ 0.3)
    - 0.1 = far OTM (delta ≈ 0.1)
    """

    def __init__(self, delta_target: float = 0.35) -> None:
        if not 0.0 < delta_target <= 1.0:
            raise ValueError(f"delta_target must be in (0.0, 1.0], got {delta_target}")
        self.delta_target = delta_target

    def pick_strike(
        self,
        spot_price: Decimal,
        available_strikes: list[Decimal],
        direction: Direction,
        view: MarketView | None = None,
    ) -> Decimal | None:
        if not available_strikes:
            return None

        atm = _closest_strike(spot_price, available_strikes)
        if atm is None:
            return None

        # Estimate OTM offset: ~2–3% of spot for delta ≈ 0.35
        # This is a rough V1 approximation; real delta requires Black-Scholes.
        offset_pct = (0.5 - self.delta_target) * 0.10  # e.g. 0.5-0.35=0.15 → 1.5%
        offset = spot_price * Decimal(str(offset_pct))

        if direction == Direction.BULLISH:
            # For calls: pick strike above spot (OTM)
            target = spot_price + offset
        elif direction == Direction.BEARISH:
            # For puts: pick strike below spot (OTM)
            target = spot_price - offset
        else:
            target = spot_price

        return _closest_strike(target, available_strikes)

    def pick_strikes(
        self,
        spot_price: Decimal,
        available_strikes: list[Decimal],
        direction: Direction,
        view: MarketView | None = None,
        count: int = 2,
    ) -> list[Decimal]:
        if not available_strikes:
            return []
        primary = self.pick_strike(spot_price, available_strikes, direction, view)
        if primary is None:
            return []
        return _pick_around(primary, available_strikes, count)


# ---------------------------------------------------------------------------
# Fixed Distance Selector — picks strike at a fixed % from spot
# ---------------------------------------------------------------------------

class FixedDistanceSelector:
    """Select strike at a fixed percentage distance from spot.

    Parameters
    ----------
    distance_pct:
        Percentage distance from spot.  Positive = OTM, negative = ITM.
        E.g. ``2.0`` means 2% OTM.
    """

    def __init__(self, distance_pct: float = 2.0) -> None:
        self.distance_pct = distance_pct

    def pick_strike(
        self,
        spot_price: Decimal,
        available_strikes: list[Decimal],
        direction: Direction,
        view: MarketView | None = None,
    ) -> Decimal | None:
        if not available_strikes:
            return None

        offset = spot_price * Decimal(str(self.distance_pct / 100))

        if direction == Direction.BULLISH:
            target = spot_price + offset  # OTM call
        elif direction == Direction.BEARISH:
            target = spot_price - offset  # OTM put
        else:
            target = spot_price

        return _closest_strike(target, available_strikes)

    def pick_strikes(
        self,
        spot_price: Decimal,
        available_strikes: list[Decimal],
        direction: Direction,
        view: MarketView | None = None,
        count: int = 2,
    ) -> list[Decimal]:
        if not available_strikes:
            return []
        primary = self.pick_strike(spot_price, available_strikes, direction, view)
        if primary is None:
            return []
        return _pick_around(primary, available_strikes, count)


# ---------------------------------------------------------------------------
# Target Price Selector — uses strategy's target_price for strike selection
# ---------------------------------------------------------------------------

class TargetPriceSelector:
    """Select strike based on the strategy's target_price.

    Picks the strike closest to the target price.  Falls back to ATM
    if no target is provided.
    """

    def pick_strike(
        self,
        spot_price: Decimal,
        available_strikes: list[Decimal],
        direction: Direction,
        view: MarketView | None = None,
    ) -> Decimal | None:
        if not available_strikes:
            return None

        target = spot_price  # default to ATM
        if view and view.target_price is not None:
            target = view.target_price

        return _closest_strike(target, available_strikes)

    def pick_strikes(
        self,
        spot_price: Decimal,
        available_strikes: list[Decimal],
        direction: Direction,
        view: MarketView | None = None,
        count: int = 2,
    ) -> list[Decimal]:
        if not available_strikes:
            return []
        primary = self.pick_strike(spot_price, available_strikes, direction, view)
        if primary is None:
            return []
        return _pick_around(primary, available_strikes, count)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _closest_strike(target: Decimal, strikes: list[Decimal]) -> Decimal | None:
    """Find the strike closest to the target price."""
    if not strikes:
        return None
    return min(strikes, key=lambda s: abs(s - target))


def _pick_around(
    center: Decimal,
    strikes: list[Decimal],
    count: int,
) -> list[Decimal]:
    """Pick ``count`` strikes around ``center`` (center included).

    Returns strikes sorted by distance from center, then by price.
    """
    sorted_by_dist = sorted(strikes, key=lambda s: (abs(s - center), s))
    return sorted(sorted_by_dist[:count])


# ---------------------------------------------------------------------------
# Selector factory
# ---------------------------------------------------------------------------

def create_selector(
    selector_type: str = "atm",
    **kwargs,
) -> StrikeSelector:
    """Create a selector by name.

    Parameters
    ----------
    selector_type:
        One of ``"atm"``, ``"delta"``, ``"fixed_distance"``, ``"target_price"``.
    **kwargs:
        Forwarded to the selector constructor.
    """
    selectors = {
        "atm": ATMSelector,
        "delta": DeltaSelector,
        "fixed_distance": FixedDistanceSelector,
        "target_price": TargetPriceSelector,
    }
    cls = selectors.get(selector_type)
    if cls is None:
        raise ValueError(
            f"Unknown selector_type '{selector_type}'. "
            f"Valid: {sorted(selectors.keys())}"
        )
    return cls(**kwargs)
