"""Phase 4 — Cleanup: the legacy path is deleted, not deprecated (Plan v2.1).

Pins the end-state contract:

* no bridge-side direction lookup, no default expression, no auto-reentry
  machinery, no legacy exit-parser call on the trading path;
* a pre-v2 (v1) expression refuses at bridge construction — and a runner
  snapshot carrying one is skipped-and-logged on restore while its peers
  restore normally (Phase 2 per-runner isolation);
* spawn has no silent quantity clamp: selections are honoured or refused;
* an emitted direction with no rule in the spec raises in the template —
  it is never silently mapped by the bridge;
* the replay harness carries no default expression of its own.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

import backtest.forward.options_bridge as bridge_module
from backtest.forward.options_bridge import OptionsBridge
from backtest.options.quote_providers import SyntheticChainGenerator, SyntheticQuoteProvider
from backtest.strategy.execution import ExecutionSpecError
from backtest.strategy.intent import Direction, MarketView
from backtest.strategy.templates import DirectionalOptionStrategy


@pytest.fixture(autouse=True)
def _purge_fixture_strategies():
    from backtest.strategy import registry as _registry

    def _purge():
        for name in [n for n in _registry._REGISTRY if n.startswith("phase4")]:
            _registry.unregister(name)

    _purge()
    yield
    _purge()


def _provider():
    return SyntheticQuoteProvider(SyntheticChainGenerator())


def _view(direction: Direction) -> MarketView:
    return MarketView(
        direction=direction,
        confidence=0.9,
        underlying="NIFTY",
        spot_price=Decimal("24800"),
    )


# --------------------------------------------------------------------------- #
# Absence pins: deleted machinery stays deleted
# --------------------------------------------------------------------------- #


def test_deleted_bridge_machinery_stays_deleted():
    assert not hasattr(bridge_module, "DEFAULT_EXPRESSION")
    for name in (
        "_transitional_direction_rule_for",
        "_structure_type_for",
        "_should_reenter",
    ):
        assert not hasattr(OptionsBridge, name), name


def test_replay_harness_has_no_default_expression():
    from backtest.replay.harness import ReplayConfig

    assert not hasattr(bridge_module, "FORWARD_DEFAULT_EXPRESSION")
    import backtest.replay.harness as harness

    assert not hasattr(harness, "FORWARD_DEFAULT_EXPRESSION")
    assert ReplayConfig().expression == {}


def test_v1_and_missing_expressions_refuse_at_construction():
    with pytest.raises(ExecutionSpecError, match="Phase 4"):
        OptionsBridge(
            capital=1_000_000.0,
            expression={"type": "long_call"},
            quote_provider=_provider(),
        )
    with pytest.raises(ExecutionSpecError, match="Phase 4"):
        OptionsBridge(capital=1_000_000.0, quote_provider=_provider())


def test_bare_view_never_enters_end_state():
    bridge = OptionsBridge(
        capital=1_000_000.0,
        expression={
            "expression_version": 2,
            "type": {
                "BULLISH": {"structure": "long_call", "strike_selection": "atm"}
            },
            "quantity": 1,
            "exit": {"stop_loss_pct": 0.5, "min_days_to_expiry": None},
        },
        quote_provider=_provider(),
    )
    # NEUTRAL = silence, and even a directional bare view is silence:
    # entries arrive only as explicit intents.
    assert bridge.on_market_view(_view(Direction.NEUTRAL), "s") is None
    assert bridge.on_market_view(_view(Direction.BULLISH), "s") is None
    assert bridge.executed_count == 0


# --------------------------------------------------------------------------- #
# Unmapped emission: the template refuses loudly, the bridge never maps
# --------------------------------------------------------------------------- #


class Phase4UnmappedProbe(DirectionalOptionStrategy):
    name = "phase4_unmapped_probe"
    description = "Phase 4 unmapped-direction fixture"
    version = "1.0"
    author = "tests"
    eligible_instruments = ["NIFTY", "BANKNIFTY"]
    execution = {
        "emits": ["bullish", "neutral"],
        "structures": {
            "bullish": {"structure": "long_call", "strike_selection": "atm"}
        },
        "exit": {"stop_loss_pct": 0.30, "min_days_to_expiry": None},
        "quantity": 1,
    }

    def entries(self, candles):
        return None

    def generate_market_view(self, candles):
        return None


def test_unmapped_emitted_direction_raises_in_the_template():
    from backtest.strategy.registry import register

    register(Phase4UnmappedProbe)
    strategy = Phase4UnmappedProbe()
    # NEUTRAL remains silence…
    assert strategy.build_opening_intent(_view(Direction.NEUTRAL)) is None
    # …a mapped direction builds an intent…
    assert strategy.build_opening_intent(_view(Direction.BULLISH)) is not None
    # …and an emitted direction with no rule is a coded refusal, never a
    # bridge-side fallback (§11: STRATEGY_EMITS_UNDECLARED_DIRECTION).
    with pytest.raises(ExecutionSpecError, match="STRATEGY_EMITS_UNDECLARED_DIRECTION"):
        strategy.build_opening_intent(_view(Direction.BEARISH))


# --------------------------------------------------------------------------- #
# Restore: a pre-v2 snapshot runner is skipped, its peers restore
# --------------------------------------------------------------------------- #


def test_pre_v2_runner_snapshot_is_skipped_on_restore():
    from backtest.forward.paper_runner import OrderLedger, RunnerConfig, StrategyRunner
    from backtest.forward.portfolio_manager import reset_portfolio_manager
    from backtest.forward.state_store import capture_runner

    equity = StrategyRunner(
        RunnerConfig(
            name="EQ-OK", strategy_name="sma_crossover",
            allocated_capital=100_000, symbols=["NIFTY"], timeframe="1day",
        ),
        ledger=OrderLedger(),
    )
    option = StrategyRunner(
        RunnerConfig(
            name="OPT-V2", strategy_name="directional_options",
            allocated_capital=500_000, symbols=["NIFTY"], timeframe="1day",
            instrument={
                "type": "option",
                "expression": {
                    "expression_version": 2,
                    "type": {
                        "BULLISH": {"structure": "long_call", "strike_selection": "atm"}
                    },
                    "quantity": 1,
                    "exit": {"stop_loss_pct": 0.5, "min_days_to_expiry": None},
                },
            },
        ),
        ledger=OrderLedger(),
    )
    payload = {"runners": [capture_runner(equity), capture_runner(option)], "manager": {}}
    # Age the option snapshot back to a pre-v2 expression.
    payload["runners"][1]["config"]["instrument"] = {
        "type": "option",
        "expression": {"type": "long_call"},
    }

    manager = reset_portfolio_manager(auto_start_feed=False)
    try:
        restored = manager._apply_state_payload(payload)
        names = {r.config.name for r in manager._runners.values()}
        assert restored == 1
        assert names == {"EQ-OK"}, "the v1 runner is skipped, the equity runner restores"
    finally:
        manager.shutdown()
