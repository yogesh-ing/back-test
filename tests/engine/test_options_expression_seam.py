"""Tests for the options expression seam (task A3).

The seam converts a ``MarketView`` plus an option chain into a
``TradeIntent`` through the real selector → structure builder chain.  It is
the first production caller of that path, so these tests are the contract
that the architecture is actually reachable outside of tests.

They also pin two failure modes that a whole PRD revision got wrong:

* comparing ``view.direction`` to a **string** (always false → a silently
  empty backtest), and
* asking for a structure the chain shape cannot represent.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal

import pytest

from backtest.engine.option_backtest_driver import (
    PHASE_A_STRUCTURES,
    UnsupportedStructureError,
    build_intent_from_view,
    default_structure_decider,
)
from backtest.options.quote_providers import SyntheticChainGenerator
from backtest.strategy.intent import Direction, MarketView


SPOT = 24800.0
UNDERLYING = "NIFTY"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _chain(option_type: str = "CE", strikes_each_side: int = 10):
    generator = SyntheticChainGenerator()
    generator.set_spot(UNDERLYING, SPOT)
    return generator.generate_chain(
        underlying=UNDERLYING,
        strikes_each_side=strikes_each_side,
        option_type=option_type,
    )


def _expiry(chain) -> date:
    return next(iter(chain.values())).expiry


def _bar_timestamp() -> datetime:
    return datetime.combine(date.today(), time(9, 15))


def _view(
    direction: Direction,
    confidence: float = 0.9,
    spot: float = SPOT,
) -> MarketView:
    return MarketView(
        direction=direction,
        confidence=confidence,
        underlying=UNDERLYING,
        spot_price=Decimal(str(spot)),
        bar_timestamp=_bar_timestamp(),
    )


# ---------------------------------------------------------------------------
# No-trade paths
# ---------------------------------------------------------------------------

class TestNoTradePaths:

    def test_none_view_yields_none(self):
        assert build_intent_from_view(None, _chain(), date(2026, 9, 24)) is None

    def test_neutral_view_yields_none(self):
        """A neutral view must not produce a trade — and this is the test
        that catches a string-vs-enum comparison regressing."""
        chain = _chain()
        assert build_intent_from_view(
            _view(Direction.NEUTRAL), chain, _expiry(chain)
        ) is None

    def test_empty_chain_yields_none(self):
        assert build_intent_from_view(
            _view(Direction.BULLISH), {}, date(2026, 9, 24)
        ) is None

    def test_missing_spot_yields_none(self):
        """A zero spot would silently select the lowest strike."""
        chain = _chain()
        assert build_intent_from_view(
            _view(Direction.BULLISH, spot=0.0), chain, _expiry(chain)
        ) is None

    def test_thin_chain_cannot_build_a_spread(self):
        """Two-strike structures need two strikes; one is not enough."""
        chain = _chain(strikes_each_side=0)  # exactly one strike
        assert len(chain) == 1
        intent = build_intent_from_view(
            _view(Direction.BULLISH, confidence=0.3),  # → bull_call_spread
            chain,
            _expiry(chain),
        )
        assert intent is None

    def test_decider_can_decline(self):
        chain = _chain()
        assert build_intent_from_view(
            _view(Direction.BULLISH),
            chain,
            _expiry(chain),
            decider=lambda view: None,
        ) is None


# ---------------------------------------------------------------------------
# View → structure mapping
# ---------------------------------------------------------------------------

class TestViewToStructure:

    def test_bullish_high_conviction_buys_outright_call(self):
        chain = _chain("CE")
        intent = build_intent_from_view(
            _view(Direction.BULLISH, confidence=0.9), chain, _expiry(chain)
        )
        assert intent is not None
        assert intent.structure_type == "long_call"
        assert len(intent.legs) == 1
        assert intent.legs[0].side == "BUY"

    def test_bullish_low_conviction_buys_a_spread(self):
        chain = _chain("CE")
        intent = build_intent_from_view(
            _view(Direction.BULLISH, confidence=0.4), chain, _expiry(chain)
        )
        assert intent is not None
        assert intent.structure_type == "bull_call_spread"
        assert len(intent.legs) == 2
        assert {leg.side for leg in intent.legs} == {"BUY", "SELL"}

    def test_bearish_high_conviction_buys_outright_put(self):
        chain = _chain("PE")
        intent = build_intent_from_view(
            _view(Direction.BEARISH, confidence=0.9), chain, _expiry(chain)
        )
        assert intent is not None
        assert intent.structure_type == "long_put"
        assert len(intent.legs) == 1

    def test_bearish_low_conviction_buys_a_spread(self):
        chain = _chain("PE")
        intent = build_intent_from_view(
            _view(Direction.BEARISH, confidence=0.4), chain, _expiry(chain)
        )
        assert intent is not None
        assert intent.structure_type == "bear_put_spread"
        assert len(intent.legs) == 2

    def test_default_decider_is_pure(self):
        """No clock, no randomness — the same view always maps the same way."""
        view = _view(Direction.BULLISH, confidence=0.5)
        assert default_structure_decider(view) == default_structure_decider(view)
        assert default_decider_result_is_phase_a(view)

    def test_every_mapping_stays_inside_phase_a(self):
        for direction in Direction:
            for confidence in (0.1, 0.5, 0.7, 0.95):
                chosen = default_structure_decider(
                    _view(direction, confidence=confidence)
                )
                if chosen is not None:
                    assert chosen in PHASE_A_STRUCTURES


def default_decider_result_is_phase_a(view: MarketView) -> bool:
    chosen = default_structure_decider(view)
    return chosen is None or chosen in PHASE_A_STRUCTURES


# ---------------------------------------------------------------------------
# Scope enforcement
# ---------------------------------------------------------------------------

class TestScopeEnforcement:

    @pytest.mark.parametrize(
        "structure_type",
        ["straddle", "strangle", "iron_condor", "calendar_spread"],
    )
    def test_out_of_scope_structures_raise(self, structure_type):
        """Phase B structures fail loudly, naming the chain-shape reason."""
        chain = _chain()
        with pytest.raises(UnsupportedStructureError) as excinfo:
            build_intent_from_view(
                _view(Direction.BULLISH),
                chain,
                _expiry(chain),
                decider=lambda view: structure_type,
            )
        message = str(excinfo.value)
        assert structure_type in message
        # The error must explain *why*, not just say "unknown structure".
        assert "chain" in message.lower()

    def test_unsupported_error_is_a_value_error(self):
        """Callers catching ValueError keep working."""
        assert issubclass(UnsupportedStructureError, ValueError)

    def test_phase_a_structures_are_all_buildable(self):
        """The advertised set and the implemented set must not drift."""
        chain = _chain()
        for structure_type in sorted(PHASE_A_STRUCTURES):
            intent = build_intent_from_view(
                _view(Direction.BULLISH),
                chain,
                _expiry(chain),
                decider=lambda view, st=structure_type: st,
            )
            assert intent is not None, f"{structure_type} produced no intent"
            assert intent.structure_type == structure_type


# ---------------------------------------------------------------------------
# Integration with the real chain and the rest of the pipeline
# ---------------------------------------------------------------------------

class TestPipelineIntegration:

    def test_legs_reference_real_contracts(self):
        """Every leg must point at a token that exists in the chain."""
        chain = _chain("CE")
        intent = build_intent_from_view(
            _view(Direction.BULLISH, confidence=0.4), chain, _expiry(chain)
        )
        tokens = {c.instrument_token for c in chain.values()}
        assert intent is not None
        for leg in intent.legs:
            assert leg.instrument_token in tokens

    def test_selected_strikes_come_from_the_chain(self):
        chain = _chain("CE")
        intent = build_intent_from_view(
            _view(Direction.BULLISH, confidence=0.4), chain, _expiry(chain)
        )
        assert intent is not None
        for leg in intent.legs:  # metadata carries the per-leg strike map
            assert Decimal(intent.metadata["strikes"][leg.trading_symbol]) in chain

    def test_estimated_premium_is_populated(self):
        """T0.1 and A3 must work together — the builder prices the legs."""
        chain = _chain("CE")
        intent = build_intent_from_view(
            _view(Direction.BULLISH), chain, _expiry(chain)
        )
        assert intent is not None
        assert intent.estimated_premium > 0

    def test_expiry_is_carried_onto_the_intent(self):
        chain = _chain("CE")
        expiry = _expiry(chain)
        intent = build_intent_from_view(
            _view(Direction.BULLISH), chain, expiry
        )
        assert intent is not None
        assert intent.expiry == expiry

    def test_strategy_name_is_recorded(self):
        chain = _chain("CE")
        intent = build_intent_from_view(
            _view(Direction.BULLISH),
            chain,
            _expiry(chain),
            strategy_name="directional_options",
        )
        assert intent is not None
        assert intent.strategy_name == "directional_options"

    def test_alternative_selector_and_kwargs(self):
        """The seam is selector-agnostic — fixed distance changes the strikes."""
        chain = _chain("CE")
        atm = build_intent_from_view(
            _view(Direction.BULLISH), chain, _expiry(chain)
        )
        far = build_intent_from_view(
            _view(Direction.BULLISH),
            chain,
            _expiry(chain),
            selector_type="fixed_distance",
            selector_kwargs={"distance_pct": 2.0},
        )
        assert atm is not None and far is not None
        atm_strike = Decimal(atm.metadata["strikes"][atm.legs[0].trading_symbol])
        far_strike = Decimal(far.metadata["strikes"][far.legs[0].trading_symbol])
        assert far_strike != atm_strike

    def test_seam_is_deterministic(self):
        def once():
            chain = _chain("CE")
            intent = build_intent_from_view(
                _view(Direction.BULLISH, confidence=0.4),
                chain,
                _expiry(chain),
            )
            return (
                intent.structure_type,
                tuple(
                    (leg.trading_symbol, leg.side, leg.quantity)
                    for leg in intent.legs
                ),
                intent.estimated_premium,
            )

        assert once() == once()
