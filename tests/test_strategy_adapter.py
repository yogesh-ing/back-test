"""Tests for Step 13: Strategy Adapter.

Covers signal generation, no-lookahead, order conversion, dry-run,
multi-symbol, position sizing, DB logging, state persistence, and
integration with Portfolio and OrderExecutor.
"""

from __future__ import annotations

import pandas as pd
import pytest
from decimal import Decimal
from datetime import datetime, timezone

from backtest.strategy.base import Strategy
from backtest.simulator import CommissionCalculator, ExecutionConfig, SlippageCalculator
from backtest.simulator.engine_loop import Bar
from backtest.simulator.portfolio import Portfolio
from backtest.simulator.execution import OrderExecutor
from backtest.forward.strategy_adapter import (
    StrategyAdapter,
    Signal,
    SignalAction,
    FixedQuantitySizer,
    FixedDollarSizer,
    PercentagePortfolioSizer,
)
from backtest.db.config import DatabaseConfig
from backtest.db.manager import DatabaseManager
from backtest.db.models import Base


class DummyLongStrategy(Strategy):
    # Use empty name to avoid auto-registration polluting global registry
    # (Strategy.__init_subclass__ registers only if name is truthy)
    name = ""
    params = {"threshold": 100}

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # set instance name for adapter logging, but keep class name empty to avoid registry
        self.name = "dummy_long"

    def generate_signals(self, candles: pd.DataFrame) -> pd.Series:
        # Buy when close > threshold
        return (candles["close"] > self.threshold).astype(int)


class DummyShortStrategy(Strategy):
    name = ""
    params = {}

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.name = "dummy_short"

    def generate_signals(self, candles: pd.DataFrame) -> pd.Series:
        # Always short when close > 100 else flat
        import pandas as pd

        series = pd.Series(0, index=candles.index)
        series[candles["close"] > 100] = -1
        return series


class SmaTestStrategy(Strategy):
    name = ""
    params = {"fast": 2, "slow": 3}

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.name = "sma_test"

    def generate_signals(self, candles: pd.DataFrame) -> pd.Series:
        fast = candles["close"].rolling(self.fast).mean()
        slow = candles["close"].rolling(self.slow).mean()
        return (fast > slow).astype(int)


def make_bar(symbol: str, ts: str, close: float, open_: float | None = None) -> dict:
    return {
        "symbol": symbol,
        "timestamp": ts,
        "open": open_ if open_ is not None else close,
        "high": close + 1,
        "low": close - 1,
        "close": close,
        "volume": 1000,
    }


# ---------------------------------------------------------------------------
# Signal model
# ---------------------------------------------------------------------------


def test_signal_validation():
    sig = Signal(symbol="INFY", action="BUY", quantity=100, reason="test")
    assert sig.symbol == "INFY"
    assert sig.action == "BUY"
    assert sig.quantity == Decimal("100")

    # invalid action
    with pytest.raises(Exception):
        Signal(symbol="INFY", action="INVALID")

    # LIMIT requires price
    with pytest.raises(Exception):
        Signal(symbol="INFY", action="BUY", order_type="LIMIT")

    # valid LIMIT
    sig2 = Signal(symbol="INFY", action="BUY", order_type="LIMIT", limit_price=1500, quantity=10)
    assert sig2.limit_price == Decimal("1500")


def test_signal_to_dict_roundtrip():
    sig = Signal(
        symbol="INFY",
        action="BUY",
        quantity=100,
        order_type="MARKET",
        reason="enter",
        indicators={"close": 1500},
        strength=Decimal("0.8"),
        target_position=Decimal("1"),
    )
    d = sig.to_dict()
    sig2 = Signal.from_dict(d)
    assert sig2.symbol == sig.symbol
    assert sig2.action == sig.action
    assert sig2.quantity == sig.quantity


# ---------------------------------------------------------------------------
# Position sizers
# ---------------------------------------------------------------------------


def test_fixed_quantity_sizer():
    sizer = FixedQuantitySizer(quantity=50)
    portfolio = Portfolio(name="sizer_test", initial_capital=100000)
    sig = Signal(symbol="INFY", action="BUY")
    qty = sizer.calculate_position_size(sig, portfolio)
    assert qty == Decimal("50")


def test_fixed_dollar_sizer():
    sizer = FixedDollarSizer(dollar_amount=10000)
    portfolio = Portfolio(name="sizer_test2", initial_capital=100000)
    sig = Signal(symbol="INFY", action="BUY", indicators={"close": 100})
    qty = sizer.calculate_position_size(sig, portfolio, current_price=100)
    assert qty == Decimal("100")


def test_percentage_sizer():
    sizer = PercentagePortfolioSizer(percentage=Decimal("0.1"))
    portfolio = Portfolio(name="sizer_test3", initial_capital=100000)
    sig = Signal(symbol="INFY", action="BUY", indicators={"close": 100})
    qty = sizer.calculate_position_size(sig, portfolio, current_price=100)
    # 10% of 100k = 10k, /100 = 100
    assert qty == Decimal("100")


# ---------------------------------------------------------------------------
# Adapter - basic
# ---------------------------------------------------------------------------


def test_adapter_initialization():
    strat = DummyLongStrategy(threshold=100)
    portfolio = Portfolio(name="init_test", initial_capital=100000)
    adapter = StrategyAdapter(strategy=strat, portfolio=portfolio, symbols=["INFY"], min_bars=2)
    assert adapter.strategy.name == "dummy_long"
    assert "INFY" in adapter.symbols


def test_adapter_requires_strategy_instance():
    portfolio = Portfolio(name="bad_test", initial_capital=100000)
    with pytest.raises(Exception):
        StrategyAdapter(strategy="not a strategy", portfolio=portfolio)


def test_on_bar_close_and_signal_generation():
    strat = DummyLongStrategy(threshold=100)
    portfolio = Portfolio(name="bar_test", initial_capital=100000)
    adapter = StrategyAdapter(strategy=strat, portfolio=portfolio, symbols=["INFY"], min_bars=2)

    bars = [
        make_bar("INFY", "2024-01-01T09:15:00+05:30", 99),
        make_bar("INFY", "2024-01-02T09:15:00+05:30", 101),
        make_bar("INFY", "2024-01-03T09:15:00+05:30", 102),
    ]

    # first bar: not enough bars
    sigs = adapter.on_bar_close(bars[0])
    assert len(sigs) == 0

    # second bar: close 101 > 100 => BUY
    sigs = adapter.on_bar_close(bars[1])
    assert len(sigs) == 1
    assert sigs[0].action == "BUY"
    assert sigs[0].symbol == "INFY"

    # third bar: target unchanged (1 -> 1), so the SIGNAL TRANSITION says HOLD
    # (ticket F-01: decisions are transition-based, not position-based —
    # the fill happens one bar later, so the portfolio is not yet long).
    sigs = adapter.on_bar_close(bars[2])
    assert len(sigs) == 1
    assert sigs[0].action == "HOLD"


def test_no_lookahead_bias():
    strat = SmaTestStrategy(fast=2, slow=3)
    portfolio = Portfolio(name="lookahead_test", initial_capital=100000)
    adapter = StrategyAdapter(strategy=strat, portfolio=portfolio, symbols=["INFY"], min_bars=3)

    bars = [
        make_bar("INFY", "2024-01-01T09:15:00+05:30", 100),
        make_bar("INFY", "2024-01-02T09:15:00+05:30", 101),
        make_bar("INFY", "2024-01-03T09:15:00+05:30", 102),
        make_bar("INFY", "2024-01-04T09:15:00+05:30", 103),
    ]

    for bar in bars:
        adapter.on_bar_close(bar)

    for sig in adapter.signal_history:
        assert sig.bar_ts is not None
        assert sig.generated_at is not None
        # bar_ts must be strictly earlier than generated_at
        assert sig.bar_ts < sig.generated_at, f"Lookahead bias: {sig.bar_ts} >= {sig.generated_at}"


def test_dry_run_mode():
    strat = DummyLongStrategy(threshold=100)
    portfolio = Portfolio(name="dry_test", initial_capital=100000)
    adapter = StrategyAdapter(
        strategy=strat, portfolio=portfolio, symbols=["INFY"], dry_run=True, min_bars=1
    )

    bar = make_bar("INFY", "2024-01-01T09:15:00+05:30", 101)
    adapter.on_bar_close(bar)

    assert len(adapter.signal_history) == 1
    assert len(adapter.order_history) == 0  # no orders in dry_run


def test_create_orders_from_signals():
    """Signal → Order only; the adapter never fills (ticket F-01)."""
    strat = DummyLongStrategy(threshold=100)
    portfolio = Portfolio(name="exec_test", initial_capital=100000)
    adapter = StrategyAdapter(strategy=strat, portfolio=portfolio, symbols=["INFY"], min_bars=1)

    bar = make_bar("INFY", "2024-01-01T09:15:00+05:30", 101)
    sigs = adapter.on_bar_close(bar)
    orders = adapter.create_orders(sigs, market_data=bar)

    assert len(orders) == 1
    assert len(adapter.order_history) == 1
    order = adapter.order_history[0]
    assert order.symbol == "INFY"
    assert str(order.side) == "buy"
    # No fill happened: the portfolio is still flat.
    assert portfolio.get_position("INFY") is None


def test_portfolio_validation():
    strat = DummyLongStrategy(threshold=100)
    # small capital, large order should be rejected
    portfolio = Portfolio(name="valid_test", initial_capital=1000)
    adapter = StrategyAdapter(
        strategy=strat,
        portfolio=portfolio,
        symbols=["INFY"],
        min_bars=1,
        position_sizer=FixedQuantitySizer(quantity=1000),  # 1000 shares @ 101 = 101k > 1k
    )

    bar = make_bar("INFY", "2024-01-01T09:15:00+05:30", 101)
    sigs = adapter.on_bar_close(bar)
    adapter.create_orders(sigs, market_data=bar)

    # order should be rejected due to insufficient funds, so no order history
    assert len(adapter.order_history) == 0


def test_multi_symbol():
    strat = DummyLongStrategy(threshold=100)
    portfolio = Portfolio(name="multi_test", initial_capital=200000)
    adapter = StrategyAdapter(strategy=strat, portfolio=portfolio, symbols=["INFY", "TCS"], min_bars=1)

    bars = [
        make_bar("INFY", "2024-01-01T09:15:00+05:30", 101),
        make_bar("TCS", "2024-01-01T09:15:00+05:30", 102),
        make_bar("INFY", "2024-01-02T09:15:00+05:30", 103),
        make_bar("TCS", "2024-01-02T09:15:00+05:30", 104),
    ]

    for bar in bars:
        adapter.on_bar_close(bar)

    assert len(adapter.signal_history) == 4
    # INFY and TCS each should have signals
    symbols = {s.symbol for s in adapter.signal_history}
    assert "INFY" in symbols
    assert "TCS" in symbols


def test_on_market_data_tick_vs_bar():
    strat = DummyLongStrategy(threshold=100)
    portfolio = Portfolio(name="tick_test", initial_capital=100000)
    adapter = StrategyAdapter(strategy=strat, portfolio=portfolio, symbols=["INFY"], min_bars=1)

    # tick data (no close) should not generate signal
    tick = {"symbol": "INFY", "bid": 100, "ask": 101, "last": 100.5, "timestamp": "2024-01-01T09:15:00+05:30"}
    sigs = adapter.on_market_data(tick)
    assert len(sigs) == 0

    # bar data should generate
    bar = make_bar("INFY", "2024-01-01T09:15:00+05:30", 101)
    sigs = adapter.on_market_data(bar)
    assert len(sigs) == 1


def test_state_persistence():
    strat = DummyLongStrategy(threshold=100)
    portfolio = Portfolio(name="state_test", initial_capital=100000)
    adapter = StrategyAdapter(strategy=strat, portfolio=portfolio, symbols=["INFY"], min_bars=1)

    bars = [
        make_bar("INFY", "2024-01-01T09:15:00+05:30", 99),
        make_bar("INFY", "2024-01-02T09:15:00+05:30", 101),
    ]

    for bar in bars:
        adapter.on_bar_close(bar)

    state = adapter.get_state()
    assert "bars" in state
    assert "symbols" in state

    # restore
    portfolio2 = Portfolio(name="state_test2", initial_capital=100000)
    adapter2 = StrategyAdapter.from_dict(state, strategy=strat, portfolio=portfolio2)
    assert len(adapter2.bars["INFY"]) == 2
    assert adapter2.symbols == ["INFY"]


def test_orders_fill_at_next_open_not_signal_bar():
    """F-01: the adapter produces orders; the executor bar clock fills at
    the NEXT bar's open — never the signal bar's close."""
    strat = DummyLongStrategy(threshold=100)
    portfolio = Portfolio(name="integ_test", initial_capital=100000)
    executor = OrderExecutor(
        config=ExecutionConfig(seed=7, price_improvement_probability=Decimal("0")),
        slippage=SlippageCalculator.disabled(),
        fees=CommissionCalculator(),
        portfolio=portfolio,
    )
    adapter = StrategyAdapter(strategy=strat, portfolio=portfolio, symbols=["INFY"], min_bars=1)

    # Signal bar t: close 101 > threshold → BUY. Order must NOT fill here.
    bar_t = make_bar("INFY", "2024-01-01T09:15:00+05:30", close=101, open_=100)
    sigs = adapter.on_bar_close(bar_t)
    orders = adapter.create_orders(sigs, market_data=bar_t)
    assert len(orders) == 1
    for order in orders:
        executor.submit(order)

    # Feeding bar t again only ARMS the order — no fill, no look-ahead.
    assert executor.step(Bar(open=Decimal("100"), close=Decimal("101"), volume=Decimal("1000"))) == []

    # Next bar t+1: fill at its OPEN (103), never bar t's close (101).
    results = executor.step(Bar(open=Decimal("103"), close=Decimal("110"), volume=Decimal("1000")))
    assert len(results) == 1
    assert results[0].fill is not None
    assert results[0].fill.fill_price == Decimal("103")
    assert results[0].fill.fill_price != Decimal("101")
    assert portfolio.get_position("INFY") is not None


def test_signal_strength_and_indicators():
    strat = SmaTestStrategy(fast=2, slow=3)
    portfolio = Portfolio(name="strength_test", initial_capital=100000)
    adapter = StrategyAdapter(strategy=strat, portfolio=portfolio, symbols=["INFY"], min_bars=3)

    bars = [
        make_bar("INFY", "2024-01-01T09:15:00+05:30", 100),
        make_bar("INFY", "2024-01-02T09:15:00+05:30", 101),
        make_bar("INFY", "2024-01-03T09:15:00+05:30", 102),
    ]

    for bar in bars:
        adapter.on_bar_close(bar)

    assert len(adapter.signal_history) >= 1
    sig = adapter.signal_history[-1]
    assert sig.strength is not None
    assert sig.indicators is not None
    assert "close" in sig.indicators
    assert "fast_sma" in sig.indicators or "slow_sma" in sig.indicators or "signal_value" in sig.indicators


def test_db_logging():
    cfg = DatabaseConfig(url="sqlite:///:memory:", pool_min_size=1, pool_max_size=5)
    db = DatabaseManager(cfg)
    db.connect()
    Base.metadata.create_all(db.engine)

    strat = DummyLongStrategy(threshold=100)
    portfolio = Portfolio(name="db_log_test", initial_capital=100000)
    adapter = StrategyAdapter(
        strategy=strat, portfolio=portfolio, symbols=["INFY"], db_manager=db, min_bars=1
    )

    bar = make_bar("INFY", "2024-01-01T09:15:00+05:30", 101)
    sigs = adapter.on_bar_close(bar)
    adapter.create_orders(sigs, market_data=bar)

    with db.session() as session:
        from backtest.db.models import StrategySignal as Row

        rows = session.query(Row).all()
        # at least 2 rows: generated (executed=False) + mapped-to-order (executed=True)
        assert len(rows) >= 2
        # check no lookahead
        for r in rows:
            if r.bar_ts and r.generated_at:
                assert r.bar_ts < r.generated_at

    db.disconnect()


def test_allow_short():
    strat = DummyShortStrategy()
    portfolio = Portfolio(name="short_test", initial_capital=100000)
    # short not allowed by default
    adapter = StrategyAdapter(strategy=strat, portfolio=portfolio, symbols=["INFY"], min_bars=1, allow_short=False)

    bar = make_bar("INFY", "2024-01-01T09:15:00+05:30", 101)
    sigs = adapter.on_bar_close(bar)
    # should be HOLD because short not allowed
    assert sigs[0].action == "HOLD"

    # with allow_short=True, should be SELL
    portfolio2 = Portfolio(name="short_test2", initial_capital=100000)
    adapter2 = StrategyAdapter(
        strategy=strat, portfolio=portfolio2, symbols=["INFY"], min_bars=1, allow_short=True
    )
    sigs2 = adapter2.on_bar_close(bar)
    assert sigs2[0].action == "SELL"


def test_existing_strategies_via_registry():
    from backtest.strategy.registry import get_strategy

    for name in ["sma_crossover", "buy_and_hold", "rsi_reversion", "donchian_breakout"]:
        StratCls = get_strategy(name)
        strat = StratCls()
        portfolio = Portfolio(name=f"reg_test_{name}", initial_capital=100000)
        adapter = StrategyAdapter(strategy=strat, portfolio=portfolio, symbols=["INFY"], min_bars=1)
        # feed a few bars
        for i in range(5):
            bar = make_bar("INFY", f"2024-01-0{i+1}T09:15:00+05:30", 100 + i)
            adapter.on_bar_close(bar)
        # should not crash
        assert len(adapter.signal_history) >= 1


# ---------------------------------------------------------------------------
# F-17 — transition-based decisions produce the SAME trade selection as the
# old position-based logic (given fills succeed), except persistent signals
# no longer re-fire. The reference below is the OLD logic's decision +
# same-bar-fill state machine, re-implemented from the pre-F-01 code.
# ---------------------------------------------------------------------------


def _clip_target(target: int, allow_short: bool) -> int:
    """Adapter clips the strategy's target before deciding (long-only ⇒ ≥ 0)."""
    if not allow_short and target < 0:
        return 0
    return target


def _old_decide(position: int, target: int) -> str:
    """The pre-F-01 position-based ``_decide_action`` body."""
    if position == target:
        return "HOLD"
    if position == 0 and target > 0:
        return "BUY"  # entry long
    if position == 0 and target < 0:
        return "SELL"  # entry short
    if position > 0 and target == 0:
        return "SELL"  # exit long
    if position < 0 and target == 0:
        return "BUY"  # exit short
    if position > 0 and target < 0:
        return "SELL"  # flip: close long only
    if position < 0 and target > 0:
        return "BUY"  # flip: close short only
    return "HOLD"


def _old_actions(targets: list[int], allow_short: bool) -> list[str]:
    """Action sequence of the OLD logic with successful same-bar fills."""
    actions: list[str] = []
    position = 0
    for raw in targets:
        target = _clip_target(raw, allow_short)
        action = _old_decide(position, target)
        actions.append(action)
        # same-bar fill state machine (fill succeeds):
        if action == "BUY":
            position = 1 if position == 0 else (0 if position < 0 else position)
        elif action == "SELL":
            position = -1 if position == 0 else (0 if position > 0 else position)
    return actions


class TargetReturningStrategy(Strategy):
    """Returns a fixed target for every bar fed so far — the adapter takes
    ``series.iloc[-1]``, so this drives the per-bar target sequence."""

    name = ""
    params = {}

    def __init__(self, target: int, **kwargs):
        super().__init__(**kwargs)
        self.name = "target_ret"
        self._target = target

    def generate_signals(self, candles: pd.DataFrame) -> pd.Series:
        return pd.Series(self._target, index=candles.index)


@pytest.mark.parametrize(
    "targets,allow_short",
    [
        ([0, 1, 1, 1, 0], False),
        ([0, 1, 1, 1, 0], True),
        ([0, 1, 0, 1, 0], False),          # 1-bar spikes
        ([0, 1, 1, 0, 1, 1, 0], False),    # re-entry
        ([0, -1, -1, 0], True),            # persistent short
        ([0, -1, 0, -1, 0], True),         # short spikes
        ([0, 1, -1, -1, 0], True),         # flip long→short
        ([0, -1, 1, 1, 0], True),          # flip short→long
        ([0, 1, -1, 0, 1], True),          # flip then flat
        ([0, 1, -1, 1, -1, 0], True),      # flip back and forth
    ],
)
def test_transition_decisions_match_old_position_based(targets, allow_short):
    """F-17: for the same data (fills succeed), the new transition-based
    decisions are IDENTICAL to the old position-based trade selection."""
    old = _old_actions(targets, allow_short)
    assert old[0] == "HOLD"  # first bar is always flat->... , targets[0]=0

    portfolio = Portfolio(name=f"f17_{allow_short}", initial_capital=100000)
    adapter = StrategyAdapter(
        strategy=TargetReturningStrategy(target=0),
        portfolio=portfolio,
        symbols=["INFY"],
        min_bars=1,
        allow_short=allow_short,
    )

    received: list[str] = []
    # one strategy per target would not work — the adapter reuses one strategy
    # for the whole run, so drive it with a per-bar strategy dict instead.
    for i, raw in enumerate(targets):
        adapter._strategies["INFY"] = TargetReturningStrategy(target=raw)
        bar = make_bar("INFY", f"2024-01-0{i+1}T09:15:00+05:30", 100 + i)
        sigs = adapter.on_bar_close(bar)
        assert len(sigs) == 1, f"expected exactly one signal on bar {i}"
        received.append(sigs[0].action)

    assert received == old


def test_persistent_signal_fires_once_not_every_bar():
    """F-17 documented divergence: a persistent target fires once (BUY on the
    0→1 transition) and then HOLDs. The OLD position-based logic re-fired BUY
    on every bar while no fill landed (dry-run, rejected order, no executor);
    the canonical engine only ever retries ORDERS the executor still holds."""
    portfolio = Portfolio(name="f17_refire", initial_capital=100000)
    adapter = StrategyAdapter(
        strategy=TargetReturningStrategy(target=1),
        portfolio=portfolio,
        symbols=["INFY"],
        min_bars=1,
    )

    for i in range(3):
        bar = make_bar("INFY", f"2024-01-0{i+1}T09:15:00+05:30", 101)
        sigs = adapter.on_bar_close(bar)
        assert len(sigs) == 1
        assert sigs[0].action == ("BUY" if i == 0 else "HOLD")
