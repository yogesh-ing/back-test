"""Canonical backtest entry tests (ticket #6 — backtest unification).

These pin the consolidation contract:

* :func:`backtest.engine.backtest_runner.run_backtest` is THE canonical
  driver run — its P&L equals a ``PaperRunner`` over the same bars (one
  shared loop) and it produces the same :class:`BacktestResult` shape the
  API's historical ``_run_driver`` produced.
* :func:`run_quick_screen` equals the legacy vectorized run + trim.
* The API modules delegate: no local engine bootstrap clones remain.
"""

from __future__ import annotations

import pytest

from backtest.data.synthetic import SyntheticSource
from backtest.engine import backtest_runner as canonical
from backtest.engine.backtest_driver import BacktestDriver
from backtest.engine.backtester import BacktestConfig, BacktestResult
from backtest.forward.paper_runner import PaperRunner, _all_in_size, _FrameSource, free_executor
from backtest.runner import run_on_candles
from backtest.simulator.portfolio import Portfolio
from backtest.strategy.registry import get_strategy

_SYMBOL = "DEMO"
_START, _END = "2021-01-01", "2024-01-01"
_PARAMS = {"fast": 10, "slow": 30}
_CAPITAL = 100_000


@pytest.fixture(scope="module")
def candles():
    return SyntheticSource().get_candles(_SYMBOL, _START, _END, "day")


def test_run_backtest_is_the_canonical_driver_entry(candles):
    """Engine-level run_backtest == BacktestDriver over the same bars, and
    the run classification tags land on the portfolio (mode/source)."""
    driver_portfolio = Portfolio(name="entry-check", initial_capital=_CAPITAL)
    driver = BacktestDriver(
        source=_FrameSource(candles),
        strategy=get_strategy("sma_crossover")(**_PARAMS),
        portfolio=driver_portfolio,
        executor=free_executor(driver_portfolio, max_participation="1"),
        symbols=[_SYMBOL],
        size_fn=_all_in_size,
    )
    driver.run()

    result = canonical.run_backtest(candles, "sma_crossover", _PARAMS, _SYMBOL, _CAPITAL)

    assert isinstance(result, BacktestResult)
    assert result.metrics["strategy"] == "sma_crossover"
    assert result.metrics["symbol"] == _SYMBOL
    assert len(result.equity) == len(candles)
    assert result.metrics["final_equity"] == pytest.approx(
        float(driver_portfolio.calculate_total_equity()), rel=1e-9
    )


def test_run_backtest_matches_paper_runner_pnl(candles):
    """Punchline of P2.1: backtest (canonical entry) and forward paper run
    are one loop — identical P&L on identical bars."""
    result = canonical.run_backtest(candles, "sma_crossover", _PARAMS, _SYMBOL, _CAPITAL)

    pf = Portfolio(name="check-forward", initial_capital=_CAPITAL)
    runner = PaperRunner(
        portfolio=pf,
        source=_FrameSource(candles),
        strategy=get_strategy("sma_crossover")(**_PARAMS),
        executor=free_executor(pf, max_participation="1"),
        symbols=[_SYMBOL],
        size_fn=_all_in_size,
    )
    runner.run()

    assert result.metrics["final_equity"] == pytest.approx(
        float(pf.calculate_total_equity()), rel=1e-9
    )


def test_run_quick_screen_equals_vectorized_plus_trim(candles):
    """The quick-screen entry = run_on_candles + trim_to_range (same shape,
    same numbers as the historical inline clone)."""
    result = canonical.run_quick_screen(
        candles,
        "sma_crossover",
        _PARAMS,
        _SYMBOL,
        _CAPITAL,
        _START,
        _END,
    )
    manual = run_on_candles(
        candles,
        "sma_crossover",
        _PARAMS,
        _SYMBOL,
        BacktestConfig(initial_capital=_CAPITAL),
    )
    manual = canonical.trim_to_range(manual, _START, _END)

    assert result.metrics == manual.metrics
    assert result.equity.equals(manual.equity)
    assert result.candles.equals(manual.candles)


def test_api_delegates_to_canonical_entry():
    """The API no longer owns an engine bootstrap: its 'driver' name IS the
    canonical function, and the cloned helpers are gone."""
    from backtest.api import backtest as api_bt
    from backtest.api import forward as api_fw

    assert api_bt._run_driver is canonical.run_backtest
    assert not hasattr(api_bt, "_interval")
    assert not hasattr(api_bt, "_trim_to_range")
    assert not hasattr(api_bt, "_run_driver_impl")
    assert not hasattr(api_fw, "_interval")
    assert not hasattr(api_fw, "_trim_to_range")


def test_shared_helpers_have_one_canonical_home():
    """FrameSource / free_executor / all_in_size are defined once; the paper
    runner only re-exports them under the historical private spellings."""
    from backtest.data.frame_source import FrameSource
    from backtest.forward.paper_runner import _all_in_size, _FrameSource
    from backtest.forward.paper_runner import free_executor as paper_free_executor
    from backtest.simulator.execution import free_executor
    from backtest.simulator.position_sizing import all_in_size

    assert _FrameSource is FrameSource
    assert _all_in_size is all_in_size
    assert paper_free_executor is free_executor
