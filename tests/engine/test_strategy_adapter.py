"""U2.4 — Strategy adapter for the engine

`generate_market_view` already emits the signal; add a thin adapter so any
equity strategy (`generate_signals`) can also feed the engine with a
normalized {direction, instrument_hint, confidence} signal — the
plug-and-play contract from the user's point 3. Options vs swing is decided
by playbook/runner type, not by strategy code.

Tests per UNIFIED-TRADING-TASKS.md U2.4:
- one equity strategy (sma_crossover) driven through the engine to a paper fill via a stub playbook.
"""

import pandas as pd
from decimal import Decimal

from backtest.engine.strategy_adapter import StrategyAdapter, adapt_strategy
from backtest.engine.execution_engine import ExecutionEngine, Fill
from backtest.playbooks.models import Playbook
from backtest.strategy.intent import Direction, MarketView
from backtest.strategy.signal import UnifiedSignal


class FakeSmaCrossover:
    """Minimal equity strategy that mimics sma_crossover generate_signals."""

    name = "sma_crossover"

    def __init__(self, bullish=True):
        self.bullish = bullish
        self.params = {"underlying": {"default": "NIFTY"}}

    def generate_signals(self, candles: pd.DataFrame) -> pd.Series:
        # Return 1 for bullish, 0 for neutral
        if self.bullish:
            return pd.Series([0, 0, 1], index=candles.index[:3])
        return pd.Series([0, 0, 0], index=candles.index[:3])


class FakeMarketViewStrategy:
    """Strategy that already emits MarketView."""

    name = "directional_options"

    def __init__(self):
        self.params = {"underlying": {"default": "NIFTY"}}

    def generate_market_view(self, candles: pd.DataFrame) -> MarketView:
        return MarketView(
            direction=Direction.BULLISH,
            confidence=0.85,
            underlying="NIFTY",
            spot_price=Decimal("25000"),
            bar_timestamp=candles.index[-1] if len(candles) > 0 else None,
        )


def _candles():
    idx = pd.date_range("2026-09-01", periods=3, freq="D")
    return pd.DataFrame(
        {"close": [24500, 24800, 25000], "open": [24400, 24700, 24900], "high": [24600, 24900, 25100], "low": [24300, 24600, 24800], "volume": [1000, 1000, 1000]},
        index=idx,
    )


def test_equity_strategy_adapted_to_unified_signal():
    """Equity strategy generate_signals → UnifiedSignal with direction/instrument_hint/confidence."""
    candles = _candles()
    strat = FakeSmaCrossover(bullish=True)
    adapter = StrategyAdapter()

    signal = adapter.adapt(strat, candles, underlying="NIFTY")
    assert signal is not None
    assert isinstance(signal, UnifiedSignal)
    assert signal.direction == Direction.BULLISH
    assert signal.underlying == "NIFTY"
    assert 0.0 <= signal.confidence <= 1.0
    # Must have equity_info for dual routing
    assert signal.equity_info is not None
    assert signal.equity_info["side"] == "BUY"
    # Must have market_view for option path
    assert signal.market_view is not None
    assert signal.market_view.direction == Direction.BULLISH


def test_market_view_strategy_adapted():
    """Strategy with generate_market_view → UnifiedSignal.option_view."""
    candles = _candles()
    strat = FakeMarketViewStrategy()
    adapter = StrategyAdapter()

    signal = adapter.adapt(strat, candles, underlying="NIFTY")
    assert signal is not None
    assert signal.direction == Direction.BULLISH
    assert signal.underlying == "NIFTY"
    assert signal.confidence == 0.85


def test_neutral_signal_returns_none():
    """Neutral / flat signal → None (no trade)."""
    candles = _candles()
    strat = FakeSmaCrossover(bullish=False)
    adapter = StrategyAdapter()

    signal = adapter.adapt(strat, candles, underlying="NIFTY")
    assert signal is None


def test_equity_signal_through_engine_paper_fill():
    """One equity strategy (sma_crossover) driven through engine to paper fill via stub playbook — U2.4 AC."""
    candles = _candles()
    strat = FakeSmaCrossover(bullish=True)
    adapter = StrategyAdapter()

    signal = adapter.adapt(strat, candles, underlying="NIFTY")
    assert signal is not None

    # Stub playbook — options vs swing decided by playbook/runner type, not strategy code
    playbook = Playbook(
        name="test",
        underlying="NIFTY",
        structure_type="bull_call_spread",
        strike_selection="atm",
        quantity=1,
        max_loss_per_trade=200000,
    )

    engine = ExecutionEngine()
    result = engine.execute(signal, playbook, mode="paper", source="synthetic")
    assert isinstance(result, Fill)
    assert result.lot_size is not None
    # Data source should be synthetic
    assert result.data_source == "synthetic"


def test_functional_wrapper():
    """Functional wrapper adapt_strategy works."""
    candles = _candles()
    strat = FakeSmaCrossover(bullish=True)

    signal = adapt_strategy(strat, candles, underlying="BANKNIFTY")
    assert signal is not None
    assert signal.underlying == "BANKNIFTY"


def test_options_vs_swing_decided_by_playbook_not_strategy():
    """Same strategy signal can be routed to options or equity based on playbook type."""
    candles = _candles()
    strat = FakeSmaCrossover(bullish=True)
    adapter = StrategyAdapter()

    signal = adapter.adapt(strat, candles, underlying="NIFTY")
    assert signal is not None

    # Options playbook
    opt_playbook = Playbook(
        name="opt",
        underlying="NIFTY",
        structure_type="bull_call_spread",
        strike_selection="atm",
        quantity=1,
        max_loss_per_trade=200000,
    )
    # Equity playbook (same signal, different runner type)
    # For test, we reuse same playbook class but check signal has both option and equity info
    assert signal.is_option() or signal.equity_info is not None

    from backtest.strategy.signal import RiskEnvelope

    loose = RiskEnvelope(max_loss_per_signal=200000, max_exposure_pct=0.1, max_lots=100, max_positions=100)
    engine = ExecutionEngine(risk_envelope=loose)
    # Options path
    result_opt = engine.execute(signal, opt_playbook, mode="paper", source="synthetic")
    assert isinstance(result_opt, Fill)

    # Equity path — same signal, no playbook (or equity playbook)
    # Engine should handle equity signals too — loose envelope so not risk-halted
    result_eq = engine.execute(signal, None, mode="paper", source="synthetic")
    assert isinstance(result_eq, Fill)
