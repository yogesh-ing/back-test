"""Phase 1 — strict bridge + one Risk Gate (Plan v2.1 §6, §7).

Strict mode is gated on ``expression_version == 2``: no DEFAULT merge, no
cross-direction fallback, coded edge-triggered halts, gate-supplied
entitlement, strict exit parsing. The legacy v1 path must behave exactly
as before. NEUTRAL is silence and never opens (plan §4.2).
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from backtest.forward.options_bridge import OptionsBridge
from backtest.options.execution_gate import (
    PreTradeRiskGate,
    StructureNotAllowedError,
)
from backtest.options.intent_journal import KIND_HALTED, MemoryJournal
from backtest.options.quote_providers import (
    SyntheticChainGenerator,
    SyntheticQuoteProvider,
)
from backtest.risk_limits import RiskLimits, load_risk_limits
from backtest.strategy.execution import ExecutionSpecError
from backtest.strategy.intent import Direction, MarketView

V2 = {
    "expression_version": 2,
    "type": {
        "BULLISH": {"structure": "long_call", "strike_selection": "atm"},
        "BEARISH": {"structure": "long_put", "strike_selection": "atm"},
    },
    "quantity": 1,
    "exit": {"stop_loss_pct": 0.30, "signal_flip": True, "min_days_to_expiry": None},
}


def _provider():
    return SyntheticQuoteProvider(SyntheticChainGenerator())


def _view(direction: Direction, spot: float = 24_800.0) -> MarketView:
    return MarketView(
        direction=direction, confidence=0.9, underlying="NIFTY",
        spot_price=Decimal(str(spot)),
    )


def _strict(expression=None, **kwargs) -> OptionsBridge:
    return OptionsBridge(
        capital=1_000_000.0,
        expression=expression if expression is not None else V2,
        quote_provider=_provider(),
        **kwargs,
    )


# --------------------------------------------------------------------------- #
# Gate (the one permission layer)
# --------------------------------------------------------------------------- #


def _limits(**overrides) -> RiskLimits:
    base = dict(
        per_trade_max_loss_inr=5000.0,
        per_runner_max_lots=10,
        per_runner_max_order_value_inr=200_000.0,
        entitlement={"paper": frozenset({"long_call", "long_put"})},
        kill_switch_engaged_on_boot=False,
        sha256="x" * 64,
    )
    base.update(overrides)
    return RiskLimits(**base)


def test_gate_entitlement_quantity_and_loss_ceilings():
    gate = PreTradeRiskGate(_limits(), mode="paper")
    gate.check_opening("long_call", quantity=2)  # permitted: no raise
    with pytest.raises(StructureNotAllowedError) as exc:
        gate.check_opening("bull_call_spread")
    assert exc.value.reason == "STRUCTURE_NOT_PERMITTED"
    with pytest.raises(StructureNotAllowedError) as exc:
        gate.check_opening("long_call", quantity=99)
    assert exc.value.reason == "QUANTITY_ABOVE_CEILING"
    with pytest.raises(StructureNotAllowedError) as exc:
        gate.check_opening("long_call", worst_case_loss_inr=999_999)
    assert exc.value.reason == "MAX_LOSS_EXCEEDED"


def test_gate_kill_switch_and_unknown_mode_permit_nothing():
    gate = PreTradeRiskGate(_limits(kill_switch_engaged_on_boot=True), mode="paper")
    with pytest.raises(StructureNotAllowedError) as exc:
        gate.check_opening("long_call")
    assert exc.value.reason == "KILL_SWITCH_ENGAGED"
    assert PreTradeRiskGate(_limits(), mode="mars").permitted_structures() == frozenset()


def test_gate_loads_shipped_rulebook_fail_closed():
    gate = PreTradeRiskGate(mode="live")  # loads config/risk_limits.yaml
    assert gate.permitted_structures() == frozenset({"long_call", "long_put"})
    assert load_risk_limits().sha256  # stamped at spawn in Phase 0


# --------------------------------------------------------------------------- #
# Strict construction
# --------------------------------------------------------------------------- #


def test_strict_rejects_invalid_expression_at_construction():
    bad = {**V2, "exit": {"take_profit_pct": 1.0}}  # no hard stop
    with pytest.raises(ExecutionSpecError):
        _strict(bad)
    bad2 = {
        **V2,
        "type": {"BULLISH": {"structure": "bull_call_spread", "strike_selection": "atm"}},
    }
    # live entitlement is long_call/long_put only -> gate-aware refusal up front
    with pytest.raises(ExecutionSpecError, match="not permitted"):
        _strict(bad2, mode="live")


def test_strict_does_not_merge_defaults():
    bridge = _strict()
    assert bridge.strict is True
    assert "strike_selection" not in bridge.expression  # no DEFAULT_EXPRESSION merge
    assert bridge.allowed_structures == frozenset(
        {"long_call", "long_put", "bull_call_spread", "bear_put_spread"}
    )  # paper entitlement from the rulebook, not V1 default


def test_legacy_path_unchanged():
    bridge = OptionsBridge(
        capital=1_000_000.0, expression={"type": "long_call"},
        quote_provider=_provider(),
    )
    assert bridge.strict is False
    assert bridge.expression["quantity"] == 1  # DEFAULT_EXPRESSION merged as before
    assert bridge._structure_type_for(_view(Direction.NEUTRAL)) == "long_call"


# --------------------------------------------------------------------------- #
# Strict runtime semantics (§4.2 table)
# --------------------------------------------------------------------------- #


def test_strict_neutral_is_silence_never_an_entry():
    bridge = _strict()
    assert bridge.on_market_view(_view(Direction.NEUTRAL), "t") is None
    assert bridge.executed_count == 0


def test_strict_no_cross_direction_fallback_and_halt_is_edge_triggered():
    expr = {
        **V2,
        "type": {"BULLISH": {"structure": "long_call", "strike_selection": "atm"}},
    }
    journal = MemoryJournal()
    bridge = _strict(expr, intent_journal=journal)
    first = bridge.on_market_view(_view(Direction.BEARISH), "t")
    assert first == {
        "rejected": True,
        "reason": "NO_DIRECTION_MAPPING",
        "code": "NO_DIRECTION_MAPPING",
    }
    # Subsequent bars: silent (no repeated journal spam), still no trade.
    assert bridge.on_market_view(_view(Direction.BEARISH), "t") is None
    assert bridge.executed_count == 0
    halts = [e for e in journal._events if e.kind == KIND_HALTED]
    assert len(halts) == 1


def test_strict_unmapped_neutral_does_not_halt():
    bridge = _strict()
    assert bridge.on_market_view(_view(Direction.NEUTRAL), "t") is None
    assert not bridge._halted_codes  # silence is not an incident


def test_strict_gate_refusal_at_trade_time_is_coded():
    gate = PreTradeRiskGate(
        _limits(entitlement={"paper": frozenset({"long_put"})}), mode="paper"
    )
    # Construction permitted it via validate? No — gate entitlement feeds
    # construction too, so build the bridge around a permissive gate, then
    # tighten: simulate by swapping the gate (entitlement changed intraday).
    bridge = _strict()
    bridge.risk_gate = gate
    result = bridge.on_market_view(_view(Direction.BULLISH), "t")
    assert result["rejected"] is True
    assert result["code"] == "STRUCTURE_NOT_PERMITTED"
    assert bridge.executed_count == 0


def test_transitional_lookup_is_bridge_side_and_template_never_needs_it():
    bridge = _strict()
    rule = bridge._transitional_direction_rule_for(_view(Direction.BULLISH))
    assert rule.structure == "long_call"
    assert bridge._transitional_direction_rule_for(_view(Direction.NEUTRAL)) is None


def test_operator_level_helper_renamed():
    bridge = OptionsBridge(
        capital=1_000_000.0, expression={"type": "long_call"},
        quote_provider=_provider(),
    )
    assert hasattr(bridge, "_operator_level_for")
    assert not hasattr(bridge, "_rule_for")
    assert bridge._operator_level_for("nope", 10.0) is None  # no open structure


def test_strict_per_direction_strike_selection_applied():
    expr = {
        **V2,
        "type": {
            "BULLISH": {
                "structure": "long_call",
                "strike_selection": "fixed_distance",
                "distance_pct": 2.0,
            }
        },
    }
    bridge = _strict(expr)
    bridge._transitional_direction_rule_for(_view(Direction.BULLISH))
    from decimal import Decimal as D

    strikes = [D(str(24_000 + 50 * i)) for i in range(41)]  # 24000..26000
    chain = {s: object() for s in strikes}
    picked = bridge._pick_strikes("long_call", D("24800"), chain, Direction.BULLISH)
    # 2% above 24800 = 25296 -> nearest listed strike 25300
    assert picked == [D("25300")]
