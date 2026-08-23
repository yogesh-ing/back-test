"""Performance benchmarks for forward testing simulator (Step 24).

Run with: pytest benchmarks/ --benchmark-only
Requires: pip install pytest-benchmark
"""

from __future__ import annotations

import pytest

from backtest.simulator.portfolio import Portfolio
from backtest.simulator.execution import OrderExecutor, ExecutionConfig
from backtest.simulator.order import Order
from backtest.simulator.slippage import SlippageCalculator
from backtest.simulator.fees import CommissionCalculator


@pytest.fixture
def portfolio():
    return Portfolio(name="bench_test", initial_capital=100000)


@pytest.fixture
def executor(portfolio):
    return OrderExecutor(config=ExecutionConfig(), slippage=SlippageCalculator(), fees=CommissionCalculator(), portfolio=portfolio)


def test_benchmark_order_creation(benchmark):
    def create_orders():
        orders = []
        for i in range(100):
            order = Order(symbol="INFY", side="buy", quantity=10, order_type="market")
            order.submit()
            orders.append(order)
        return orders

    benchmark(create_orders)


def test_benchmark_order_execution(benchmark, executor):
    order = Order(symbol="INFY", side="buy", quantity=100, order_type="market")
    order.submit()

    market_data = {"bid": 99, "ask": 101, "last": 100, "volume": 10000}

    def execute():
        # Reset executor for each run
        executor.reset()
        order2 = Order(symbol="INFY", side="buy", quantity=100, order_type="market")
        order2.submit()
        return executor.execute(order2, market_data)

    benchmark(execute)


def test_benchmark_slippage_calculation(benchmark):
    from backtest.simulator.slippage import SlippageCalculator

    calc = SlippageCalculator()
    order = Order(symbol="INFY", side="buy", quantity=100, order_type="market")
    order.submit()
    market_data = {"bid": 99, "ask": 101, "last": 100, "volume": 10000, "high": 101, "low": 99}

    def calc_slippage():
        return calc.calculate_slippage(order, market_data)

    benchmark(calc_slippage)


def test_benchmark_fees_calculation(benchmark):
    from backtest.simulator.fees import CommissionCalculator

    calc = CommissionCalculator()

    def calc_fees():
        return calc.calculate(quantity=100, fill_price=1500, side="buy")

    benchmark(calc_fees)


def test_benchmark_portfolio_equity(benchmark, portfolio):
    portfolio.open_position("INFY", 100, 100)
    portfolio.open_position("TCS", 50, 200)

    def calc_equity():
        return portfolio.calculate_total_equity()

    benchmark(calc_equity)
