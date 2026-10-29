"""V1 execution-gate whitelist (PRD §6) — forged intents must die at the gate.

The playbook/engine layers support spreads and short-option structures; the V1
contract does not. These tests prove the rejection happens at the **execution**
boundary (paper broker, live trader, bridge) rather than relying on the authoring
UI never offering those structures — including payloads built by hand, which is
what a forged API/imported config looks like from the broker's point of view.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from backtest.options.execution_gate import (
    REASON_LEG_COUNT,
    REASON_LEG_SIDE,
    REASON_OPTION_SIDE,
    REASON_STRUCTURE_TYPE,
    RESEARCH_STRUCTURES,
    StructureNotAllowedError,
    V1_ALLOWED_STRUCTURES,
    check_intent,
)
from backtest.options.live_trading import LiveOptionTrader
from backtest.options.paper_trading import FakeQuoteProvider, OptionPaperBroker
from backtest.strategy.intent import Direction, MarketView, OptionLeg, TradeIntent

EXPIRY = date(2026, 12, 29)


def _view(direction: Direction = Direction.BULLISH) -> MarketView:
    return MarketView(
        direction=direction,
        confidence=0.9,
        underlying="NIFTY",
        spot_price=Decimal("24800"),
    )


def _leg(side: str = "BUY", symbol: str = "NIFTY26DEC24800CE", token: str = "T1") -> OptionLeg:
    return OptionLeg(
        instrument_token=token,
        trading_symbol=symbol,
        side=side,
        quantity=1,
        lot_size=25,
    )


def _intent(structure_type: str, legs: tuple[OptionLeg, ...], **kwargs) -> TradeIntent:
    return TradeIntent(
        view=_view(),
        structure_type=structure_type,
        legs=legs,
        expiry=EXPIRY,
        estimated_premium=Decimal("150"),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Default posture: V1 only
# ---------------------------------------------------------------------------


def test_v1_defaults_to_long_call_and_long_put_only():
    assert V1_ALLOWED_STRUCTURES == {"long_call", "long_put"}


def test_long_call_intent_passes():
    broker = OptionPaperBroker(capital=1_000_000)
    positions = broker.execute_structure(_intent("long_call", (_leg(),)), FakeQuoteProvider())
    assert len(positions) == 1
    assert broker.get_open_structures()


def test_long_put_intent_passes():
    broker = OptionPaperBroker(capital=1_000_000)
    leg = _leg(symbol="NIFTY26DEC24800PE", token="T2")
    positions = broker.execute_structure(_intent("long_put", (leg,)), FakeQuoteProvider())
    assert len(positions) == 1


# ---------------------------------------------------------------------------
# Forged payloads
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "structure_type",
    ["bull_call_spread", "bear_put_spread", "straddle", "strangle", "iron_condor"],
)
def test_forged_non_v1_structure_type_rejected_at_paper_broker(structure_type: str):
    # One leg throughout: the rejection must be about the *structure type*,
    # not an incidental leg-count violation.
    broker = OptionPaperBroker(capital=1_000_000)
    with pytest.raises(StructureNotAllowedError) as exc:
        broker.execute_structure(_intent(structure_type, (_leg(),)), FakeQuoteProvider())
    assert exc.value.reason == REASON_STRUCTURE_TYPE
    assert broker.get_open_structures() == []


def test_forged_label_with_two_legs_rejected_as_multi_leg():
    """A payload claiming ``long_call`` but carrying two legs is still caught."""
    legs = (_leg(), _leg(symbol="NIFTY26DEC25200CE", token="T2"))
    broker = OptionPaperBroker(capital=1_000_000)
    with pytest.raises(StructureNotAllowedError) as exc:
        broker.execute_structure(_intent("long_call", legs), FakeQuoteProvider())
    assert exc.value.reason == REASON_LEG_COUNT


def test_forged_label_with_sell_leg_rejected_as_short_option():
    broker = OptionPaperBroker(capital=1_000_000)
    with pytest.raises(StructureNotAllowedError) as exc:
        broker.execute_structure(
            _intent("long_call", (_leg(side="SELL"),)), FakeQuoteProvider()
        )
    assert exc.value.reason == REASON_LEG_SIDE


def test_forged_put_inside_a_long_call_label_rejected():
    """``long_call`` with a PE contract behind it fails the option-side check."""
    broker = OptionPaperBroker(capital=1_000_000)
    with pytest.raises(StructureNotAllowedError) as exc:
        broker.execute_structure(
            _intent("long_call", (_leg(symbol="NIFTY26DEC24800PE"),)), FakeQuoteProvider()
        )
    assert exc.value.reason == REASON_OPTION_SIDE


def test_check_intent_is_pure_and_raises_for_the_same_payloads():
    with pytest.raises(StructureNotAllowedError):
        check_intent(_intent("strangle", (_leg(),)))


# ---------------------------------------------------------------------------
# Research profile is explicit, never the default
# ---------------------------------------------------------------------------


def test_research_profile_allows_the_wider_registry():
    assert "bull_call_spread" in RESEARCH_STRUCTURES
    broker = OptionPaperBroker(capital=1_000_000, allowed_structures=RESEARCH_STRUCTURES)
    legs = (_leg(), _leg(symbol="NIFTY26DEC25000CE", token="T2"))
    positions = broker.execute_structure(
        _intent("bull_call_spread", legs), FakeQuoteProvider()
    )
    assert len(positions) == 2


# ---------------------------------------------------------------------------
# Live adapter: gate runs before any broker call
# ---------------------------------------------------------------------------


class _RecordingBroker:
    """Duck-typed broker that records any attempt to place an order."""

    def __init__(self) -> None:
        self.placed: list = []

    def place_order(self, order):  # noqa: ANN001 - duck type
        self.placed.append(order)
        return "ORDER-1"


def test_live_trader_rejects_before_any_submission():
    """Gate runs ahead of the submit loop.

    Built with ``dry_run=True`` on purpose: arming this trader for real orders
    needs the environment kill-switch *and* the Cost & Risk panel gate
    (``segments_store.assert_live_arming_allowed``), which correctly refuses in a
    test environment. The gate under test is upstream of both, so a refusal here
    is a refusal with the broker wired either way.
    """
    broker = _RecordingBroker()
    trader = LiveOptionTrader(broker=broker, dry_run=True)
    with pytest.raises(StructureNotAllowedError) as exc:
        trader.execute_structure(_intent("bear_put_spread", (_leg(), _leg(token="T2"))))
    assert exc.value.reason == REASON_STRUCTURE_TYPE

    with pytest.raises(StructureNotAllowedError) as exc:
        trader.execute_structure(_intent("long_call", (_leg(side="SELL"),)))
    assert exc.value.reason == REASON_LEG_SIDE
    assert broker.placed == []


def test_live_trader_defaults_to_the_v1_profile():
    trader = LiveOptionTrader(broker=_RecordingBroker(), dry_run=True)
    assert trader.allowed_structures == V1_ALLOWED_STRUCTURES


# ---------------------------------------------------------------------------
# Bridge: the default expression is long premium, and a spread request is refused
# ---------------------------------------------------------------------------


_V2_SPREADS = {
    "expression_version": 2,
    "type": {
        "BULLISH": {"structure": "bull_call_spread", "strike_selection": "atm"},
        "BEARISH": {"structure": "bear_put_spread", "strike_selection": "atm"},
    },
    "quantity": 1,
    "exit": {
        "stop_loss_points": 250.0,
        "take_profit_pct": 0.5,
        "signal_flip": True,
        "min_days_to_expiry": None,
    },
}


def test_bridge_without_a_v2_expression_refuses():
    # Phase 4: the default expression is deleted — no expression, no bridge.
    from backtest.forward.options_bridge import OptionsBridge
    from backtest.strategy.execution import ExecutionSpecError

    with pytest.raises(ExecutionSpecError, match="Phase 4"):
        OptionsBridge(capital=1_000_000)


def test_bridge_refuses_a_spread_expression_with_a_logged_reason():
    # Spreads against the live entitlement (long premium only) refuse at
    # construction — the gate-aware validation fires before any bar.
    from backtest.forward.options_bridge import OptionsBridge
    from backtest.strategy.execution import ExecutionSpecError

    with pytest.raises(ExecutionSpecError, match="not permitted"):
        OptionsBridge(capital=1_000_000, expression=_V2_SPREADS, mode="live")


def test_bridge_research_profile_opens_a_spread():
    from backtest.forward.options_bridge import OptionsBridge

    bridge = OptionsBridge(
        capital=1_000_000,
        expression=_V2_SPREADS,
        allowed_structures=RESEARCH_STRUCTURES,
        option_broker=OptionPaperBroker(capital=1_000_000, allowed_structures=RESEARCH_STRUCTURES),
    )
    result = bridge.on_intent(
        {"structure": "bull_call_spread", "strike_selection": "atm"},
        _view(),
        "gate-test",
    )
    assert result is not None and result["structure_type"] == "bull_call_spread"
