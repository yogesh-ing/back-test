"""Forward paper-run tests (ticket P1.4).

The walk-forward loop now runs on the simulator executor through
:class:`PaperRunner` (fills at the NEXT bar's open, Decimal-exact cash),
so it no longer equals the vectorized close-fill ``Backtester`` — that
parity is the subject of ticket P1.5. What IS asserted here: the
walk-forward wrapper faithfully passes the engine through, equity curves
have the right shape, buckets stay isolated, snapshots round-trip and the
live loop resumes idempotently.
"""

import pandas as pd

from backtest.data.synthetic import SyntheticSource
from backtest.forward.paper_runner import (
    PaperRunner,
    StrategyPortfolio,
    _all_in_size,
    free_executor,
    load_state,
    run_live_papertrade,
    run_walkforward,
    save_state,
)
from backtest.simulator.portfolio import Portfolio
from backtest.strategy.registry import get_strategy

WINDOW = ("2021-01-01", "2024-01-01")


def test_walkforward_matches_direct_paper_runner():
    """The walk-forward wrapper must equal a raw PaperRunner on the same bars.

    Both paths fill at the next bar's open via the same zero-cost executor,
    so the per-bar equity curves have to line up exactly.
    """
    source = SyntheticSource()
    walk = run_walkforward(
        source, ["sma_crossover"], "DEMO", WINDOW[0], WINDOW[1],
        {"sma_crossover": 100_000.0},
    )

    # Direct engine run over identical data/sizing.
    candles = source.get_candles("DEMO", WINDOW[0], WINDOW[1], "day")
    portfolio = Portfolio(
        name="walk-sma_crossover",
        initial_capital=100_000.0,
        mode="paper",
        source="synthetic",
    )
    runner = PaperRunner(
        portfolio=portfolio,
        source=source,
        strategy=get_strategy("sma_crossover")(),
        executor=free_executor(portfolio, max_participation="1"),
        symbols=["DEMO"],
        start=WINDOW[0],
        end=WINDOW[1],
        size_fn=_all_in_size,
    )
    runner.run()
    direct = [float(p.total_equity) for p in portfolio.equity_history]

    walk_equity = walk["equity"]["sma_crossover"]
    assert len(walk_equity) == len(candles)
    assert direct == walk_equity
    # The strategy actually trades on this data (not a flat curve).
    assert max(walk_equity) != min(walk_equity)


def test_walkforward_total_equity_is_finite():
    source = SyntheticSource()
    walk = run_walkforward(
        source,
        ["sma_crossover", "rsi_reversion"],
        "DEMO",
        WINDOW[0],
        WINDOW[1],
        {"sma_crossover": 100_000.0, "rsi_reversion": 100_000.0},
    )
    assert walk["total_equity"] > 0
    equity = walk["equity"]["sma_crossover"]
    assert equity
    assert all(isinstance(v, float) and pd.notna(v) and v > 0 for v in equity)


def test_strategy_capital_isolation():
    source = SyntheticSource()
    walk = run_walkforward(
        source,
        ["sma_crossover", "rsi_reversion"],
        "DEMO",
        WINDOW[0],
        WINDOW[1],
        {"sma_crossover": 50_000.0, "rsi_reversion": 50_000.0},
    )

    assert set(walk["portfolio"].accounts) == {"sma_crossover", "rsi_reversion"}
    assert walk["portfolio"].accounts["sma_crossover"].cash >= 0
    assert walk["portfolio"].accounts["rsi_reversion"].cash >= 0
    assert walk["portfolio"].accounts["sma_crossover"].cash + walk["portfolio"].accounts["rsi_reversion"].cash >= 0


def test_portfolio_snapshot_roundtrip(tmp_path):
    portfolio = StrategyPortfolio({"sma_crossover": 100_000.0})
    portfolio.accounts["sma_crossover"].position = 1.0
    portfolio.accounts["sma_crossover"].cash = 90_000.0
    portfolio.mark_to_market({"sma_crossover": 105.0})

    path = tmp_path / "portfolio.json"
    save_state(portfolio, str(path))
    loaded = load_state(str(path))

    assert loaded.snapshot() == portfolio.snapshot()
    assert loaded.allocations == portfolio.allocations
    assert loaded.accounts["sma_crossover"].position == portfolio.accounts["sma_crossover"].position
    assert loaded.accounts["sma_crossover"].cash == portfolio.accounts["sma_crossover"].cash


def test_live_papertrade_state_resume_roundtrip(tmp_path):
    source = SyntheticSource()
    path = tmp_path / "live_state.json"

    first = run_live_papertrade(
        source,
        ["sma_crossover"],
        "DEMO",
        {"sma_crossover": 100_000.0},
        from_date="2021-01-01",
        to_date="2021-12-31",
        interval="day",
        state_file=str(path),
    )

    second = run_live_papertrade(
        source,
        ["sma_crossover"],
        "DEMO",
        {"sma_crossover": 100_000.0},
        from_date="2021-01-01",
        to_date="2021-12-31",
        interval="day",
        state_file=str(path),
    )

    assert first["portfolio"].snapshot() == second["portfolio"].snapshot()
    assert path.exists()
    assert second["state"]["resume_count"] >= 1
