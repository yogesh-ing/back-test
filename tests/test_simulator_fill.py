"""Tests for the Fill model and commission models (Step 6).

Fills are the ground truth of a run — positions, cash and the equity curve are
all derived from them — so the accounting tests here are the ones that matter
most. In particular: fees must be counted exactly once, and slippage must
never be double-counted into cash (it is already inside ``fill_price``).
"""

from __future__ import annotations

import dataclasses
import json
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from backtest.db.manager import DatabaseManager
from backtest.db.models import Base
from backtest.simulator import (
    Fill,
    FlatCommission,
    LiquidityFlag,
    Order,
    OrderStatus,
    PercentageCommission,
    PerShareCommission,
    Portfolio,
    PortfolioLimits,
    Position,
    PositionAction,
    TieredCommission,
    ValidationError,
    ZeroCommission,
    resolve_commission_model,
)

D = Decimal
UTC = timezone.utc


@pytest.fixture()
def db():
    manager = DatabaseManager.from_env(profile="testing", url="sqlite:///:memory:")
    manager.connect()
    Base.metadata.create_all(manager.engine)
    yield manager
    manager.disconnect()


def mkfill(**kw) -> Fill:
    base = dict(symbol="INFY", side="buy", quantity=10, fill_price=100)
    base.update(kw)
    return Fill(**base)


# ===========================================================================
# Commission models
# ===========================================================================


class TestZeroCommission:
    def test_always_zero(self):
        assert ZeroCommission().calculate(1000, 5000) == D("0")


class TestFlatCommission:
    def test_size_independent(self):
        model = FlatCommission(per_trade=20)
        assert model.calculate(1, 100) == D("20.0000")
        assert model.calculate(10_000, 5_000) == D("20.0000")

    def test_negative_rejected(self):
        with pytest.raises(ValidationError, match="must not be negative"):
            FlatCommission(per_trade=-1)


class TestPerShareCommission:
    def test_scales_with_shares(self):
        model = PerShareCommission(per_share=D("0.01"), minimum=None)
        assert model.calculate(100, 500) == D("1.0000")
        assert model.calculate(1000, 500) == D("10.0000")

    def test_price_is_irrelevant(self):
        """This is the model's defining weakness: cheap stocks cost the same."""
        model = PerShareCommission(per_share=D("0.01"), minimum=None)
        assert model.calculate(1000, 2) == model.calculate(1000, 2000)

    def test_minimum_applies(self):
        model = PerShareCommission(per_share=D("0.005"), minimum=D("20"))
        assert model.calculate(10, 100) == D("20.0000")

    def test_maximum_applies(self):
        model = PerShareCommission(per_share=D("0.01"), minimum=None, maximum=D("50"))
        assert model.calculate(100_000, 100) == D("50.0000")

    def test_max_below_min_rejected(self):
        with pytest.raises(ValidationError, match="maximum must be >= minimum"):
            PerShareCommission(per_share=D("0.01"), minimum=D("20"), maximum=D("5"))


class TestPercentageCommission:
    def test_basic_rate(self):
        model = PercentageCommission(rate=D("0.0003"), maximum=None)
        assert model.calculate(100, 500) == D("15.0000")  # 0.03% of 50,000

    def test_cap_models_the_indian_discount_broker(self):
        """'0.03% or Rs 20, whichever is lower'."""
        model = PercentageCommission(rate=D("0.0003"), maximum=D("20"))
        assert model.calculate(1000, 5000) == D("20.0000")

    def test_percentage_units_mistake_is_caught(self):
        """0.03 meaning '0.03%' rather than 3% is a classic slip."""
        with pytest.raises(ValidationError, match="fractional, not a percentage"):
            PercentageCommission(rate=D("3"))

    def test_negative_rate_rejected(self):
        with pytest.raises(ValidationError, match="must not be negative"):
            PercentageCommission(rate=D("-0.001"))


class TestTieredCommission:
    @pytest.fixture()
    def model(self):
        return TieredCommission(
            [(0, D("0.0005")), (100_000, D("0.0003")), (1_000_000, D("0.0001"))]
        )

    def test_selects_the_right_tier(self, model):
        assert model.rate_for(50_000) == D("0.0005")
        assert model.rate_for(100_000) == D("0.0003")  # boundary is inclusive
        assert model.rate_for(500_000) == D("0.0003")
        assert model.rate_for(5_000_000) == D("0.0001")

    def test_whole_trade_charged_at_the_selected_rate(self, model):
        """Selected-rate, not marginal — the retail convention."""
        assert model.calculate(3000, 500) == D("150.0000")  # 1.5M x 0.0001

    def test_tiers_are_sorted_automatically(self):
        model = TieredCommission([(1_000_000, D("0.0001")), (0, D("0.0005"))])
        assert model.rate_for(10) == D("0.0005")

    def test_first_tier_must_start_at_zero(self):
        with pytest.raises(ValidationError, match="must be 0"):
            TieredCommission([(1000, D("0.0005"))])

    def test_empty_tiers_rejected(self):
        with pytest.raises(ValidationError, match="at least one tier"):
            TieredCommission([])

    def test_duplicate_thresholds_rejected(self):
        with pytest.raises(ValidationError, match="unique"):
            TieredCommission([(0, D("0.0005")), (0, D("0.0003"))])

    def test_rate_above_one_rejected(self):
        with pytest.raises(ValidationError, match="between 0 and 1"):
            TieredCommission([(0, D("2"))])


class TestResolveCommissionModel:
    def test_passthrough(self):
        model = PercentageCommission()
        assert resolve_commission_model(model) is model

    def test_none_gives_zero(self):
        assert isinstance(resolve_commission_model(None), ZeroCommission)

    def test_from_name(self):
        assert isinstance(resolve_commission_model("percentage"), PercentageCommission)

    def test_from_dict(self):
        model = resolve_commission_model({"model": "percentage", "rate": "0.001"})
        assert model.rate == D("0.001")

    def test_from_number_is_flat(self):
        assert resolve_commission_model(25).calculate(1, 1) == D("25.0000")

    def test_unknown_name_lists_options(self):
        with pytest.raises(ValidationError, match="expected one of"):
            resolve_commission_model("astrology")

    def test_tiered_by_name_alone_is_refused(self):
        with pytest.raises(ValidationError, match="needs explicit tiers"):
            resolve_commission_model("tiered")

    def test_bad_kwargs_reported(self):
        with pytest.raises(ValidationError, match="bad configuration"):
            resolve_commission_model({"model": "flat", "nonsense": 1})

    def test_round_trip_through_dict(self):
        original = TieredCommission([(0, D("0.0005")), (100_000, D("0.0003"))])
        rebuilt = resolve_commission_model(original.to_dict())
        assert rebuilt.rate_for(200_000) == original.rate_for(200_000)


# ===========================================================================
# Fill construction and validation
# ===========================================================================


class TestFillConstruction:
    def test_defaults(self):
        f = mkfill()
        assert f.symbol == "INFY" and f.is_buy
        assert f.gross_value == D("1000.0000")
        assert f.total_fees == D("0.0000")
        assert f.fill_id

    def test_symbol_normalised(self):
        assert mkfill(symbol=" infy ").symbol == "INFY"

    @pytest.mark.parametrize(
        "kwargs, match",
        [
            (dict(quantity=0), "must be positive"),
            (dict(quantity=-5), "side"),
            (dict(fill_price=0), "must be positive"),
            (dict(fill_price=-1), "must be positive"),
            (dict(commission=-1), "must not be negative"),
            (dict(exchange_fees=-1), "must not be negative"),
            (dict(regulatory_fees=-1), "must not be negative"),
            (dict(reference_price=0), "must be positive"),
            (dict(symbol="  "), "symbol"),
            (dict(side="sideways"), "invalid"),
            (dict(liquidity_flag="ghost"), "liquidity_flag"),
        ],
    )
    def test_invalid_inputs(self, kwargs, match):
        with pytest.raises(ValidationError, match=match):
            mkfill(**kwargs)

    def test_liquidity_flags_accepted(self):
        for flag in LiquidityFlag.ALL:
            assert mkfill(liquidity_flag=flag).liquidity_flag == flag

    def test_is_frozen(self):
        """An execution is a historical fact; nothing may rewrite it."""
        f = mkfill()
        with pytest.raises(dataclasses.FrozenInstanceError):
            f.fill_price = D("999")
        with pytest.raises(dataclasses.FrozenInstanceError):
            f.commission = D("999")

    def test_with_position_returns_a_copy(self):
        f = mkfill()
        linked = f.with_position("pos-1")
        assert linked is not f
        assert linked.position_id == "pos-1"
        assert f.position_id is None


# ===========================================================================
# Costs and slippage
# ===========================================================================


class TestCosts:
    def test_buy_total_cost_adds_fees(self):
        f = mkfill(side="buy", commission=5, exchange_fees=2, regulatory_fees=3)
        assert f.total_fees == D("10.0000")
        assert f.calculate_total_cost() == D("1010.0000")
        assert f.calculate_cash_delta() == D("-1010.0000")

    def test_sell_proceeds_subtract_fees(self):
        f = mkfill(side="sell", commission=5, exchange_fees=2, regulatory_fees=3)
        assert f.calculate_total_cost() == D("990.0000")
        assert f.calculate_cash_delta() == D("990.0000")

    def test_net_price_moves_against_you_both_ways(self):
        buy = mkfill(side="buy", commission=10)
        sell = mkfill(side="sell", commission=10)
        assert buy.calculate_net_price() == D("101.00000000")
        assert sell.calculate_net_price() == D("99.00000000")

    def test_signed_quantity(self):
        assert mkfill(side="buy").signed_quantity == D("10.00000000")
        assert mkfill(side="sell").signed_quantity == D("-10.00000000")


class TestSlippage:
    def test_buy_above_reference_is_adverse(self):
        f = mkfill(side="buy", fill_price=101, reference_price=100)
        assert f.slippage_per_share == D("1.00000000")
        assert f.slippage_amount == D("10.0000")
        assert f.slippage_bps == D("100.000000")

    def test_sell_below_reference_is_adverse(self):
        """Positive means 'worse' for either side."""
        f = mkfill(side="sell", fill_price=99, reference_price=100)
        assert f.slippage_per_share == D("1.00000000")
        assert f.slippage_bps == D("100.000000")

    def test_favourable_slippage_is_negative(self):
        assert mkfill(side="buy", fill_price=99, reference_price=100).slippage_bps < 0
        assert mkfill(side="sell", fill_price=101, reference_price=100).slippage_bps < 0

    def test_zero_without_a_reference(self):
        f = mkfill(fill_price=101)
        assert f.slippage_bps == D("0") and f.slippage_amount == D("0.0000")

    def test_slippage_is_not_a_fee(self):
        """It is already inside fill_price; counting it as cash double-counts."""
        f = mkfill(side="buy", fill_price=101, reference_price=100, commission=5)
        assert f.total_fees == D("5.0000")
        assert f.calculate_total_cost() == D("1015.0000")  # 10x101 + 5
        assert f.slippage_amount == D("10.0000")  # tracked separately
        assert f.total_cost_of_trading == D("15.0000")  # attribution only


# ===========================================================================
# Position impact
# ===========================================================================


class TestPositionImpact:
    def test_preview_open(self):
        impact = mkfill().impact_on_position(None)
        assert impact.action == PositionAction.OPEN
        assert impact.resulting_quantity == D("10.00000000")

    def test_preview_increase(self):
        pos = Position(symbol="INFY", quantity=10, average_entry_price=100)
        impact = mkfill(side="buy", quantity=5).impact_on_position(pos)
        assert impact.action == PositionAction.INCREASE
        assert impact.resulting_quantity == D("15.00000000")

    def test_preview_reduce(self):
        pos = Position(symbol="INFY", quantity=10, average_entry_price=100)
        impact = mkfill(side="sell", quantity=4, fill_price=110).impact_on_position(pos)
        assert impact.action == PositionAction.REDUCE
        assert impact.realized_pnl == D("40.0000")
        assert not impact.fully_closed

    def test_preview_close(self):
        pos = Position(symbol="INFY", quantity=10, average_entry_price=100)
        impact = mkfill(side="sell", quantity=10, fill_price=110).impact_on_position(pos)
        assert impact.action == PositionAction.CLOSE
        assert impact.fully_closed
        assert impact.realized_pnl == D("100.0000")

    def test_preview_reverse_is_flagged(self):
        pos = Position(symbol="INFY", quantity=10, average_entry_price=100)
        impact = mkfill(side="sell", quantity=15).impact_on_position(pos)
        assert impact.action == PositionAction.REVERSE

    def test_preview_does_not_mutate(self):
        pos = Position(symbol="INFY", quantity=10, average_entry_price=100)
        mkfill(side="sell", quantity=4, fill_price=110).impact_on_position(pos)
        assert pos.quantity == D("10.00000000")
        assert pos.realized_pnl == D("0.0000")

    def test_short_position_preview(self):
        pos = Position(symbol="INFY", quantity=-10, average_entry_price=100)
        impact = mkfill(side="buy", quantity=10, fill_price=90).impact_on_position(pos)
        assert impact.action == PositionAction.CLOSE
        assert impact.realized_pnl == D("100.0000")

    def test_apply_increase_mutates(self):
        pos = Position(symbol="INFY", quantity=10, average_entry_price=100)
        mkfill(side="buy", quantity=10, fill_price=120).apply_to_position(pos)
        assert pos.quantity == D("20.00000000")
        assert pos.average_entry_price == D("110.00000000")

    def test_apply_close_mutates(self):
        pos = Position(symbol="INFY", quantity=10, average_entry_price=100)
        result = mkfill(side="sell", quantity=10, fill_price=110).apply_to_position(pos)
        assert result.fully_closed and not pos.is_open

    def test_apply_links_the_position_id(self):
        pos = Position(symbol="INFY", quantity=10, average_entry_price=100)
        f = mkfill(side="buy", quantity=5)
        f.apply_to_position(pos)
        assert f.position_id == pos.position_id

    def test_apply_reversal_is_refused(self):
        """Flipping long to short in one step hides a sizing bug."""
        pos = Position(symbol="INFY", quantity=10, average_entry_price=100)
        with pytest.raises(ValidationError, match="reverse the position"):
            mkfill(side="sell", quantity=15).apply_to_position(pos)

    def test_symbol_mismatch_refused(self):
        pos = Position(symbol="TCS", quantity=10, average_entry_price=100)
        with pytest.raises(ValidationError, match="does not match"):
            mkfill(symbol="INFY").apply_to_position(pos)

    def test_closed_position_refused(self):
        pos = Position(symbol="INFY", quantity=10, average_entry_price=100)
        pos.close(110)
        with pytest.raises(ValidationError, match="closed position"):
            mkfill(side="buy").apply_to_position(pos)


# ===========================================================================
# Fill.from_order
# ===========================================================================


class TestFromOrder:
    def test_prices_commission_and_advances_the_order(self):
        o = Order.market("INFY", "buy", 10)
        o.submit()
        f = Fill.from_order(
            o,
            fill_price=1500,
            commission_model=PercentageCommission(rate=D("0.0003"), maximum=None),
        )
        assert f.commission == D("4.5000")
        assert o.status is OrderStatus.FILLED
        assert f.order_id == o.order_id

    def test_partial_fill(self):
        o = Order.market("INFY", "buy", 10)
        o.submit()
        Fill.from_order(o, quantity=4, fill_price=1500)
        assert o.status is OrderStatus.PARTIAL
        assert o.remaining_quantity == D("6.00000000")

    def test_defaults_to_remaining_quantity(self):
        o = Order.market("INFY", "buy", 10)
        o.submit()
        o.add_fill(quantity=3, fill_price=1500)
        f = Fill.from_order(o, fill_price=1500)
        assert f.quantity == D("7.00000000")
        assert o.status is OrderStatus.FILLED

    def test_overfill_refused_before_anything_changes(self):
        o = Order.market("INFY", "buy", 10)
        o.submit()
        with pytest.raises(ValidationError, match="exceed"):
            Fill.from_order(o, quantity=11, fill_price=1500)
        assert o.filled_quantity == D("0E-8")
        assert o.status is OrderStatus.PENDING

    def test_unsubmitted_order_refused(self):
        with pytest.raises(ValidationError, match="not working"):
            Fill.from_order(Order.market("INFY", "buy", 10), fill_price=1500)

    def test_terminal_order_refused(self):
        o = Order.market("INFY", "buy", 10)
        o.submit()
        o.cancel("x")
        with pytest.raises(ValidationError, match="not working"):
            Fill.from_order(o, fill_price=1500)

    def test_apply_to_order_can_be_disabled(self):
        o = Order.market("INFY", "buy", 10)
        o.submit()
        Fill.from_order(o, fill_price=1500, apply_to_order=False)
        assert o.status is OrderStatus.PENDING

    def test_limit_price_used_when_no_price_given(self):
        o = Order.limit("INFY", "buy", 10, 1500)
        o.submit()
        assert Fill.from_order(o).fill_price == D("1500.00000000")

    def test_missing_price_reported(self):
        o = Order.market("INFY", "buy", 10)
        o.submit()
        with pytest.raises(ValidationError, match="fill_price is required"):
            Fill.from_order(o)

    def test_strategy_name_is_carried_over(self):
        o = Order.market("INFY", "buy", 10, strategy_name="sma")
        o.submit()
        assert Fill.from_order(o, fill_price=1500).strategy_name == "sma"

    def test_several_partials_accumulate_on_the_order(self):
        o = Order.market("INFY", "buy", 10)
        o.submit()
        for _ in range(5):
            Fill.from_order(o, quantity=2, fill_price=1500)
        assert o.status is OrderStatus.FILLED
        assert len(o.fills) == 5


# ===========================================================================
# Portfolio.apply_fill
# ===========================================================================


class TestPortfolioApplyFill:
    def test_opens_a_position_and_settles_cash(self):
        p = Portfolio(name="p", initial_capital=100_000)
        f = mkfill(side="buy", quantity=10, fill_price=1500, commission=5)
        impact = p.apply_fill(f)
        assert impact.action == PositionAction.OPEN
        assert p.current_cash == D("84995.0000")
        assert p.get_position("INFY").quantity == D("10.00000000")

    def test_equity_drops_only_by_fees_on_entry(self):
        p = Portfolio(name="p", initial_capital=100_000)
        p.apply_fill(mkfill(side="buy", quantity=10, fill_price=1500, commission=5))
        assert p.calculate_total_equity() == D("99995.0000")

    def test_round_trip_reconciles(self):
        p = Portfolio(name="p", initial_capital=100_000)
        p.apply_fill(mkfill(side="buy", quantity=10, fill_price=1500, commission=5))
        p.apply_fill(mkfill(side="sell", quantity=10, fill_price=1520, commission=5))
        assert p.realized_pnl == D("200.0000")
        assert p.total_commission == D("10.0000")
        assert p.calculate_total_equity() == D("100190.0000")
        assert p.current_cash == p.calculate_total_equity()

    def test_increase_then_partial_close(self):
        p = Portfolio(name="p", initial_capital=100_000)
        p.apply_fill(mkfill(side="buy", quantity=10, fill_price=100))
        p.apply_fill(mkfill(side="buy", quantity=10, fill_price=120))
        pos = p.get_position("INFY")
        assert pos.quantity == D("20.00000000")
        assert pos.average_entry_price == D("110.00000000")
        p.apply_fill(mkfill(side="sell", quantity=5, fill_price=130))
        assert p.realized_pnl == D("100.0000")  # 5 x (130-110)

    def test_closing_retires_the_position(self):
        p = Portfolio(name="p", initial_capital=100_000)
        p.apply_fill(mkfill(side="buy", quantity=10, fill_price=100))
        p.apply_fill(mkfill(side="sell", quantity=10, fill_price=110))
        assert p.positions == {}
        assert len(p.closed_positions) == 1

    def test_short_entry_credits_cash(self):
        p = Portfolio(
            name="p",
            initial_capital=100_000,
            limits=PortfolioLimits(allow_short=True, max_gross_exposure_pct=D("2")),
        )
        p.apply_fill(mkfill(side="sell", quantity=10, fill_price=100, commission=5))
        assert p.current_cash == D("100995.0000")
        assert p.get_position("INFY").is_short
        assert p.calculate_total_equity() == D("99995.0000")

    def test_limits_are_enforced_on_open(self):
        p = Portfolio(name="p", initial_capital=1_000)
        from backtest.simulator import InsufficientFundsError

        with pytest.raises(InsufficientFundsError):
            p.apply_fill(mkfill(side="buy", quantity=100, fill_price=1000))

    def test_shorting_blocked_by_default(self):
        from backtest.simulator import ShortSellingNotAllowedError

        p = Portfolio(name="p", initial_capital=100_000)
        with pytest.raises(ShortSellingNotAllowedError):
            p.apply_fill(mkfill(side="sell", quantity=10, fill_price=100))

    def test_validate_false_bypasses_limits(self):
        p = Portfolio(name="p", initial_capital=1_000)
        p.apply_fill(mkfill(side="buy", quantity=100, fill_price=1000), validate=False)
        assert p.has_position("INFY")

    def test_reversal_refused(self):
        p = Portfolio(name="p", initial_capital=100_000)
        p.apply_fill(mkfill(side="buy", quantity=10, fill_price=100))
        with pytest.raises(ValidationError, match="reverse the position"):
            p.apply_fill(mkfill(side="sell", quantity=15, fill_price=100))

    def test_fees_counted_exactly_once(self):
        p = Portfolio(name="p", initial_capital=100_000)
        p.apply_fill(
            mkfill(
                side="buy",
                quantity=10,
                fill_price=100,
                commission=3,
                exchange_fees=1,
                regulatory_fees=1,
            )
        )
        assert p.total_commission == D("5.0000")
        assert p.current_cash == D("98995.0000")  # 100000 - 1000 - 5

    def test_end_to_end_order_fill_portfolio(self):
        p = Portfolio(name="p", initial_capital=100_000)
        o = p.add_order(Order.market("INFY", "buy", 10, portfolio_id=p.portfolio_id))
        o.submit()
        f = Fill.from_order(o, fill_price=1500, commission_model=FlatCommission(per_trade=20))
        p.apply_fill(f)
        p.sync_orders()
        assert o.status is OrderStatus.FILLED
        assert p.pending_orders == [] and len(p.filled_orders) == 1
        assert p.current_cash == D("84980.0000")
        assert f.position_id == p.get_position("INFY").position_id

    def test_many_round_trips_reconcile_exactly(self):
        p = Portfolio(name="p", initial_capital=100_000)
        for _ in range(100):
            p.apply_fill(
                mkfill(side="buy", quantity=3, fill_price=D("100.1"), commission=D("0.33"))
            )
            p.apply_fill(
                mkfill(side="sell", quantity=3, fill_price=D("100.2"), commission=D("0.33"))
            )
        assert p.realized_pnl == D("30.0000")
        assert p.total_commission == D("66.0000")
        assert p.calculate_total_equity() == D("99964.0000")


# ===========================================================================
# Serialisation
# ===========================================================================


class TestSerialisation:
    def test_round_trip(self):
        f = mkfill(
            side="sell",
            fill_price=101,
            reference_price=100,
            commission=5,
            exchange_fees=1,
            regulatory_fees=2,
            liquidity_flag="maker",
            order_id="o1",
            position_id="p1",
        )
        restored = Fill.from_dict(f.to_dict())
        assert restored.fill_id == f.fill_id
        assert restored.side is f.side
        assert restored.commission == f.commission
        assert restored.slippage_bps == f.slippage_bps

    def test_survives_json(self):
        f = mkfill(quantity=D("3.14159265"), fill_price=D("1234.56789012"))
        restored = Fill.from_dict(json.loads(json.dumps(f.to_dict())))
        assert restored.quantity == D("3.14159265")
        assert restored.fill_price == D("1234.56789012")

    def test_slippage_is_recomputed_not_trusted(self):
        """A hand-edited snapshot must not smuggle in bad attribution."""
        f = mkfill(side="buy", fill_price=101, reference_price=100)
        payload = f.to_dict()
        payload["slippage_bps"] = "-9999"
        assert Fill.from_dict(payload).slippage_bps == D("100.000000")

    def test_dict_carries_derived_values_for_readers(self):
        payload = mkfill(side="buy", fill_price=101, reference_price=100).to_dict()
        assert payload["slippage_bps"] == "100.000000"
        assert payload["slippage_amount"] == "10.0000"


# ===========================================================================
# Persistence
# ===========================================================================


class TestPersistence:
    def _order(self, db, portfolio):
        o = Order.market("INFY", "buy", 10, portfolio_id=portfolio.portfolio_id)
        o.submit()
        o.save_to_db(db)
        return o

    def test_requires_an_order_id(self, db):
        with pytest.raises(ValidationError, match="order_id is required"):
            mkfill().save_to_db(db)

    def test_save_and_read_back(self, db):
        p = Portfolio(name="p", initial_capital=100_000)
        p.save_to_db(db)
        o = self._order(db, p)
        f = Fill.from_order(
            o,
            fill_price=1501,
            reference_price=1500,
            commission_model=FlatCommission(per_trade=20),
            liquidity_flag="taker",
        )
        f.save_to_db(db)

        row = db.fetch_one("SELECT * FROM fills")
        assert D(str(row["quantity"])) == D("10.00000000")
        assert D(str(row["commission"])) == D("20.0000")
        assert D(str(row["slippage_bps"])) == D("6.666667")
        assert row["liquidity_flag"] == "taker"

    def test_resaving_is_a_safe_noop(self, db):
        """Fills are append-only; a retry must not duplicate or mutate."""
        p = Portfolio(name="p", initial_capital=100_000)
        p.save_to_db(db)
        o = self._order(db, p)
        f = Fill.from_order(o, fill_price=1500)
        f.save_to_db(db)
        f.save_to_db(db)
        assert db.fetch_scalar("SELECT count(*) FROM fills") == 1

    def test_several_partial_fills_persist(self, db):
        p = Portfolio(name="p", initial_capital=100_000)
        p.save_to_db(db)
        o = self._order(db, p)
        for _ in range(5):
            Fill.from_order(o, quantity=2, fill_price=1500).save_to_db(db)
        assert db.fetch_scalar("SELECT count(*) FROM fills") == 5
        assert db.fetch_scalar("SELECT sum(quantity) FROM fills") == 10

    def test_position_link_persists(self, db):
        p = Portfolio(name="p", initial_capital=100_000)
        o = Order.market("INFY", "buy", 10, portfolio_id=p.portfolio_id)
        o.submit()
        f = Fill.from_order(o, fill_price=1500)
        p.apply_fill(f)
        p.save_to_db(db)
        o.save_to_db(db)
        f.save_to_db(db)
        row = db.fetch_one("SELECT position_id FROM fills")
        assert row["position_id"] == p.get_position("INFY").position_id


# ===========================================================================
# FK-ordered graph persistence
#
# Regression: Fill.save_to_db used to emit a bare ForeignKeyViolation when the
# position it referenced had not been written yet. Caught by an end-to-end run
# against real PostgreSQL; SQLite tests missed it because they saved in a
# convenient order by accident.
# ===========================================================================


class TestGraphPersistence:
    def _session(self, p):
        """A small trading session: two orders, three fills, one close."""
        o1 = p.add_order(Order.market("INFY", "buy", 10, portfolio_id=p.portfolio_id))
        o1.submit()
        p.apply_fill(Fill.from_order(o1, quantity=4, fill_price=1500))
        p.apply_fill(Fill.from_order(o1, quantity=6, fill_price=1510))
        o2 = p.add_order(Order.market("INFY", "sell", 10, portfolio_id=p.portfolio_id))
        o2.submit()
        p.apply_fill(Fill.from_order(o2, fill_price=1550))
        p.sync_orders()
        return o1, o2

    def test_one_call_saves_positions_orders_and_fills(self, db):
        p = Portfolio(name="p", initial_capital=100_000)
        self._session(p)
        p.save_to_db(db)
        assert db.fetch_scalar("SELECT count(*) FROM positions") == 1
        assert db.fetch_scalar("SELECT count(*) FROM orders") == 2
        assert db.fetch_scalar("SELECT count(*) FROM fills") == 3

    def test_fill_foreign_keys_all_resolve(self, db):
        p = Portfolio(name="p", initial_capital=100_000)
        self._session(p)
        p.save_to_db(db)
        joined = db.fetch_scalar(
            "SELECT count(*) FROM fills f "
            "JOIN orders o ON o.order_id = f.order_id "
            "JOIN positions ps ON ps.position_id = f.position_id"
        )
        assert joined == 3

    def test_graph_save_is_idempotent(self, db):
        p = Portfolio(name="p", initial_capital=100_000)
        self._session(p)
        p.save_to_db(db)
        p.save_to_db(db)
        assert db.fetch_scalar("SELECT count(*) FROM fills") == 3
        assert db.fetch_scalar("SELECT count(*) FROM orders") == 2

    def test_include_orders_false_writes_only_positions(self, db):
        p = Portfolio(name="p", initial_capital=100_000)
        self._session(p)
        p.save_to_db(db, include_orders=False)
        assert db.fetch_scalar("SELECT count(*) FROM orders") == 0
        assert db.fetch_scalar("SELECT count(*) FROM fills") == 0
        assert db.fetch_scalar("SELECT count(*) FROM positions") == 1

    def test_graph_save_is_atomic(self, db, monkeypatch):
        """A failure part-way through must leave nothing behind."""
        p = Portfolio(name="p", initial_capital=100_000)
        self._session(p)

        def explode(self, session, FillRow, fill):
            raise RuntimeError("disk on fire")

        monkeypatch.setattr(Portfolio, "_insert_fill", explode)
        with pytest.raises(RuntimeError, match="disk on fire"):
            p.save_to_db(db)
        assert db.fetch_scalar("SELECT count(*) FROM portfolios") == 0
        assert db.fetch_scalar("SELECT count(*) FROM orders") == 0

    def test_standalone_fill_save_explains_the_ordering(self, db):
        """Actionable message instead of a raw ForeignKeyViolation."""
        p = Portfolio(name="p", initial_capital=100_000)
        p.save_to_db(db)
        o = Order.market("INFY", "buy", 10, portfolio_id=p.portfolio_id)
        o.submit()
        o.save_to_db(db)
        f = Fill.from_order(o, fill_price=1500)
        p.apply_fill(f)  # links position_id, but the row is unsaved

        with pytest.raises(ValidationError, match="dependency order"):
            f.save_to_db(db)

    def test_standalone_fill_save_works_once_ordered(self, db):
        p = Portfolio(name="p", initial_capital=100_000)
        o = Order.market("INFY", "buy", 10, portfolio_id=p.portfolio_id)
        o.submit()
        f = Fill.from_order(o, fill_price=1500)
        p.apply_fill(f)
        p.save_to_db(db, include_orders=False)  # positions first
        o.save_to_db(db)  # then the order
        assert f.save_to_db(db) == f.fill_id  # now the fill fits
