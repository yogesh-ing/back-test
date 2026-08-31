"""Ticket P3.3 — pluggable fill providers in OrderExecutor.

The provider is the ONLY paper/live seam: simulated pricing by default,
a live broker's REAL fill when injected. Everything above the fill
(portfolio, position, risk, metrics) is shared.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal as D

from backtest.simulator.engine_loop import Bar
from backtest.simulator.enums import OrderSide, OrderType, TimeInForce
from backtest.simulator.execution import (
    BrokerFillProvider,
    ExecutionConfig,
    FillDecision,
    FillProvider,
    OrderExecutor,
    SimulatedFillProvider,
)
from backtest.simulator.fees import BrokerProfile, CommissionCalculator
from backtest.simulator.commission import FlatCommission
from backtest.simulator.fill import Fill
from backtest.simulator.fees import NoStatutoryFees
from backtest.simulator.order import Order
from backtest.simulator.portfolio import Portfolio
from backtest.simulator.slippage import SlippageCalculator


def _config(**overrides) -> ExecutionConfig:
    base = dict(
        seed=7,
        price_improvement_probability=D("0"),
        min_latency_ms=D("0"),
        max_latency_ms=D("0"),
        max_participation=D("1"),
    )
    base.update(overrides)
    return ExecutionConfig(**base)


def _flat_fees(per_trade: str = "20") -> CommissionCalculator:
    return CommissionCalculator(
        broker=BrokerProfile(
            name="flat",
            commission_model=FlatCommission(per_trade=D(per_trade)),
            fee_schedule=NoStatutoryFees(),
        )
    )


def _market_order(
    symbol: str = "DEMO", quantity: int = 100, portfolio_id: str | None = None
) -> Order:
    order = Order(
        symbol=symbol,
        side=OrderSide.BUY,
        quantity=quantity,
        order_type=OrderType.MARKET,
        time_in_force=TimeInForce.DAY,
        portfolio_id=portfolio_id,
        client_order_id=f"test-{symbol}",
    )
    order.validate()
    order.submit()
    return order


def _bar(open_price: float, volume: float = 1_000_000) -> Bar:
    return Bar(open=open_price, close=open_price, volume=volume,
               timestamp=datetime(2024, 1, 2))


# ---------------------------------------------------------------------------
# SimulatedFillProvider (paper)
# ---------------------------------------------------------------------------


def test_simulated_fill_uses_next_bar_open_and_fees():
    """Paper fills at the NEXT bar's open, through the fee model."""
    config = _config()
    portfolio = Portfolio(name="sim", initial_capital=100_000)
    executor = OrderExecutor(
        config=config,
        slippage=SlippageCalculator.disabled(),
        fees=_flat_fees("20"),
        portfolio=portfolio,
    )
    order = _market_order(quantity=100, portfolio_id=portfolio.portfolio_id)
    portfolio.add_order(order)  # the engine loop tracks it at submit time
    executor.submit(order)

    # Look-ahead rule (P1.3): the first step only ARMS the order; it
    # trades at the next bar's open.
    assert executor.step(_bar(open_price=100.0)) == []
    results = executor.step(_bar(open_price=100.5))

    assert len(results) == 1
    result = results[0]
    assert result.status == "filled"
    fill = result.fill
    assert fill is not None
    # next-bar-open fill (no slippage/improvement in this config)
    assert fill.fill_price == D("100.5")
    assert fill.quantity == D("100")
    # the flat fee model is applied by the provider
    assert fill.commission == D("20")
    # portfolio accounting (above the seam) saw the fill
    assert portfolio.positions["DEMO"].quantity == D("100")
    portfolio.sync_orders()  # the engine loop does this per tick
    assert portfolio.filled_orders, "fill not recorded on the portfolio"


def test_default_provider_is_simulated():
    """Acceptance: paper runs use SimulatedFillProvider by default."""
    executor = OrderExecutor()
    assert isinstance(executor.fill_provider, SimulatedFillProvider)
    assert isinstance(executor.fill_provider, FillProvider)


# ---------------------------------------------------------------------------
# BrokerFillProvider (live)
# ---------------------------------------------------------------------------


class _FakeBroker:
    """Duck-typed live broker: place_order + poll_fill, canned REAL fill."""

    def __init__(self, raw_fill: dict | None):
        self.raw_fill = raw_fill
        self.placed: list = []
        self.polled: list = []

    def place_order(self, order):
        self.placed.append(order)
        return "BROKER-42"

    def poll_fill(self, broker_order_id):
        self.polled.append(broker_order_id)
        return self.raw_fill


def test_broker_fill_calls_place_order_and_sets_broker_id():
    """Live: place_order is called, the REAL broker fill (not the bar's
    open) is what the portfolio trades, and the broker id rides the fill."""
    raw = {
        "tradingsymbol": "DEMO",
        "transaction_type": "BUY",
        "quantity": 100,
        "price": 99.75,
        "brokerage": 20,
    }
    broker = _FakeBroker(raw)
    portfolio = Portfolio(name="live", initial_capital=100_000)
    executor = OrderExecutor(config=_config(), portfolio=portfolio,
                             fill_provider=BrokerFillProvider(broker))
    order = _market_order(quantity=100, portfolio_id=portfolio.portfolio_id)
    executor.submit(order)

    assert executor.step(_bar(open_price=100.0)) == []  # arm
    results = executor.step(_bar(open_price=100.5))  # the bar's open is IGNORED

    assert len(broker.placed) == 1 and broker.placed[0] is order
    assert broker.polled == ["BROKER-42"]
    fill = results[0].fill
    assert fill is not None
    assert fill.broker_order_id == "BROKER-42"
    # REAL fill price from the broker — not the next-bar open (100.5)
    assert fill.fill_price == D("99.75")
    assert fill.commission == D("20")
    assert results[0].status == "filled"


def test_broker_no_fill_is_a_no_fill_not_an_error():
    broker = _FakeBroker(None)
    executor = OrderExecutor(config=_config(), fill_provider=BrokerFillProvider(broker))
    order = _market_order()
    executor.submit(order)
    assert executor.step(_bar(100.0)) == []  # arm
    results = executor.step(_bar(100.0))
    assert results[0].status == "no_fill"
    assert len(broker.placed) == 1  # the order still went to the broker


# ---------------------------------------------------------------------------
# Executor honors an injected provider (any provider)
# ---------------------------------------------------------------------------


class _SpyProvider(FillProvider):
    def __init__(self, decision: FillDecision):
        self.decision = decision
        self.calls: list = []

    def get_fill(self, order, market_data, quantity, rng):
        self.calls.append((order, dict(market_data), quantity, rng))
        return self.decision


def test_executor_uses_injected_provider():
    """The executor prices fills exclusively through the injected provider."""
    price = D("55.5")
    portfolio = Portfolio(name="spy", initial_capital=100_000)
    order = _market_order(quantity=10, portfolio_id=portfolio.portfolio_id)
    decision = FillDecision(
        fill=Fill(symbol="DEMO", side=OrderSide.BUY, quantity=D("10"),
                  fill_price=price, order_id=order.order_id),
        latency_ms=D("7"),
    )
    spy = _SpyProvider(decision)
    executor = OrderExecutor(config=_config(), portfolio=portfolio, fill_provider=spy)

    result = executor.execute(order, {"bid": 99, "ask": 101, "last": 100})

    assert len(spy.calls) == 1
    called_order, snapshot, quantity, rng = spy.calls[0]
    assert called_order is order
    assert quantity == D("10")
    assert rng is executor._rng  # the run's single seeded generator
    assert result.fill.fill_price == price
    assert result.latency_ms == D("7")
    assert result.status == "filled"


def test_provider_no_trade_maps_to_no_fill():
    portfolio = Portfolio(name="spy-none", initial_capital=100_000)
    order = _market_order(quantity=10, portfolio_id=portfolio.portfolio_id)
    spy = _SpyProvider(FillDecision(fill=None))
    executor = OrderExecutor(config=_config(), portfolio=portfolio, fill_provider=spy)

    result = executor.execute(order, {"bid": 99, "ask": 101, "last": 100})

    assert result.status == "no_fill"
    assert "provider" in result.reason
    assert order.is_working  # the order rests; nothing was applied
