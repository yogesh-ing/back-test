"""Phase 0 — strategy-owned execution contract (Plan v2.1 §4, §7).

Covers: spec parsing/canonicalisation, emits↔structures coverage, NEUTRAL
rules, strict-vs-legacy exit parsing, resolve/provenance, round-trip with
no key loss, base-class wiring, template resolution, catalogue block.
Zero live-behaviour change: nothing here touches the bridge.
"""

from __future__ import annotations

import copy

import pytest

from backtest.options.exit_policy import ExitConfig
from backtest.strategy.base import Strategy, StrategyContractError
from backtest.strategy.execution import (
    DirectionRule,
    ExecutionSpec,
    ExecutionSpecError,
    ExitPlan,
    canonical_direction,
    execution_summary,
    parse_exit_plan_strict,
    resolve_expression,
    validate_expression,
)
from backtest.strategy.intent import Direction, MarketView
from backtest.strategy.templates import DirectionalOptionStrategy

GOOD_DICT = {
    "emits": ["bullish", "bearish"],
    "structures": {
        "BULLISH": {"structure": "long_call", "strike_selection": "atm"},
        "bearish": "long_put",
    },
    "exit": {"stop_loss_pct": 0.30, "take_profit_pct": 1.0, "signal_flip": True},
    "quantity": 1,
    "min_quantity": 1,
    "max_quantity": 5,
    "allowed_overrides": ["quantity", "exit"],
}


def _spec(raw=GOOD_DICT):
    return ExecutionSpec.from_declared(copy.deepcopy(raw))


# --------------------------------------------------------------------------- #
# Parsing / canonicalisation
# --------------------------------------------------------------------------- #


def test_execution_spec_from_dict():
    spec = _spec()
    assert spec.emits == frozenset({Direction.BULLISH, Direction.BEARISH})
    assert spec.structures[Direction.BULLISH].structure == "long_call"
    assert spec.structures[Direction.BEARISH].structure == "long_put"  # string form
    assert (spec.default_lots, spec.min_lots, spec.max_lots) == (1, 1, 5)
    assert spec.exit_plan.stop_loss_pct == 0.30


@pytest.mark.parametrize("raw", ["NEUTRAL", "neutral", " Neutral ", "BULLISH", "bullish"])
def test_canonical_direction_single_spelling_point(raw):
    assert canonical_direction(raw) is Direction[raw.strip().upper()]


def test_canonical_direction_refuses_unknown():
    with pytest.raises(ExecutionSpecError):
        canonical_direction("sideways")


def test_spec_is_immutable():
    spec = _spec()
    with pytest.raises(TypeError):
        spec.structures[Direction.BULLISH] = DirectionRule(structure="long_put")


def test_unknown_keys_refused_everywhere():
    bad = copy.deepcopy(GOOD_DICT)
    bad["surprise"] = 1
    with pytest.raises(ExecutionSpecError):
        _spec(bad)
    bad2 = copy.deepcopy(GOOD_DICT)
    bad2["structures"]["bullish"] = {"structure": "long_call", "bogus": 1}
    with pytest.raises(ExecutionSpecError):
        _spec(bad2)


# --------------------------------------------------------------------------- #
# emits ↔ structures, NEUTRAL
# --------------------------------------------------------------------------- #


def test_execution_spec_requires_all_emits_directions():
    bad = copy.deepcopy(GOOD_DICT)
    del bad["structures"]["bearish"]
    with pytest.raises(ExecutionSpecError, match="missing"):
        _spec(bad)


def test_neutral_may_not_carry_a_structure_rule():
    bad = copy.deepcopy(GOOD_DICT)
    bad["emits"] = ["neutral"]
    bad["structures"] = {"neutral": "long_call"}
    with pytest.raises(ExecutionSpecError, match="NEUTRAL"):
        _spec(bad)


def test_direction_rule_for_neutral_is_silence_and_undeclared_is_breach():
    spec = _spec()
    assert spec.direction_rule_for(Direction.NEUTRAL) is None
    assert spec.direction_rule_for("BULLISH").structure == "long_call"
    only_bull = ExecutionSpec.from_declared(
        {
            "emits": ["bullish"],
            "structures": {"bullish": "long_call"},
            "exit": {"structure_capped": True},
        }
    )
    with pytest.raises(ExecutionSpecError, match="STRATEGY_EMITS_UNDECLARED_DIRECTION"):
        only_bull.direction_rule_for(Direction.BEARISH)


# --------------------------------------------------------------------------- #
# DirectionRule validation
# --------------------------------------------------------------------------- #


def test_rule_requires_companions_for_delta_and_fixed_distance():
    with pytest.raises(ExecutionSpecError):
        DirectionRule(structure="long_call", strike_selection="delta")
    with pytest.raises(ExecutionSpecError):
        DirectionRule(structure="long_call", strike_selection="delta", delta_target=1.5)
    with pytest.raises(ExecutionSpecError):
        DirectionRule(structure="strangle", strike_selection="fixed_distance")
    ok = DirectionRule(
        structure="strangle", strike_selection="fixed_distance", distance_pct=2.0
    )
    assert ok.to_dict()["distance_pct"] == 2.0


def test_unknown_structure_refused():
    with pytest.raises(ExecutionSpecError):
        DirectionRule(structure="iron_condor")  # not materialisable by the bridge today


# --------------------------------------------------------------------------- #
# ExitPlan: strict vs legacy
# --------------------------------------------------------------------------- #


def test_exit_plan_requires_exactly_one_hard_stop():
    with pytest.raises(ExecutionSpecError):
        ExitPlan()  # no stop at all
    with pytest.raises(ExecutionSpecError):
        ExitPlan(stop_loss_pct=0.3, stop_loss_points=100)  # two forms
    assert ExitPlan(stop_loss_pct=0.3).has_hard_stop
    assert ExitPlan(structure_capped=True).has_hard_stop


def test_strict_parser_refuses_unknown_keys_and_missing_stop():
    with pytest.raises(ExecutionSpecError, match="unknown exit keys"):
        parse_exit_plan_strict({"stop_loss_pct": 0.3, "stop_los_pct": 0.2})
    with pytest.raises(ExecutionSpecError):
        parse_exit_plan_strict({"take_profit_pct": 1.0})  # target without a stop
    with pytest.raises(ExecutionSpecError):
        parse_exit_plan_strict(None)


def test_legacy_parser_still_tolerant_for_v1():
    # The legacy contract is unchanged: warn-and-skip, never refuse (plan §4.3).
    cfg = ExitConfig.from_expression({"stop_los_pct": 0.2, "stop_loss_pct": 0.4})
    assert cfg.stop_loss_pct == 0.4


# --------------------------------------------------------------------------- #
# Expression round-trip / resolution / validation
# --------------------------------------------------------------------------- #


def test_expression_round_trip_preserves_data():
    spec = _spec()
    expr = spec.to_expression()
    assert expr["expression_version"] == 2
    assert validate_expression(expr, spec=spec) == []
    again = ExecutionSpec.from_declared(
        {
            "emits": [d.lower() for d in expr["type"]],
            "structures": expr["type"],
            "exit": expr["exit"],
            "quantity": expr["quantity"],
            "max_quantity": 5,
            "allowed_overrides": ["quantity", "exit"],
        }
    )
    assert again.to_expression() == expr


def test_resolve_expression_precedence_order():
    spec = _spec()
    resolved, provenance = resolve_expression(
        spec=spec,
        playbook_selections={"quantity": 2},
        user_selections={"quantity": 3},
    )
    assert resolved["quantity"] == 3  # user > playbook > strategy
    assert provenance["quantity"] == "user"
    assert provenance["type.BULLISH.structure"] == "strategy"
    assert provenance["exit.stop_loss_pct"] == "strategy"


def test_resolve_refuses_deletion_out_of_menu_and_undeclared_override():
    spec = _spec()
    with pytest.raises(ExecutionSpecError, match="cannot delete"):
        resolve_expression(spec=spec, user_selections={"quantity": None})
    with pytest.raises(ExecutionSpecError, match="outside the strategy's lot menu"):
        resolve_expression(spec=spec, user_selections={"quantity": 99})
    with pytest.raises(ExecutionSpecError, match="allowed_overrides"):
        resolve_expression(spec=spec, user_selections={"expiry_min_days": 3})


def test_resolve_preserves_legacy_expression_surface_keys():
    spec = _spec()
    resolved, provenance = resolve_expression(
        spec=spec,
        playbook_selections={
            "exit": {"take_profit_pct": 0.8},
        },
        user_selections={"quantity": 2},
    )
    # Strategy exit leaves survive a partial exit selection (leaf merge).
    assert resolved["exit"]["stop_loss_pct"] == 0.30
    assert resolved["exit"]["take_profit_pct"] == 0.8
    assert provenance["exit.take_profit_pct"] == "playbook"
    assert provenance["exit.stop_loss_pct"] == "strategy"


def test_validate_expression_gate_entitlement_view():
    expr = _spec().to_expression()
    errors = validate_expression(expr, permitted_structures=frozenset({"long_put"}))
    assert any("not permitted" in e for e in errors)
    bad = copy.deepcopy(expr)
    bad["exit"] = {"take_profit_pct": 1.0}
    assert any("hard stop" in e or "exactly one" in e for e in validate_expression(bad))


# --------------------------------------------------------------------------- #
# Base class + templates + catalogue
# --------------------------------------------------------------------------- #


class _SpecStrategy(DirectionalOptionStrategy):
    name = "phase0_test_strategy"
    description = "Phase 0 fixture"
    version = "1.0"
    author = "tests"
    execution = GOOD_DICT

    def entries(self, candles):
        return None

    def generate_market_view(self, candles):
        return MarketView(direction=Direction.BULLISH, confidence=1.0)


def test_strategy_execution_spec_validates_and_caches():
    _SpecStrategy.validate()  # must not raise
    first = _SpecStrategy.execution_spec()
    assert first is _SpecStrategy.execution_spec()  # cached per class
    assert _SpecStrategy.default_expression()["expression_version"] == 2


def test_strategy_with_invalid_spec_fails_validate():
    class Bad(DirectionalOptionStrategy):
        name = "phase0_bad_strategy"
        description = "x"
        version = "1.0"
        author = "tests"
        execution = {"emits": ["bullish"], "structures": {}, "exit": {}}

        def generate_market_view(self, candles):
            return None

    with pytest.raises(StrategyContractError):
        Bad.validate()


def test_strategy_without_execution_returns_none():
    class Legacy(Strategy):
        name = "phase0_legacy_strategy"
        description = "x"
        version = "1.0"
        author = "tests"
        params = {}

        def generate_signals(self, candles):
            return None

    assert Legacy.execution_spec() is None
    assert Legacy.default_expression() is None
    Legacy.validate()


def test_template_direction_rule_for_and_intent():
    strategy = _SpecStrategy()
    view = MarketView(direction=Direction.BULLISH, confidence=1.0)
    assert strategy.direction_rule_for(view).structure == "long_call"
    neutral = MarketView(direction=Direction.NEUTRAL, confidence=1.0)
    assert strategy.direction_rule_for(neutral) is None
    intent = strategy.build_opening_intent(view)
    assert intent["action"] == "OPEN" and intent["structure"] == "long_call"
    assert intent["exit"]["stop_loss_pct"] == 0.30
    assert strategy.build_opening_intent(neutral) is None


def test_execution_summary_for_catalogue():
    summary = execution_summary(_spec())
    assert "NEUTRAL" not in summary["summary"] or "no trade" in summary["summary"]
    assert summary["lots"] == {"default": 1, "min": 1, "max": 5}
    assert "BULLISH → long_call (atm)" in summary["summary"]
    assert execution_summary(None) is None
