"""The pricing-feed interface — the only pricing surface the options
backtest driver may depend on (plan V3 §3, rule 1).

Concrete feeds live elsewhere (:class:`~backtest.options.model_pricing.ModelBSFeed`
now, a recorded-chain feed later). The driver imports this module, never a
feed implementation, ``bs_price`` or ``quote_providers`` — enforced by an
import-graph test so a "temporary" hardcoded price call cannot sneak in.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Protocol, runtime_checkable

import pandas as pd

__all__ = ["OptionPricingFeed", "LegQuote"]


@dataclass(frozen=True)
class LegQuote:
    """One leg's quote at one bar. Money is Decimal, greeks are float.

    ``theta`` is per year and ``vega`` per 1 vol point (the
    :class:`~backtest.options.greeks.OptionGreeks` convention).
    ``bar_time`` pins the quote for deterministic replay debugging.
    """

    mid: Decimal
    bid: Decimal
    ask: Decimal
    iv: float
    delta: float
    gamma: float
    theta: float
    vega: float
    source_tag: str
    bar_time: datetime


@runtime_checkable
class OptionPricingFeed(Protocol):
    """How one option leg is priced, independent of where the price comes from.

    ``source_tag`` names the feed on every quote and result; a result that
    cannot state its feed is a bug. ``data_source_real`` is True only for
    feeds replaying recorded market premiums — model feeds are False always.
    """

    source_tag: str
    data_source_real: bool

    def set_reference(self, bar_time: datetime) -> None:
        """Pin quote generation to a bar time (determinism)."""
        ...

    def quote(
        self,
        *,
        underlying: str,
        strike: float,
        expiry: date,
        option_type: str,
        spot: float,
        bar_time: datetime | None = None,
    ) -> LegQuote:
        """Price one leg. ``bar_time`` defaults to the pinned reference."""
        ...

    def expiry_settlement(
        self,
        session_closes: pd.Series,
        volumes: pd.Series | None = None,
    ) -> Decimal:
        """Settlement price for the expiry session (VWAP of the last 30 bars)."""
        ...