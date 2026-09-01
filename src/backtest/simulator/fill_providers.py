"""Pluggable fill providers (ticket P3.3).

The fill provider is the ONLY code path that differs between paper and
live:

* :class:`SimulatedFillProvider` — paper: prices the fill at the supplied
  touch (the next bar's open on the bar clock) through the slippage and
  fee engines, with simulated latency.
* :class:`BrokerFillProvider` — live: sends the order to the broker and
  returns what the broker ACTUALLY filled.

Everything ABOVE the fill — portfolio, positions, risk, metrics — is
shared by both, which is what keeps live P&L comparable to paper P&L
(same math, real fills).

Determinism: providers make no random decisions of their own — every draw
goes through the run's single seeded generator, which the executor passes
in as the ``rng`` argument of :meth:`FillProvider.get_fill`. A provider
with its own RNG would desynchronise runs and break replay comparability.
"""

from __future__ import annotations

import random
from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Mapping

from backtest.simulator.enums import OrderType
from backtest.simulator.errors import ValidationError
from backtest.simulator.fill import Fill, LiquidityFlag
from backtest.simulator.money import ZERO, quantize_price, to_decimal

if TYPE_CHECKING:  # pragma: no cover
    from backtest.simulator.execution import ExecutionConfig
    from backtest.simulator.fees import CommissionCalculator
    from backtest.simulator.order import Order
    from backtest.simulator.slippage import SlippageCalculator

__all__ = [
    "FillDecision",
    "FillProvider",
    "SimulatedFillProvider",
    "BrokerFillProvider",
    "simulate_latency",
    "apply_price_improvement",
]

_DUST = Decimal("0.00000001")


def simulate_latency(
    config: "ExecutionConfig",
    rng: random.Random,
    min_ms: Any = None,
    max_ms: Any = None,
) -> Decimal:
    """Draw a fill latency in milliseconds (canonical implementation).

    Reported rather than slept on: a simulator that actually waited
    500 ms per order would take hours to replay a day.
    """
    low = to_decimal(min_ms if min_ms is not None else config.min_latency_ms, "min_ms")
    high = to_decimal(max_ms if max_ms is not None else config.max_latency_ms, "max_ms")
    if high < low:
        raise ValidationError("max_ms must be >= min_ms", code="invalid_latency_range")
    if high == low:
        return low
    span = high - low
    return (low + span * Decimal(str(rng.random()))).quantize(Decimal("0.001"))


def apply_price_improvement(
    order: "Order", price: Decimal, config: "ExecutionConfig", rng: random.Random
) -> Decimal:
    """Occasionally fill better than expected, as a real venue might
    (canonical implementation)."""
    chance = float(config.price_improvement_probability)
    if chance <= 0 or rng.random() >= chance:
        return price
    delta = price * config.price_improvement_bps / Decimal("10000")
    improved = price - delta if order.is_buy else price + delta
    return quantize_price(max(improved, _DUST))


@dataclass(frozen=True)
class FillDecision:
    """What a :class:`FillProvider` decided for one execution attempt.

    ``fill`` is ``None`` when the provider reports "nothing traded" (a
    live broker that could not execute); the executor maps that to a
    NO_FILL result rather than an error.
    """

    fill: Fill | None
    latency_ms: Decimal = ZERO


class FillProvider(ABC):
    """The paper/live seam (ticket P3.3).

    One method: given an order that already passed all pre-trade checks
    (gating, liquidity cap, queue risk — all owned by the executor),
    produce the fill — or say nothing traded.

    Parameters
    ----------
    order:
        The working order (the provider must not mutate its status; the
        executor applies the fill).
    market_data:
        The market snapshot (tick quote, or the bar-open snapshot on the
        bar clock). A live provider may ignore it — the broker has the
        real market.
    quantity:
        How much the executor allows to trade (liquidity/participation
        already applied).
    rng:
        The run's single seeded generator — see module docstring.
    """

    @abstractmethod
    def get_fill(
        self,
        order: "Order",
        market_data: Mapping[str, Any] | Any,
        quantity: Any,
        rng: random.Random,
    ) -> FillDecision:
        """Produce the fill for one execution attempt."""


class SimulatedFillProvider(FillProvider):
    """Paper — fills at the supplied touch + slippage/fee models.

    This is the default provider: an :class:`OrderExecutor` built without
    one behaves exactly as before ticket P3.3 (same formulas, same RNG
    draw order, so existing runs are bit-identical).
    """

    def __init__(
        self,
        slippage: "SlippageCalculator",
        fees: "CommissionCalculator",
        config: "ExecutionConfig",
    ) -> None:
        self.slippage = slippage
        self.fees = fees
        # Live reference — improvement probabilities / segment are read at
        # fill time, so executor.config mutations still apply.
        self.config = config

    def get_fill(
        self,
        order: "Order",
        market_data: Mapping[str, Any] | Any,
        quantity: Any,
        rng: random.Random,
    ) -> FillDecision:
        reference = order.calculate_fill_price(market_data)
        estimate = self.slippage.calculate_slippage(
            order, market_data, reference_price=reference, quantity=quantity
        )
        price = apply_price_improvement(order, estimate.executed_price, self.config, rng)

        # A limit order can never fill worse than its limit, whatever the
        # slippage model says. Belt and braces: the calculator caps too.
        if order.limit_price is not None and order.order_type in (
            OrderType.LIMIT,
            OrderType.STOP_LIMIT,
        ):
            price = min(price, order.limit_price) if order.is_buy else max(price, order.limit_price)

        breakdown = self.fees.calculate(
            quantity=quantity,
            fill_price=price,
            side=order.side,
            segment=self.config.segment,
        )

        latency = simulate_latency(self.config, rng)
        fill = Fill(
            symbol=order.symbol,
            side=order.side,
            quantity=quantity,
            fill_price=price,
            order_id=order.order_id,
            reference_price=reference,
            liquidity_flag=(
                LiquidityFlag.MAKER
                if order.order_type in (OrderType.LIMIT, OrderType.STOP_LIMIT)
                else LiquidityFlag.TAKER
            ),
            strategy_name=order.strategy_name,
            **breakdown.as_fill_kwargs(),
        )
        return FillDecision(fill=fill, latency_ms=latency)


class BrokerFillProvider(FillProvider):
    """Live — sends to the broker, gets the REAL fill back.

    The broker is duck-typed with the two methods this provider needs
    (a superset of :class:`backtest.brokers.base.BrokerOrderBase`, whose
    pinned five-method contract is extended by live broker adapters):

    * ``place_order(order) -> <broker order id>``
    * ``poll_fill(broker_order_id) -> Mapping | None`` — the broker's
      raw fill row (mStock TypeA keys or generic aliases, see
      :meth:`Fill.from_broker`), or ``None`` when nothing executed.

    The ``market_data`` snapshot is deliberately unused: a live fill is
    whatever the venue actually did, at whatever price it actually paid.
    Latency is not simulated for real fills.
    """

    def __init__(self, broker: Any) -> None:
        self.broker = broker

    def get_fill(
        self,
        order: "Order",
        market_data: Mapping[str, Any] | Any,
        quantity: Any,
        rng: random.Random,
    ) -> FillDecision:
        # Place ONCE per order. ``order.broker_order_id`` is stamped at
        # place time and survives state-file round-trips (T7), so a retry
        # (order still working after an unfilled poll) and a RESUMED order
        # poll the SAME broker order instead of double-placing at the venue.
        broker_order_id = order.broker_order_id
        if broker_order_id is None:
            broker_order_id = self.broker.place_order(order)
            order.broker_order_id = str(broker_order_id)
        raw = self.broker.poll_fill(broker_order_id)
        if raw is None:
            return FillDecision(fill=None)
        if not isinstance(raw, Mapping):
            raise ValidationError(
                f"broker poll_fill must return a mapping or None, got {type(raw).__name__}",
                code="invalid_broker_fill",
            )
        fill = Fill.from_broker(
            raw,
            broker_order_id=broker_order_id,
            order_id=order.order_id,
            strategy_name=order.strategy_name,
        )
        return FillDecision(fill=fill)
