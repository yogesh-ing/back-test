"""Strategy intent models — the bridge between equity signals and options execution.

Strategies emit a :class:`MarketView` (directional intent), which the expression
layer converts into a :class:`TradeIntent` (specific option structure with legs).

Usage::

    from backtest.strategy.intent import MarketView, Direction, TradeIntent

    view = MarketView(
        direction=Direction.BULLISH,
        confidence=0.8,
        underlying="NIFTY",
        target_price=Decimal("25000"),
    )

    # After expression layer processes the view:
    intent = TradeIntent(
        view=view,
        structure_type="bull_call_spread",
        legs=[...],
        expiry=date(2026, 9, 24),
    )
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Any


class Direction(Enum):
    """Directional bias emitted by a strategy."""

    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"


@dataclass(frozen=True)
class MarketView:
    """A strategy's directional intent for a given bar.

    This is the output of ``Strategy.generate_market_view()`` and the input
    to the expression layer.  Strategies that only emit equity signals
    (``generate_signals``) can optionally override ``generate_market_view``
    to express directional bias for options trading.

    Parameters
    ----------
    direction:
        The directional bias (bullish / bearish / neutral).
    confidence:
        Strength of the conviction, 0.0 (no conviction) to 1.0 (certain).
    underlying:
        The index name (``"NIFTY"`` or ``"BANKNIFTY"``).
    spot_price:
        Current spot/underlying price at the time of the signal.
    target_price:
        Optional target price — used by some strategies to influence strike selection.
    stop_price:
        Optional stop price — used for risk-aware strike selection.
    bar_timestamp:
        The timestamp of the bar that produced this view (for audit trail).
    metadata:
        Arbitrary extra data the strategy wants to pass through (e.g. RSI value,
        SMA gap).  Expression selectors can use this for smarter choices.
    """

    direction: Direction
    confidence: float = 0.5
    underlying: str = "NIFTY"
    spot_price: Decimal = Decimal("0")
    target_price: Decimal | None = None
    stop_price: Decimal | None = None
    bar_timestamp: Any = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(
                f"confidence must be in [0.0, 1.0], got {self.confidence}"
            )
        if self.direction == Direction.NEUTRAL:
            # Neutral views should not generate option trades;
            # the expression layer skips them.
            pass


@dataclass(frozen=True)
class OptionLeg:
    """A single leg in a multi-leg option trade.

    Parameters
    ----------
    instrument_token:
        Exchange token for the specific contract.
    trading_symbol:
        Human-readable symbol (e.g. ``"NIFTY26SEP24500CE"``).
    side:
        ``"BUY"`` or ``"SELL"``.
    quantity:
        Number of lots.
    lot_size:
        Lot size of the contract.
    """

    instrument_token: str
    trading_symbol: str
    side: str  # "BUY" or "SELL"
    quantity: int  # number of lots
    lot_size: int

    def __post_init__(self) -> None:
        if self.side not in ("BUY", "SELL"):
            raise ValueError(f"side must be BUY or SELL, got '{self.side}'")
        if self.quantity < 1:
            raise ValueError(f"quantity must be >= 1, got {self.quantity}")
        if self.lot_size < 1:
            raise ValueError(f"lot_size must be >= 1, got {self.lot_size}")

    @property
    def total_quantity(self) -> int:
        """Total number of shares/units (quantity × lot_size)."""
        return self.quantity * self.lot_size


@dataclass(frozen=True)
class TradeIntent:
    """The expression layer's output — a ready-to-execute option structure.

    This is what gets sent to the broker (paper or live) for execution.

    Parameters
    ----------
    view:
        The original ``MarketView`` that triggered this trade.
    structure_type:
        Name of the option structure (e.g. ``"long_call"``,
        ``"bull_call_spread"``).
    legs:
        The individual option legs to execute.
    expiry:
        The expiry date for all legs.
    estimated_premium:
        Model-estimated net premium (in ₹). Positive = debit (you pay),
        negative = credit (you receive), ``0`` = not estimated.  Populated
        by the structure builders from the pricing model — an *estimate*,
        not the fill price the broker actually gets.
    estimated_margin:
        Estimated margin requirement (in ₹).  Not yet populated by the
        builders (V1 leaves it at ``0``).
    strategy_name:
        The strategy that generated the original view (for logging / audit).
    metadata:
        Arbitrary extra data (e.g. Greeks estimates, selector choices).
    """

    view: MarketView
    structure_type: str
    legs: tuple[OptionLeg, ...]
    expiry: date
    estimated_premium: Decimal = Decimal("0")
    estimated_margin: Decimal = Decimal("0")
    strategy_name: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.legs:
            raise ValueError("TradeIntent must have at least one leg")
        if len(self.legs) > 4:
            raise ValueError(
                f"V1 supports max 4 legs, got {len(self.legs)}"
            )
        if self.structure_type not in _VALID_STRUCTURES:
            raise ValueError(
                f"Unknown structure_type '{self.structure_type}'. "
                f"Valid: {sorted(_VALID_STRUCTURES)}"
            )

    @property
    def is_multi_leg(self) -> bool:
        """True if the structure has more than one leg."""
        return len(self.legs) > 1

    @property
    def is_debit(self) -> bool:
        """True when the structure costs money to open (a net debit).

        Derived from :attr:`estimated_premium`, which structure builders
        populate from the pricing model.  A structure built without an
        estimate reads as a debit (the default of ``0`` is not a credit).
        """
        return self.estimated_premium >= Decimal("0")


# Valid structure types (V1 scope)
_VALID_STRUCTURES: frozenset[str] = frozenset({
    "long_call",
    "long_put",
    "bull_call_spread",
    "bear_put_spread",
    "straddle",
    "strangle",
    "iron_condor",
    "calendar_spread",
})
