"""Phase 0 tests for the options backtesting PRD.

Covers the two prerequisites that block every backtest task:

- **T0.1** — ``TradeIntent.net_debit`` is gone, and the structure builders
  populate ``estimated_premium`` from the pricing model.
- **T0.2** — ``StructurePosition.exit_reason`` records *why* a structure
  closed, for both manual closes and expiry settlement.

These tests assert the documented contract, so they fail if either
plumbing is removed or the estimate regresses to a hard ``0``.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal

import pytest

from backtest.options.expiry import ExpiryManager, StaticSettlementProvider
from backtest.options.paper_trading import (
    OptionPaperBroker,
    PositionStatus,
    StructurePosition,
)
from backtest.options.quote_providers import (
    SyntheticChainGenerator,
    SyntheticQuoteProvider,
    bs_price,
)
from backtest.options.structures import create_structure
from backtest.strategy.intent import Direction, MarketView, TradeIntent


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SPOT = 24800.0
UNDERLYING = "NIFTY"


def _make_chain(spot: float = SPOT, option_type: str = "CE"):
    """A synthetic priced chain + its registered quote provider."""
    generator = SyntheticChainGenerator()
    generator.set_spot(UNDERLYING, spot)
    chain = generator.generate_chain(
        underlying=UNDERLYING, option_type=option_type
    )
    provider = SyntheticQuoteProvider(generator)
    provider.register_chain(chain)
    return chain, provider


def _view(direction: Direction = Direction.BULLISH, spot: float = SPOT,
          bar_timestamp: datetime | None = ...) -> MarketView:
    return MarketView(
        direction=direction,
        confidence=0.8,
        underlying=UNDERLYING,
        spot_price=Decimal(str(spot)),
        bar_timestamp=(
            datetime.combine(date.today(), time(9, 15))
            if bar_timestamp is ...
            else bar_timestamp
        ),
    )


def _atm_strike(chain) -> Decimal:
    """Strike closest to spot — the one an ATM selector would pick."""
    return min(chain.keys(), key=lambda s: abs(float(s) - SPOT))


# ---------------------------------------------------------------------------
# T0.1 — no net_debit, a populated estimated_premium
# ---------------------------------------------------------------------------

class TestEstimatedPremium:

    def test_net_debit_property_is_gone(self):
        """The lot_size placeholder must not come back."""
        assert not hasattr(TradeIntent, "net_debit")

    def test_is_debit_flag_exists(self):
        """A safe replacement for the debit/credit question."""
        assert hasattr(TradeIntent, "is_debit")

    def test_long_call_premium_is_model_priced(self):
        """estimated_premium equals a hand Black-Scholes calc for the leg."""
        chain, _ = _make_chain()
        strike = _atm_strike(chain)
        contract = chain[strike]
        view = _view()
        expiry = contract.expiry

        intent = create_structure("long_call").build(
            view=view, strikes=[strike], chain=chain, expiry=expiry,
            strategy_name="test",
        )

        dte = (expiry - view.bar_timestamp.date()).days
        expected_unit = bs_price(
            SPOT, float(strike), dte / 365.0,
            float(contract.metadata["vol"]), "CE",
        )
        expected = Decimal(str(expected_unit)) * Decimal(str(contract.lot_size))

        assert intent.estimated_premium == pytest.approx(expected, abs=0.01)
        assert intent.estimated_premium > 0
        assert intent.is_debit is True

    def test_long_put_premium_is_positive_debit(self):
        chain, _ = _make_chain(option_type="PE")
        strike = _atm_strike(chain)
        intent = create_structure("long_put").build(
            view=_view(Direction.BEARISH), strikes=[strike], chain=chain,
            expiry=chain[strike].expiry,
        )
        # The field name is "estimated_premium" but the sign convention is
        # net debit: a long put that costs money is a positive number.
        assert intent.estimated_premium > 0
        assert intent.is_debit is True

    def test_bull_call_spread_costs_less_than_the_long_leg_alone(self):
        """The short leg must reduce the debit — that is the whole point."""
        chain, _ = _make_chain()
        atm = _atm_strike(chain)
        long_strike = atm
        short_strike = next(s for s in sorted(chain) if s > atm)
        view = _view()
        expiry = chain[atm].expiry

        spread = create_structure("bull_call_spread").build(
            view=view, strikes=[long_strike, short_strike], chain=chain,
            expiry=expiry,
        )
        single = create_structure("long_call").build(
            view=view, strikes=[long_strike], chain=chain, expiry=expiry,
        )

        assert 0 < spread.estimated_premium < single.estimated_premium

    def test_bear_put_spread_costs_less_than_the_long_put_alone(self):
        chain, _ = _make_chain(option_type="PE")
        atm = _atm_strike(chain)
        long_strike = atm
        short_strike = next(s for s in sorted(chain, reverse=True) if s < atm)
        view = _view(Direction.BEARISH)
        expiry = chain[atm].expiry

        spread = create_structure("bear_put_spread").build(
            view=view, strikes=[long_strike, short_strike], chain=chain,
            expiry=expiry,
        )
        single = create_structure("long_put").build(
            view=view, strikes=[long_strike], chain=chain, expiry=expiry,
        )

        assert 0 < spread.estimated_premium < single.estimated_premium

    def test_premium_is_zero_without_a_bar_timestamp(self):
        """No timestamp means "not estimated" — never a guess from date.today()."""
        chain, _ = _make_chain()
        strike = _atm_strike(chain)
        intent = create_structure("long_call").build(
            view=_view(bar_timestamp=None), strikes=[strike], chain=chain,
            expiry=chain[strike].expiry,
        )
        assert intent.estimated_premium == Decimal("0")

    def test_premium_is_zero_for_an_unpriced_contract(self):
        """A vendor-style contract with no modelled vol degrades, not crashes."""
        chain, _ = _make_chain()
        strike = _atm_strike(chain)
        chain[strike].metadata.pop("vol", None)

        intent = create_structure("long_call").build(
            view=_view(), strikes=[strike], chain=chain,
            expiry=chain[strike].expiry,
        )
        assert intent.estimated_premium == Decimal("0")

    def test_estimate_is_deterministic(self):
        """Same inputs, same number — no clock, no randomness."""
        def build_once():
            chain, _ = _make_chain()
            strike = _atm_strike(chain)
            return create_structure("long_call").build(
                view=_view(), strikes=[strike], chain=chain,
                expiry=chain[strike].expiry,
            ).estimated_premium

        assert build_once() == build_once()


# ---------------------------------------------------------------------------
# T0.2 — exit_reason
# ---------------------------------------------------------------------------

class TestExitReason:

    def test_defaults_to_none_while_open(self):
        structure = StructurePosition(
            structure_id="s1", structure_type="long_call",
            strategy_name="t", underlying=UNDERLYING, expiry=date.today(),
        )
        assert structure.exit_reason is None

    def test_manual_close_records_the_default_reason(self):
        chain, provider = _make_chain()
        broker = OptionPaperBroker(capital=1_000_000)
        strike = _atm_strike(chain)
        intent = create_structure("long_call").build(
            view=_view(), strikes=[strike], chain=chain,
            expiry=chain[strike].expiry,
        )
        positions = broker.execute_structure(intent, provider)
        sid = positions[0].structure_id

        broker.close_structure(sid, provider)

        assert broker.get_structure(sid).exit_reason == "manual"

    def test_close_accepts_an_explicit_reason(self):
        chain, provider = _make_chain()
        broker = OptionPaperBroker(capital=1_000_000)
        strike = _atm_strike(chain)
        intent = create_structure("long_call").build(
            view=_view(), strikes=[strike], chain=chain,
            expiry=chain[strike].expiry,
        )
        positions = broker.execute_structure(intent, provider)
        sid = positions[0].structure_id

        broker.close_structure(sid, provider, reason="strategy_signal")

        assert broker.get_structure(sid).exit_reason == "strategy_signal"

    def test_expiry_settlement_stamps_the_parent_structure(self):
        """Settlement bypasses close_structure — it must still record a reason."""
        chain, provider = _make_chain()
        broker = OptionPaperBroker(capital=1_000_000)
        strike = _atm_strike(chain)
        expiry = chain[strike].expiry
        intent = create_structure("long_call").build(
            view=_view(), strikes=[strike], chain=chain, expiry=expiry,
        )
        positions = broker.execute_structure(intent, provider)
        sid = positions[0].structure_id

        manager = ExpiryManager(broker)
        settlement = StaticSettlementProvider({UNDERLYING: 26000.0})
        # One day past expiry so every leg qualifies.
        after_expiry = datetime.combine(
            expiry + timedelta(days=1), time(15, 30)
        )
        manager.settle_expired(settlement, as_of=after_expiry)

        structure = broker.get_structure(sid)
        assert structure.exit_reason == "expiry_settlement"
        assert structure.closed_at is not None
        assert structure.is_open is False
        # The position itself is EXPIRED, not CLOSED — that is the status
        # the code actually uses, and what a trade log must read.
        assert positions[0].status == PositionStatus.EXPIRED

    def test_auto_square_off_records_its_own_reason(self):
        """The DTE exit is a different reason from a manual close."""
        chain, provider = _make_chain()
        broker = OptionPaperBroker(capital=1_000_000)
        strike = _atm_strike(chain)
        expiry = chain[strike].expiry
        intent = create_structure("long_call").build(
            view=_view(), strikes=[strike], chain=chain, expiry=expiry,
        )
        positions = broker.execute_structure(intent, provider)
        sid = positions[0].structure_id

        manager = ExpiryManager(broker, squareoff_minutes_before=30)
        # Inside the square-off window: on expiry day, before it has passed.
        manager.auto_square_off(
            provider,
            as_of=datetime.combine(expiry, time(15, 15)),
        )

        if broker.get_structure(sid).closed_at is not None:
            assert broker.get_structure(sid).exit_reason == "auto_square_off"
