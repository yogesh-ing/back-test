"""The canonical backtest ≈ forward equivalency test (ticket P1.5).

This is the test that proves the migration's whole point: the same
strategy over the same bars produces matching P&L through

* (a) the backtest path — the vectorized :class:`Backtester`, which fills
  at the **previous close** with fractional, fully-invested positions, and
* (b) the forward path — :class:`PaperRunner` on the simulator executor,
  which fills at the **next bar's open** with share-based accounting.

The two fill anchors coincide ONLY on **gapless bars** (``open[t] ==
close[t-1]``), so the test constructs a deterministic gapless bar series
with a bounded per-bar move (``MAX_BAR_MOVE``). Both sides run
zero-cost, and the forward side is sized off total equity with a
``(1 + MAX_BAR_MOVE)`` gap margin so every entry stays funded at the
next-open fill price. The only structural differences left are the
≤ ``MAX_BAR_MOVE`` sizing margin and integer-share rounding — anything
larger is a second fill-timing or cost-model leak (ticket: stop and
report before continuing).

P2.1 landed ``BacktestDriver`` (``engine/backtest_driver.py``), which
shares the SAME engine loop as ``PaperRunner`` — so
``test_backtest_driver_matches_forward_same_data`` below runs the driver
through this same harness and asserts an EXACT P&L match (one loop, two
entry points). See ``instructions/refactoring-findings.md`` (F-08).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from backtest.engine.backtest_driver import BacktestDriver
from backtest.engine.backtester import BacktestConfig, Backtester
from backtest.forward.paper_runner import PaperRunner, _FrameSource, free_executor
from backtest.simulator.portfolio import Portfolio
from backtest.strategy.registry import get_strategy

INITIAL_CAPITAL = 100_000.0
N_BARS = 300
MAX_BAR_MOVE = 0.005  # per-bar move bound of the synthetic data
# Zero cost on both sides ⇒ tolerance absorbs only the ≤ MAX_BAR_MOVE
# sizing margin + integer-share rounding. A one-bar fill-timing leak on
# this data moves P&L by ~600–3000+, far above this.
COST_TOLERANCE = 0.005 * INITIAL_CAPITAL


def _gapless_bars(n: int = N_BARS, seed: int = 7, vmax: float = MAX_BAR_MOVE) -> pd.DataFrame:
    """Deterministic gapless bars: open[t] == close[t-1], |move| ≤ vmax.

    On these bars the Backtester's prev-close fill and the PaperRunner's
    next-open fill land on the SAME price — the precondition for the
    equivalency claim.
    """
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
            "volume": 1_000_000,  # never a liquidity constraint
        },
        index=pd.date_range("2021-01-01", periods=n, freq="B"),
    )


def _funded_all_in(symbol: str, price: float, portfolio: Portfolio) -> int:
    """All-in entry sizing that stays funded at the next-bar-open fill.

    Sizes off TOTAL EQUITY, not cash: while the previous position's close
    is still in flight, cash sits near zero while equity carries the full
    value. The ``(1 + MAX_BAR_MOVE)`` margin covers the gap between the
    sizing price (this bar's open) and the fill price (next bar's open).
    """
    if price <= 0:
        return 0
    equity = float(portfolio.calculate_total_equity())
    return int(equity / price / (1 + MAX_BAR_MOVE))


def _forward_pnl(bars: pd.DataFrame, strategy) -> float:
    portfolio = Portfolio(
        name="equivalency", initial_capital=INITIAL_CAPITAL, mode="paper", source="replay"
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


def _driver_pnl(bars: pd.DataFrame, strategy) -> float:
    portfolio = Portfolio(
        name="equivalency-driver", initial_capital=INITIAL_CAPITAL, mode="paper", source="replay"
    )
    driver = BacktestDriver(
        source=_FrameSource(bars),
        strategy=strategy,
        portfolio=portfolio,
        executor=free_executor(portfolio, max_participation="1"),
        symbols=["EQ"],
        start=bars.index[0].date().isoformat(),
        end=bars.index[-1].date().isoformat(),
        size_fn=_funded_all_in,
    )
    driver.run()
    return float(portfolio.calculate_total_equity()) - INITIAL_CAPITAL


def test_same_strategy_backtest_eq_forward_within_cost():
    bars = _gapless_bars()
    strategy = get_strategy("sma_crossover")(fast=5, slow=20)
    signals = strategy.generate_signals(bars)

    # (a) backtest path — zero-cost vectorized Backtester.
    backtest = Backtester(
        BacktestConfig(initial_capital=INITIAL_CAPITAL, commission_pct=0.0, slippage_pct=0.0)
    ).run(bars, signals)
    backtest_pnl = float(backtest.equity.iloc[-1] - INITIAL_CAPITAL)

    # (b) forward path — PaperRunner over the SAME bars, zero-cost executor.
    forward_pnl = _forward_pnl(bars, strategy)

    # The strategy must actually trade on both sides (no vacuous equality).
    held = signals.clip(0, 1).shift(1).fillna(0)
    turnover_events = int((held.diff().abs().fillna(0) > 0).sum())
    assert turnover_events >= 6  # at least three full round trips

    # Equally-timed signals + identical fill timing (gapless bars) + zero
    # cost ⇒ P&L matches within COST_TOLERANCE.
    assert abs(backtest_pnl - forward_pnl) < COST_TOLERANCE


def test_backtest_driver_matches_forward_same_data():
    """P2.1 leg of ticket P1.5 — BacktestDriver shares the engine loop.

    The driver and the forward runner now run the SAME
    ``run_engine_loop`` with identical inputs, so the match is EXACT
    (any difference is a loop divergence), and the P1.5 property holds
    against the vectorized Backtester as well.
    """
    bars = _gapless_bars()
    strategy = get_strategy("sma_crossover")(fast=5, slow=20)

    driver_pnl = _driver_pnl(bars, strategy)
    forward_pnl = _forward_pnl(bars, strategy)

    assert driver_pnl == forward_pnl  # one loop, two entry points
    signals = strategy.generate_signals(bars)
    backtest = Backtester(
        BacktestConfig(initial_capital=INITIAL_CAPITAL, commission_pct=0.0, slippage_pct=0.0)
    ).run(bars, signals)
    backtest_pnl = float(backtest.equity.iloc[-1] - INITIAL_CAPITAL)
    assert abs(driver_pnl - backtest_pnl) < COST_TOLERANCE
