"""PaperRunner — one run = one Portfolio + one source + one strategy (P1.4).

Acceptance under test:

* an end-to-end synthetic paper run produces orders, fills, positions and an
  equity curve;
* fills happen at the NEXT bar's open, never the signal bar's close (P1.3);
* after a run with a database, the written rows are tagged ``mode='paper'``
  (P1.1 columns);
* the order queue is idempotent on ``client_order_id``.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

from backtest.db.manager import DatabaseManager
from backtest.db.models import Base
from backtest.db.models import Portfolio as PortfolioRow
from backtest.forward.paper_runner import OrderQueue, PaperRunner
from backtest.simulator import (
    CommissionCalculator,
    ExecutionConfig,
    Order,
    OrderExecutor,
    SlippageCalculator,
)
from backtest.simulator.portfolio import Portfolio

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_FILE = REPO_ROOT / "config" / "database.yaml"

D = Decimal


# ---------------------------------------------------------------------------
# Fakes: a deterministic strategy and a fixed-bar source
# ---------------------------------------------------------------------------


def _fake_signals(candles: pd.DataFrame, entry_at: int, exit_at: int) -> pd.Series:
    """Entry/exit-model signals: 1 from bar ``entry_at`` until bar ``exit_at``."""
    s = pd.Series(0, index=candles.index, dtype=int)
    if len(candles) > entry_at:
        s.iloc[entry_at:] = 1
    if len(candles) > exit_at:
        s.iloc[exit_at:] = 0
    return s


class FakeBuyStrategy:
    """Duck-typed strategy fake: enters at bar 3, exits at bar 10.

    Deliberately NOT a ``Strategy`` subclass — subclassing auto-registers
    into the global strategy registry and pollutes
    ``tests/test_backtest.py::test_all_strategies_auto_registered``.
    """

    name = "fake_buy"

    def generate_signals(self, candles: pd.DataFrame) -> pd.Series:
        return _fake_signals(candles, entry_at=3, exit_at=10)


class FakeSource:
    """Fixed bars: open[t] = 100 + t, close[t] = open[t] + 1, big volume."""

    def __init__(self, bars: dict[str, pd.DataFrame], volumes: dict[str, list] | None = None):
        self._bars = bars
        self._volumes = volumes or {}

    def get_candles(self, symbol, start, end, interval="day"):
        return self._bars[str(symbol).strip().upper()]


def make_bars(n: int = 30, start: str = "2024-01-01") -> pd.DataFrame:
    idx = pd.date_range(start=start, periods=n, freq="B")
    return pd.DataFrame(
        {
            "open": [100.0 + t for t in range(n)],
            "high": [101.0 + t for t in range(n)],
            "low": [99.0 + t for t in range(n)],
            "close": [101.0 + t for t in range(n)],
            "volume": [100_000] * n,
        },
        index=idx,
    )


def make_executor() -> OrderExecutor:
    """Zero slippage, no price improvement: fills land exactly at the open."""
    config = ExecutionConfig(seed=7, price_improvement_probability=D("0"))
    return OrderExecutor(
        config=config,
        slippage=SlippageCalculator.disabled(),
        fees=CommissionCalculator(),
    )


@pytest.fixture()
def db():
    """In-memory SQLite manager with the schema (same pattern as test_db_manager)."""
    manager = DatabaseManager.from_env(
        path=str(CONFIG_FILE), profile="testing", url="sqlite:///:memory:"
    )
    manager.connect()
    Base.metadata.create_all(manager.engine)
    yield manager
    manager.disconnect()


def make_portfolio() -> Portfolio:
    return Portfolio(name="paper-run", initial_capital=100_000)


# ---------------------------------------------------------------------------
# The ticket's tests
# ---------------------------------------------------------------------------


def test_end_to_end_synthetic_run():
    from backtest.data.synthetic import SyntheticSource

    portfolio = make_portfolio()
    executor = make_executor()
    runner = PaperRunner(
        portfolio=portfolio,
        source=SyntheticSource(),
        strategy=FakeBuyStrategy(),
        executor=executor,
        symbols=["INFY"],
        start="2023-01-01",
        end="2023-12-31",
        interval="day",
    )
    summary = runner.run()

    # orders were placed (entry + exit)
    assert len(runner.order_queue) == 2
    assert len(portfolio.filled_orders) == 2
    # positions updated: the round trip is closed by the exit signal
    assert len(portfolio.closed_positions) == 1
    assert len(portfolio.positions) == 0
    # equity curve recorded: one snapshot per bar
    candles = SyntheticSource().get_candles("INFY", "2023-01-01", "2023-12-31", "day")
    assert len(portfolio.equity_history) == len(candles.index)
    # the summary reflects the run
    assert summary["name"] == "paper-run"
    assert summary["closed_positions"] == 1
    assert portfolio.mode == "paper"
    assert portfolio.source == "synthetic"


def test_paper_rows_tagged_mode_paper(db):
    from backtest.data.synthetic import SyntheticSource

    portfolio = make_portfolio()
    runner = PaperRunner(
        portfolio=portfolio,
        source=SyntheticSource(),
        strategy=FakeBuyStrategy(),
        executor=make_executor(),
        symbols=["INFY"],
        start="2023-01-01",
        end="2023-12-31",
        db=db,
    )
    runner.run()

    from sqlalchemy import func, select

    from backtest.db.models import Fill as FillRow
    from backtest.db.models import Order as OrderRow
    from backtest.db.models import Position as PositionRow

    with db.session() as session:
        row = session.get(PortfolioRow, portfolio.portfolio_id)
        assert row is not None
        assert row.mode == "paper"
        assert row.source == "synthetic"
        # the portfolio graph (positions/orders/fills) was written with the run
        assert session.scalar(select(func.count()).select_from(PositionRow)) >= 1
        assert session.scalar(select(func.count()).select_from(OrderRow)) >= 2
        assert session.scalar(select(func.count()).select_from(FillRow)) >= 2


# ---------------------------------------------------------------------------
# Fill timing (P1.3 discipline at the runner level)
# ---------------------------------------------------------------------------


def test_fill_price_is_next_bar_open_not_signal_close():
    bars = make_bars()
    portfolio = make_portfolio()
    executor = make_executor()
    runner = PaperRunner(
        portfolio=portfolio,
        source=FakeSource({"INFY": bars}),
        strategy=FakeBuyStrategy(),
        executor=executor,
        symbols=["INFY"],
        quantity=100,
    )
    runner.run()

    fills = [f for o in portfolio.filled_orders for f in getattr(o, "fills", [])]
    assert len(fills) == 2
    entry, exit_ = fills[0], fills[1]
    # signal on bar 3 → fills at bar 4's OPEN (104.0). (open[4] equals
    # close[3] by construction of make_bars, so the unambiguous next-open
    # proof lives in test_timing_with_distinct_prices.)
    assert entry.fill_price == D("104.0")
    # signal on bar 10 → fills at bar 11's OPEN (111.0)
    assert exit_.fill_price == D("111.0")
    # cash moved (bought 104 → sold 111: this round trip is profitable)
    # and fees were deducted along the way
    assert portfolio.current_cash != portfolio.initial_capital
    assert portfolio.current_cash == D("100660.80")  # +700 gross − 39.20 fees
    assert portfolio.total_commission == D("39.20")


def test_first_bar_signal_cannot_fill_same_bar():
    bars = make_bars()
    portfolio = make_portfolio()
    executor = make_executor()
    runner = PaperRunner(
        portfolio=portfolio,
        source=FakeSource({"INFY": bars}),
        strategy=FakeBuyStrategy(),
        executor=executor,
        symbols=["INFY"],
        quantity=100,
    )
    # No position may exist before bar 4's open — the entry signal is bar 3.
    runner.run()
    fills = [f for o in portfolio.filled_orders for f in getattr(o, "fills", [])]
    assert fills[0].fill_price == D("104.0")  # bar 4 open, not bar 3's close (104.0 close!)
    # NOTE: open[4]==104.0 equals close[3]==104.0 by construction of make_bars,
    # so the price identity alone is ambiguous here — the multi-order test
    # below pins the timing with distinct values.


def test_timing_with_distinct_prices():
    """Entry signal bar's close differs from next bar's open → unambiguous."""
    idx = pd.date_range(start="2024-01-01", periods=30, freq="B")
    n = len(idx)
    bars = pd.DataFrame(
        {
            "open": [100.0 + t for t in range(n)],
            "high": [102.0 + t for t in range(n)],
            "low": [99.0 + t for t in range(n)],
            # close deliberately far from the NEXT open
            "close": [200.0 + t for t in range(n)],
            "volume": [100_000] * n,
        },
        index=idx,
    )
    portfolio = make_portfolio()
    runner = PaperRunner(
        portfolio=portfolio,
        source=FakeSource({"INFY": bars}),
        strategy=FakeBuyStrategy(),
        executor=make_executor(),
        symbols=["INFY"],
        quantity=100,
    )
    runner.run()
    fills = [f for o in portfolio.filled_orders for f in getattr(o, "fills", [])]
    # signal bar 3: close = 203.0 — the fill MUST be 104.0 (bar 4 open), never 203
    assert fills[0].fill_price == D("104.0")
    assert fills[0].fill_price != D("203.0")
    # exit signal bar 10: close = 210.0 — the fill MUST be 111.0 (bar 11 open)
    assert fills[1].fill_price == D("111.0")
    assert fills[1].fill_price != D("210.0")


# ---------------------------------------------------------------------------
# Multi-symbol and partial fills
# ---------------------------------------------------------------------------


def test_multi_symbol_run_trades_every_symbol():
    bars_a = make_bars()
    bars_b = make_bars(start="2024-01-01")  # same shape, different symbol
    portfolio = make_portfolio()
    runner = PaperRunner(
        portfolio=portfolio,
        source=FakeSource({"INFY": bars_a, "TCS": bars_b}),
        strategy=FakeBuyStrategy(),
        executor=make_executor(),
        symbols=["INFY", "TCS"],
        quantity=50,
    )
    summary = runner.run()

    assert len(runner.order_queue) == 4  # 2 symbols × (entry + exit)
    assert summary["closed_positions"] == 2
    assert portfolio.get_position("INFY") is None
    assert portfolio.get_position("TCS") is None
    assert len(portfolio.equity_history) == len(bars_a.index)


def test_partial_fill_then_full_close():
    """A buy capped by liquidity fills in pieces; the exit closes the actual qty."""
    bars = make_bars()
    volumes = [100_000] * 30
    volumes[4] = 500  # entry fill bar: cap = 50 (10% participation)
    volumes[5] = 100_000
    bars["volume"] = volumes
    portfolio = make_portfolio()
    executor = make_executor()
    runner = PaperRunner(
        portfolio=portfolio,
        source=FakeSource({"INFY": bars}),
        strategy=FakeBuyStrategy(),
        executor=executor,
        symbols=["INFY"],
        quantity=100,
    )
    runner.run()

    fills = [f for o in portfolio.filled_orders for f in getattr(o, "fills", [])]
    buy_fills = [f for f in fills if f.side.value == "buy"]
    assert len(buy_fills) == 2  # 50 at bar 4 open, 50 at bar 5 open
    assert buy_fills[0].quantity == D("50")
    assert buy_fills[0].fill_price == D("104.0")
    assert buy_fills[1].quantity == D("50")
    assert buy_fills[1].fill_price == D("105.0")
    sell_fills = [f for f in fills if f.side.value == "sell"]
    assert len(sell_fills) == 1
    assert sell_fills[0].quantity == D("100")  # closed exactly what was held
    assert sell_fills[0].fill_price == D("111.0")
    assert len(portfolio.closed_positions) == 1


# ---------------------------------------------------------------------------
# OrderQueue idempotency + misc
# ---------------------------------------------------------------------------


def test_order_queue_idempotent_on_client_order_id():
    q = OrderQueue()
    o = Order.market("INFY", "buy", 100, client_order_id="abc-123")
    o.submit()
    assert q.submit(o) is True
    assert q.submit(o) is False
    assert len(q) == 1
    assert q.orders == (o,)


def test_no_signals_means_no_trades():
    class NeverTrade:
        name = "never_trade"

        def generate_signals(self, candles: pd.DataFrame) -> pd.Series:
            return pd.Series(0, index=candles.index, dtype=int)

    portfolio = make_portfolio()
    runner = PaperRunner(
        portfolio=portfolio,
        source=FakeSource({"INFY": make_bars()}),
        strategy=NeverTrade(),
        executor=make_executor(),
        symbols=["INFY"],
    )
    summary = runner.run()
    assert len(runner.order_queue) == 0
    assert summary["closed_positions"] == 0
    assert summary["equity"] == D("100000.0000") or summary["equity"] == D("100000")
    assert len(portfolio.equity_history) == 30


def test_run_without_symbols_raises():
    from backtest.simulator.errors import ValidationError

    runner = PaperRunner(
        portfolio=make_portfolio(),
        source=FakeSource({"INFY": make_bars()}),
        strategy=FakeBuyStrategy(),
        executor=make_executor(),
    )
    with pytest.raises(ValidationError):
        runner.run()
