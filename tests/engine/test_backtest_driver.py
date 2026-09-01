"""BacktestDriver tests (ticket P2.1).

The driver runs the SAME engine loop as PaperRunner
(``simulator/engine_loop.run_engine_loop``), so the equivalence tests here
are structural: identical inputs → identical P&L, plus the P1.5 property
(within COST_TOLERANCE of the vectorized Backtester on gapless bars).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from backtest.data.db_source import DbSource
from backtest.engine.backtest_driver import BacktestDriver
from backtest.engine.backtester import BacktestConfig, Backtester
from backtest.forward.paper_runner import PaperRunner, _FrameSource, free_executor
from backtest.simulator.engine_loop import OrderQueue
from backtest.simulator.portfolio import Portfolio
from backtest.strategy.registry import get_strategy

INITIAL_CAPITAL = 100_000.0
N_BARS = 300
MAX_BAR_MOVE = 0.005
# Zero cost on both sides ⇒ tolerance absorbs only the ≤ MAX_BAR_MOVE
# sizing margin + integer-share rounding (see the P1.5 module docstring).
COST_TOLERANCE = 0.005 * INITIAL_CAPITAL


def _gapless_bars(n: int = N_BARS, seed: int = 7, vmax: float = MAX_BAR_MOVE) -> pd.DataFrame:
    """Deterministic gapless bars: open[t] == close[t-1], |move| ≤ vmax."""
    rng = np.random.default_rng(seed)
    rets = rng.uniform(-vmax, vmax, n)
    close = 100 * np.exp(np.cumsum(rets))
    open_ = np.empty(n)
    open_[0] = 100.0
    open_[1:] = close[:-1]
    return pd.DataFrame(
        {
            "open": open_,
            "high": np.maximum(open_, close) * 1.001,
            "low": np.minimum(open_, close) * 0.999,
            "close": close,
            "volume": 1_000_000,
        },
        index=pd.date_range("2021-01-01", periods=n, freq="B"),
    )


def _funded_all_in(symbol: str, price: float, portfolio: Portfolio) -> int:
    """Equity-based all-in sizing, funded at the next-bar-open fill price."""
    if price <= 0:
        return 0
    equity = float(portfolio.calculate_total_equity())
    return int(equity / price / (1 + MAX_BAR_MOVE))


def _paper_pnl(bars: pd.DataFrame, strategy) -> float:
    portfolio = Portfolio(
        name="forward", initial_capital=INITIAL_CAPITAL, mode="paper", source="synthetic"
    )
    runner = PaperRunner(
        portfolio=portfolio,
        source=_FrameSource(bars),
        strategy=strategy,
        executor=free_executor(portfolio, max_participation="1"),
        symbols=["EQ"],
        start=bars.index[0].date().isoformat(),
        end=bars.index[-1].date().isoformat(),
        size_fn=_funded_all_in,
    )
    runner.run()
    return float(portfolio.calculate_total_equity()) - INITIAL_CAPITAL


def _driver_pnl(bars: pd.DataFrame, strategy, source=None, source_tag=None):
    portfolio = Portfolio(
        name="backtest", initial_capital=INITIAL_CAPITAL, mode="paper", source="replay"
    )
    driver = BacktestDriver(
        source=source or _FrameSource(bars),
        strategy=strategy,
        portfolio=portfolio,
        executor=free_executor(portfolio, max_participation="1"),
        order_queue=OrderQueue(),
        symbols=["EQ"],
        start=bars.index[0].date().isoformat(),
        end=bars.index[-1].date().isoformat(),
        size_fn=_funded_all_in,
        source_tag=source_tag,
    )
    driver.run()
    return portfolio, float(portfolio.calculate_total_equity()) - INITIAL_CAPITAL


def _vectorized_pnl(bars: pd.DataFrame, strategy, commission_pct=0.0, slippage_pct=0.0) -> float:
    signals = strategy.generate_signals(bars)
    result = Backtester(
        BacktestConfig(
            initial_capital=INITIAL_CAPITAL,
            commission_pct=commission_pct,
            slippage_pct=slippage_pct,
        )
    ).run(bars, signals)
    return float(result.equity.iloc[-1] - INITIAL_CAPITAL)


def test_backtest_matches_forward_same_data():
    """P2.1 acceptance: BacktestDriver == PaperRunner on identical bars."""
    bars = _gapless_bars()
    strategy = get_strategy("sma_crossover")(fast=5, slow=20)

    _, backtest_pnl = _driver_pnl(bars, strategy)
    forward_pnl = _paper_pnl(bars, strategy)

    # One shared loop, identical inputs ⇒ identical P&L (stronger than a
    # tolerance: any difference is a loop divergence).
    assert backtest_pnl == forward_pnl

    # And the P1.5 property: within COST_TOLERANCE of the vectorized
    # Backtester on gapless bars.
    assert abs(backtest_pnl - _vectorized_pnl(bars, strategy)) < COST_TOLERANCE


def test_backtest_records_positions_and_equity():
    bars = _gapless_bars()
    strategy = get_strategy("sma_crossover")(fast=5, slow=20)

    # A real DbSource instance (no connection needed — get_candles is
    # shadowed) so the source tag resolves to "replay".
    source = DbSource(db_url=None)
    source.get_candles = lambda *a, **k: bars  # noqa: SLF001 — test seam

    portfolio, pnl = _driver_pnl(bars, strategy, source=source)

    # Run classification: simulated fills ⇒ 'paper'; historical bars ⇒ 'replay'.
    assert portfolio.mode == "paper"
    assert portfolio.source == "replay"

    # Positions and equity were actually recorded.
    assert len(portfolio.closed_positions) >= 3  # the strategy round-trips
    assert len(portfolio.equity_history) == len(bars)  # one snapshot per bar
    assert len(portfolio.filled_orders) >= 6  # entry + exit per round trip
    assert portfolio.filled_orders, "no fills recorded"
    assert pnl != 0.0  # the run actually traded


def test_quick_screen_mode_still_works():
    """The legacy vectorized quick path coexists with the driver.

    The ticket's name references a "quick screen" mode; this repo has no
    such feature — the legacy fast path is the vectorized Backtester used
    by the backtest API. This guards that path (with default costs) and
    re-asserts the P1.5 property: the driver stays within COST_TOLERANCE
    of the zero-cost vectorized baseline (acceptance: "P1.5 test passes
    for backtest too"). See instructions/refactoring-findings.md.
    """
    bars = _gapless_bars(seed=42)
    strategy = get_strategy("sma_crossover")(fast=5, slow=20)
    signals = strategy.generate_signals(bars)

    # The quick path still works with its default cost model.
    result = Backtester(BacktestConfig(initial_capital=INITIAL_CAPITAL)).run(bars, signals)
    assert len(result.equity) == len(bars)
    assert abs(float(result.equity.iloc[-1] - INITIAL_CAPITAL)) > 0.0  # it trades

    # The driver (zero cost) stays within tolerance of the zero-cost
    # vectorized baseline.
    _, driver_pnl = _driver_pnl(bars, strategy)
    assert abs(driver_pnl - _vectorized_pnl(bars, strategy)) < COST_TOLERANCE
