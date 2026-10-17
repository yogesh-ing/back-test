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


def _enter(bridge: OptionsBridge, view: MarketView, structure: str = "long_call"):
    """The end-state entry: an explicit intent, never a bare view."""
    return bridge.on_intent(
        {"structure": structure, "strike_selection": "atm"}, view, "t"
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
    assert bridge.allowed_structures == frozenset({"long_call", "long_put"})
    # The rulebook's paper entitlement is wider (spreads, strangle), but the
    # gate-facing set is narrowed to what this strategy declared — the
    # entitlement is a ceiling, not a menu (Phase 2).


def test_legacy_expression_refused_at_construction():
    # Phase 4: the legacy path is deleted, not deprecated — a v1
    # expression (or none) refuses at construction, and there is no
    # default expression left to merge.
    import backtest.forward.options_bridge as bridge_module

    assert not hasattr(bridge_module, "DEFAULT_EXPRESSION")
    with pytest.raises(ExecutionSpecError, match="Phase 4"):
        OptionsBridge(
            capital=1_000_000.0, expression={"type": "long_call"},
            quote_provider=_provider(),
        )
    with pytest.raises(ExecutionSpecError, match="Phase 4"):
        OptionsBridge(capital=1_000_000.0, quote_provider=_provider())


# --------------------------------------------------------------------------- #
# Strict runtime semantics (§4.2 table)
# --------------------------------------------------------------------------- #


def test_strict_neutral_is_silence_never_an_entry():
    bridge = _strict()
    assert bridge.on_market_view(_view(Direction.NEUTRAL), "t") is None
    assert bridge.executed_count == 0


def test_strict_no_cross_direction_fallback():
    # Phase 4 end-state: the bridge has no direction lookup at all. A
    # bare BEARISH view against a BULLISH-only expression is silence —
    # the coded refusal for an unmapped emission lives in the template
    # (build_opening_intent raises; pinned in test_phase4_cleanup.py).
    expr = {
        **V2,
        "type": {"BULLISH": {"structure": "long_call", "strike_selection": "atm"}},
    }
    journal = MemoryJournal()
    bridge = _strict(expr, intent_journal=journal)
    assert bridge.on_market_view(_view(Direction.BEARISH), "t") is None
    assert bridge.on_market_view(_view(Direction.BULLISH), "t") is None
    assert bridge.executed_count == 0


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
    result = _enter(bridge, _view(Direction.BULLISH))
    assert result["rejected"] is True
    assert result["code"] == "STRUCTURE_NOT_PERMITTED"
    assert bridge.executed_count == 0


def test_operator_level_helper_renamed():
    bridge = _strict()
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
    bridge._active_rule = bridge._strict_rules[Direction.BULLISH]
    from decimal import Decimal as D

    strikes = [D(str(24_000 + 50 * i)) for i in range(41)]  # 24000..26000
    chain = {s: object() for s in strikes}
    picked = bridge._pick_strikes("long_call", D("24800"), chain, Direction.BULLISH)
    # 2% above 24800 = 25296 -> nearest listed strike 25300
    assert picked == [D("25300")]


# --------------------------------------------------------------------------- #
# Phase 1 verification-flag dispositions (must close before any v2 runner)
# --------------------------------------------------------------------------- #


def test_price_only_stop_is_refused_never_dropped():
    # Flag 1: ExitConfig cannot represent stop_loss_price; a price-only
    # stop must refuse at construction, not trade unprotected.
    expr = {**V2, "exit": {"stop_loss_price": 42.0, "min_days_to_expiry": None}}
    with pytest.raises(ExecutionSpecError, match="stop_loss_price is refused"):
        _strict(expr)


def test_gate_ceilings_fire_at_entry_from_live_quotes():
    # Flag 2: worst-case (premium paid) was dormant when max_loss_inr was
    # undeclared. A ₹1 per-trade ceiling must now refuse a long call at
    # entry, from quoted premium — coded, nothing booked.
    gate = PreTradeRiskGate(_limits(per_trade_max_loss_inr=1.0), mode="paper")
    bridge = _strict(risk_gate=gate)
    result = _enter(bridge, _view(Direction.BULLISH))
    assert result["rejected"] is True
    assert result["code"] == "MAX_LOSS_EXCEEDED"
    assert bridge.executed_count == 0


def test_unpriceable_worst_case_cannot_trade():
    # Flag 2b: short legs + no declared max_loss_inr = unstateable worst
    # case = no trade (plan §4.3), raised by the entry gate check itself.
    from types import SimpleNamespace

    bridge = _strict()
    leg = SimpleNamespace(
        instrument_token="NOPE", side="SELL", quantity=1, lot_size=25
    )
    intent = SimpleNamespace(legs=[leg])
    with pytest.raises(StructureNotAllowedError) as exc:
        bridge._gate_check_at_entry(intent, "strangle")
    assert exc.value.reason == "WORST_CASE_UNPRICEABLE"


def test_strict_string_form_type_is_one_rule_for_all_directions():
    # Flag 4a: legacy string form inside a v2 envelope — one rule, every
    # (non-neutral) direction, no fallback involved.
    expr = {
        "expression_version": 2,
        "type": "long_call",
        "quantity": 1,
        "exit": {"structure_capped": True, "min_days_to_expiry": None},
    }
    bridge = _strict(expr)
    assert bridge.strict is True
    for direction in (Direction.BULLISH, Direction.BEARISH):
        assert bridge._strict_rules[direction].structure == "long_call"
    assert Direction.NEUTRAL not in bridge._strict_rules
