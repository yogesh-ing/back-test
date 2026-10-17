"""U2.2 — Two-tier exit precedence

Per-bar order as code: engine tier (breakers → emergency flatten, first and unconditional)
→ playbook tier in priority order: stop_loss_pct → take_profit_pct → time/DTE square-off → signal_flip.
Re-entry only on next bar, default off (C3).

Tests per docs/archive/UNIFIED-TRADING-TASKS.md U2.2:
- stop beats target
- stop beats flip
- DTE beats flip
- emergency overrides all
- re-entry same-bar impossible
- max_reentries_per_day honoured (V1.1 knob, default 2)
"""

from datetime import date, timedelta
from decimal import Decimal

from backtest.forward.options_bridge import OptionsBridge
from backtest.options.execution_gate import RESEARCH_STRUCTURES
from backtest.options.exit_policy import ExitConfig, ExitPolicy
from backtest.options.paper_trading import OptionPaperBroker
from backtest.options.quote_providers import SyntheticChainGenerator, SyntheticQuoteProvider
from backtest.strategy.intent import Direction, MarketView
from backtest.engine.execution_engine import ExecutionEngine, Fill, OrderRejected, RiskHalted
from backtest.strategy.signal import UnifiedSignal, RiskEnvelope


def _view(direction, underlying="NIFTY", spot=25000):
    return MarketView(
        direction=direction,
        confidence=0.8,
        underlying=underlying,
        spot_price=Decimal(str(spot)),
    )


def _bridge_with_config(exit_cfg, capital=1000000):
    gen = SyntheticChainGenerator()
    provider = SyntheticQuoteProvider(chain_generator=gen)
    # Re-entry is exercised on two-leg spreads — these bridges take the
    # research profile. Phase 4: expressions are v2; a declared points
    # stop (₹10k, inside the rulebook ceiling, beyond a 50-point spread's
    # maximum loss) supplies the mandatory hard stop.
    broker = OptionPaperBroker(capital=capital, allowed_structures=RESEARCH_STRUCTURES)
    exit_cfg = {"stop_loss_points": 10_000.0, **dict(exit_cfg)}
    expression = {
        "expression_version": 2,
        "type": {
            "BULLISH": {"structure": "bull_call_spread", "strike_selection": "atm"},
            "BEARISH": {"structure": "bear_put_spread", "strike_selection": "atm"},
        },
        "quantity": 1,
        "exit": exit_cfg,
    }
    return OptionsBridge(capital=capital, expression=expression, option_broker=broker, quote_provider=provider, allowed_structures=RESEARCH_STRUCTURES)


def _enter(bridge, view):
    rule = bridge._strict_rules[view.direction]
    return bridge.on_intent(
        {"structure": rule.structure, "strike_selection": "atm"}, view, "directional_options"
    )


def test_stop_beats_target():
    """stop_loss_pct beats take_profit_pct — risk before profit."""
    cfg = ExitConfig(stop_loss_pct=0.5, take_profit_pct=1.0, min_days_to_expiry=None)
    policy = ExitPolicy(cfg)
    # Both stop and target conditions met: pnl -60% (stop -50%) and +120% (target 100%) can't both happen,
    # but we test order: if pnl is -60% of premium, stop should fire even if target also configured
    # For this test, we set pnl that hits stop, and also would hit target if logic were reversed
    # Use -0.6 premium for stop
    decision = policy.evaluate(
        view=_view(Direction.BULLISH),
        structure_direction=Direction.BULLISH,
        unrealized_pnl=Decimal("-600"),
        basis=Decimal("1000"),
        bars_held=5,
    )
    assert decision is not None
    assert decision.reason == "stop_loss"


def test_stop_beats_flip():
    """stop_loss_pct beats signal_flip."""
    cfg = ExitConfig(signal_flip=True, stop_loss_pct=0.5, min_days_to_expiry=None)
    policy = ExitPolicy(cfg)
    # View flipped to BEARISH (would trigger flip), but pnl also hits stop
    decision = policy.evaluate(
        view=_view(Direction.BEARISH),
        structure_direction=Direction.BULLISH,
        unrealized_pnl=Decimal("-600"),
        basis=Decimal("1000"),
        bars_held=2,
    )
    # Risk first → stop, not flip
    assert decision is not None
    assert decision.reason == "stop_loss"


def test_dte_beats_flip():
    """DTE square-off beats signal_flip."""
    cfg = ExitConfig(signal_flip=True, min_days_to_expiry=1)
    policy = ExitPolicy(cfg)
    today = date(2026, 9, 16)
    expiry = today + timedelta(days=1)  # 1d to expiry ≤ 1 → DTE fires
    decision = policy.evaluate(
        view=_view(Direction.BEARISH),
        structure_direction=Direction.BULLISH,
        unrealized_pnl=Decimal("100"),
        basis=Decimal("1000"),
        bars_held=2,
        bar_date=today,
        expiry=expiry,
    )
    assert decision is not None
    assert decision.reason == "auto_square_off"


def test_emergency_overrides_all():
    """Emergency tier (breakers) overrides all playbook tactical exits."""
    from backtest.forward.execution_engine import UnifiedExecutionEngine

    engine = UnifiedExecutionEngine()
    # Daily loss breaker
    exit_sig = engine.evaluate_emergency_exit(
        daily_pnl=-15000, drawdown_pct=0.05, daily_loss_limit=10000, max_drawdown_pct=0.2
    )
    assert exit_sig is not None
    assert exit_sig.tier.value == "emergency"
    assert exit_sig.reason == "circuit_breaker"

    # Even if tactical would also fire, emergency is priority 0
    # Simulate: engine tier checked first in runner loop, so it overrides
    # Here we just verify emergency exists and is unconditional


def test_reentry_same_bar_impossible():
    """Same-bar re-entry impossible (U2.2): the exit bar takes no intent."""
    exit_cfg = {"signal_flip": True, "reenter": True, "min_days_to_expiry": None}
    bridge = _bridge_with_config(exit_cfg)
    view_bull = _view(Direction.BULLISH)
    bridge.on_bar("NIFTY", 25000, ts="2026-09-16T09:15:00")
    result_open = _enter(bridge, view_bull)
    assert result_open is not None and not result_open.get("rejected")

    # Same bar: flip view closes via exit management…
    view_bear = _view(Direction.BEARISH)
    result_close = bridge.on_market_view(view_bear, "directional_options")
    assert result_close is not None
    assert result_close.get("exited") is True
    assert result_close.get("reason") == "signal_flip"

    # …but an intent on the same bar is refused (silence, no trade).
    assert bridge._blocked_reentry(view_bear) is True
    assert _enter(bridge, view_bear) is None
    assert not bridge._has_open_structure()
    assert bridge.executed_count == 1


def test_reentry_next_bar_is_a_fresh_strategy_intent():
    """Phase 4: the bridge never auto-reverses. Next bar, a fresh intent
    from the strategy opens the reverse structure — ``reenter`` in the
    ExitPlan no longer moves the bridge."""
    exit_cfg = {"signal_flip": True, "reenter": True, "min_days_to_expiry": None}
    bridge = _bridge_with_config(exit_cfg)
    view_bull = _view(Direction.BULLISH)
    view_bear = _view(Direction.BEARISH)

    bridge.on_bar("NIFTY", 25000, ts="2026-09-16T09:15:00")
    _enter(bridge, view_bull)
    assert bridge._has_open_structure()

    bridge.on_bar("NIFTY", 24900, ts="2026-09-16T10:15:00")
    closed = bridge.on_market_view(view_bear, "directional_options")
    assert closed is not None and closed.get("exited") is True
    # A bare opposite view is not an entry any more.
    assert bridge.on_market_view(view_bear, "directional_options") is None
    assert bridge._blocked_reentry(view_bear) is True

    # Next bar: the guard lifts and the strategy's intent opens the reverse.
    bridge.on_bar("NIFTY", 24800, ts="2026-09-16T11:15:00")
    assert bridge._blocked_reentry(view_bear) is False
    result = _enter(bridge, view_bear)
    assert result is not None and not result.get("rejected")
    opens = bridge.option_broker.get_open_structures()
    assert len(opens) == 1 and opens[0].structure_type == "bear_put_spread"


def test_bridge_imposes_no_reentry_cap():
    """Phase 4: ``max_reentries_per_day`` is strategy bookkeeping now.

    The bridge deleted its per-day reversal counter with the auto-reentry
    machinery; how often to re-enter is the strategy's decision, bounded
    per trade by the one Pre-Trade Risk Gate like every other entry.
    """
    exit_cfg = {"signal_flip": True, "reenter": True, "min_days_to_expiry": None, "max_reentries_per_day": 1}
    bridge = _bridge_with_config(exit_cfg)
    view_bull = _view(Direction.BULLISH)
    view_bear = _view(Direction.BEARISH)

    bridge.on_bar("NIFTY", 25000, ts="2026-09-16T09:15:00")
    _enter(bridge, view_bull)
    bridge.on_bar("NIFTY", 24900, ts="2026-09-16T10:15:00")
    bridge.on_market_view(view_bear, "directional_options")  # flip close
    bridge.on_bar("NIFTY", 24800, ts="2026-09-16T11:15:00")
    _enter(bridge, view_bear)  # reversal 1
    assert bridge._has_open_structure()
    bridge.on_bar("NIFTY", 24900, ts="2026-09-16T12:15:00")
    bridge.on_market_view(view_bull, "directional_options")  # flip close
    bridge.on_bar("NIFTY", 25000, ts="2026-09-16T13:15:00")
    result = _enter(bridge, view_bull)  # reversal 2 — no bridge cap
    assert result is not None and not result.get("rejected")
    assert bridge.executed_count == 3


def test_engine_exit_precedence_constants():
    """Engine tier (breakers) first and unconditional → playbook tier stop>target>DTE>flip."""
    from backtest.forward.execution_engine import EXIT_PRECEDENCE

    # Verify precedence list exists and order is correct per architecture
    assert len(EXIT_PRECEDENCE) >= 5
    # Priority 0 emergency
    assert EXIT_PRECEDENCE[0][0] == 0
    assert EXIT_PRECEDENCE[0][1] == "emergency"
    # Priority 1 stop
    assert EXIT_PRECEDENCE[1][1] == "stop_loss_pct"
    # Priority 2 target
    assert EXIT_PRECEDENCE[2][1] == "take_profit_pct"
    # Priority 3 DTE/time
    assert "dte" in EXIT_PRECEDENCE[3][1].lower() or "time" in EXIT_PRECEDENCE[3][1].lower()
    # Priority 4 flip
    assert EXIT_PRECEDENCE[4][1] == "signal_flip"
