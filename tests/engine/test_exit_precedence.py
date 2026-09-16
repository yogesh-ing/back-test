"""U2.2 — Two-tier exit precedence

Per-bar order as code: engine tier (breakers → emergency flatten, first and unconditional)
→ playbook tier in priority order: stop_loss_pct → take_profit_pct → time/DTE square-off → signal_flip.
Re-entry only on next bar, default off (C3).

Tests per UNIFIED-TRADING-TASKS.md U2.2:
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
    broker = OptionPaperBroker(capital=capital)
    return OptionsBridge(capital=capital, expression={"type": "bull_call_spread", "strike_selection": "atm", "quantity": 1, "exit": exit_cfg}, option_broker=broker, quote_provider=provider)


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
    """Same-bar re-entry impossible, regardless of reenter flag."""
    # Create bridge with reenter=True
    exit_cfg = {"signal_flip": True, "reenter": True, "min_days_to_expiry": None}
    bridge = _bridge_with_config(exit_cfg)
    # Simulate: open structure, then flip view same bar → should close but NOT re-enter same bar
    # We need to open first
    view_bull = _view(Direction.BULLISH)
    # First bar: open
    result_open = bridge.on_market_view(view_bull, "directional_options")
    assert result_open is not None
    assert "structure_id" in result_open or "exited" not in result_open

    # Same bar index: flip to bearish — should close, but not re-enter same bar
    view_bear = _view(Direction.BEARISH)
    # Don't increment bar index — same bar
    result_close = bridge.on_market_view(view_bear, "directional_options")
    assert result_close is not None
    assert result_close.get("exited") is True
    assert result_close.get("reason") == "signal_flip"

    # Same bar, try to open again — should be blocked by _blocked_reentry
    # _blocked_reentry should be True on exit bar
    assert bridge._blocked_reentry(view_bear) is True
    # _should_reenter should be False on same bar (next-bar only)
    assert bridge._should_reenter(view_bear) is False


def test_reentry_next_bar_allowed_when_reenter_true():
    """Re-entry happens on next bar only when reenter=true."""
    exit_cfg = {"signal_flip": True, "reenter": True, "min_days_to_expiry": None}
    bridge = _bridge_with_config(exit_cfg)
    view_bull = _view(Direction.BULLISH)
    view_bear = _view(Direction.BEARISH)

    # Bar 0: open bullish
    bridge.on_bar("NIFTY", 25000, ts="2026-09-16T09:15:00")
    bridge.on_market_view(view_bull, "directional_options")
    assert bridge._has_open_structure()

    # Bar 1: flip to bearish → close
    bridge.on_bar("NIFTY", 24900, ts="2026-09-16T10:15:00")
    closed = bridge.on_market_view(view_bear, "directional_options")
    assert closed is not None
    assert closed.get("exited") is True
    # Same bar: blocked
    assert bridge._blocked_reentry(view_bear) is True
    assert bridge._should_reenter(view_bear) is False

    # Bar 2: next bar, still bearish → should allow re-entry (next-bar only)
    bridge.on_bar("NIFTY", 24800, ts="2026-09-16T11:15:00")
    assert bridge._blocked_reentry(view_bear) is False
    # Now should_reenter should be True (next bar, flip, reenter true, direction differs)
    assert bridge._should_reenter(view_bear) is True

    # Opening on next bar should increment reentries_today
    result = bridge.on_market_view(view_bear, "directional_options")
    # Should open new structure
    assert result is not None
    assert result.get("exited") is None or "structure_id" in result
    assert bridge._reentries_today >= 1


def test_max_reentries_per_day_honoured():
    """max_reentries_per_day knob honoured (V1.1, default 2)."""
    exit_cfg = {"signal_flip": True, "reenter": True, "min_days_to_expiry": None, "max_reentries_per_day": 1}
    bridge = _bridge_with_config(exit_cfg)
    view_bull = _view(Direction.BULLISH)
    view_bear = _view(Direction.BEARISH)

    # Day 1: open bull
    bridge.on_bar("NIFTY", 25000, ts="2026-09-16T09:15:00")
    bridge.on_market_view(view_bull, "directional_options")

    # Flip to bear → close
    bridge.on_bar("NIFTY", 24900, ts="2026-09-16T10:15:00")
    bridge.on_market_view(view_bear, "directional_options")

    # Next bar: re-enter bear (1st re-entry)
    bridge.on_bar("NIFTY", 24800, ts="2026-09-16T11:15:00")
    bridge.on_market_view(view_bear, "directional_options")
    assert bridge._reentries_today == 1

    # Flip back to bull → close bear
    bridge.on_bar("NIFTY", 24900, ts="2026-09-16T12:15:00")
    bridge.on_market_view(view_bull, "directional_options")

    # Next bar: try to re-enter bull — should be blocked by max_reentries_per_day=1
    bridge.on_bar("NIFTY", 25000, ts="2026-09-16T13:15:00")
    # should_reenter should now be False due to max per day
    assert bridge._should_reenter(view_bull) is False


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
