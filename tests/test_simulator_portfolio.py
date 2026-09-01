"""Tests for the Portfolio and Position domain models (Step 3).

Pure in-memory except for the persistence section, which uses an in-memory
SQLite database via ``DatabaseManager``.

The accounting tests are the important ones: if cash, equity and P&L do not
reconcile exactly, every downstream metric is wrong and the error compounds
silently over thousands of fills.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from backtest.db.manager import DatabaseManager
from backtest.db.models import Base
from backtest.simulator import (
    DuplicatePositionError,
    EquityPoint,
    InsufficientFundsError,
    LimitExceededError,
    Portfolio,
    PortfolioLimits,
    PortfolioStateError,
    PortfolioStatus,
    Position,
    PositionNotFoundError,
    ShortSellingNotAllowedError,
    ValidationError,
)
from backtest.simulator.money import money, to_decimal

D = Decimal
UTC_NOW = datetime.now(timezone.utc)


@pytest.fixture()
def portfolio():
    return Portfolio(name="Test", initial_capital=100_000)


@pytest.fixture()
def shorting():
    return Portfolio(
        name="Shorting",
        initial_capital=100_000,
        limits=PortfolioLimits(allow_short=True, max_gross_exposure_pct=D("2")),
    )


@pytest.fixture()
def db():
    manager = DatabaseManager.from_env(profile="testing", url="sqlite:///:memory:")
    manager.connect()
    Base.metadata.create_all(manager.engine)
    yield manager
    manager.disconnect()


# ===========================================================================
# Money helpers
# ===========================================================================


class TestMoney:
    def test_float_avoids_binary_expansion(self):
        """0.1 must become Decimal('0.1'), not the exact binary value."""
        assert to_decimal(0.1) == D("0.1")
        assert str(to_decimal(0.1)) == "0.1"

    def test_accepts_str_int_decimal(self):
        assert to_decimal("1500.50") == D("1500.50")
        assert to_decimal(42) == D("42")
        assert to_decimal(D("1.5")) == D("1.5")

    def test_bool_is_rejected(self):
        """bool is an int subclass; accepting it silently hides bugs."""
        with pytest.raises(ValueError, match="bool"):
            to_decimal(True)

    @pytest.mark.parametrize("bad", [None, "abc", object(), float("nan"), float("inf")])
    def test_rejects_non_numeric(self, bad):
        with pytest.raises(ValueError):
            to_decimal(bad)

    def test_money_quantises_to_four_places(self):
        assert money("1.005551") == D("1.0056")

    def test_error_message_names_the_field(self):
        with pytest.raises(ValueError, match="initial_capital"):
            to_decimal("oops", "initial_capital")


# ===========================================================================
# Position
# ===========================================================================


class TestPositionBasics:
    def test_long_construction(self):
        p = Position(symbol="infy", quantity=10, average_entry_price=1500)
        assert p.symbol == "INFY"  # normalised
        assert p.is_long and p.is_open and not p.is_short
        assert p.position_type == "long"
        assert p.status == "open"

    def test_short_has_negative_quantity(self):
        p = Position(symbol="TCS", quantity=-5, average_entry_price=100)
        assert p.is_short and p.position_type == "short"

    @pytest.mark.parametrize(
        "kwargs, match",
        [
            (dict(quantity=0), "zero quantity"),
            (dict(average_entry_price=0), "must be positive"),
            (dict(average_entry_price=-1), "must be positive"),
            (dict(symbol="  "), "symbol"),
        ],
    )
    def test_invalid_construction(self, kwargs, match):
        base = dict(symbol="INFY", quantity=10, average_entry_price=100)
        with pytest.raises(ValidationError, match=match):
            Position(**{**base, **kwargs})

    def test_market_value_signed_by_direction(self):
        long = Position(symbol="A", quantity=10, average_entry_price=100, current_price=110)
        short = Position(symbol="B", quantity=-10, average_entry_price=100, current_price=110)
        assert long.market_value == D("1100.0000")
        assert short.market_value == D("-1100.0000")
        assert short.notional == D("1100.0000")  # always positive

    def test_effective_price_falls_back_to_entry(self):
        p = Position(symbol="A", quantity=1, average_entry_price=100)
        assert p.effective_price == D("100.00000000")
        assert p.unrealized_pnl == D("0.0000")

    def test_long_unrealized_pnl(self):
        p = Position(symbol="A", quantity=10, average_entry_price=100)
        p.update_price(110)
        assert p.unrealized_pnl == D("100.0000")
        assert p.unrealized_pnl_percentage == D("0.100000")

    def test_short_profits_when_price_falls(self):
        p = Position(symbol="A", quantity=-10, average_entry_price=100)
        p.update_price(90)
        assert p.unrealized_pnl == D("100.0000")

    def test_short_loses_when_price_rises(self):
        p = Position(symbol="A", quantity=-10, average_entry_price=100)
        p.update_price(110)
        assert p.unrealized_pnl == D("-100.0000")

    def test_get_pnl_at_price_does_not_mutate(self):
        p = Position(symbol="A", quantity=10, average_entry_price=100, current_price=100)
        assert p.get_pnl_at_price(120) == D("200.0000")
        assert p.current_price == D("100.00000000")
        assert p.unrealized_pnl == D("0.0000")

    def test_net_pnl_subtracts_commission(self):
        p = Position(symbol="A", quantity=10, average_entry_price=100, commission_total=D("15"))
        p.update_price(110)
        assert p.total_pnl == D("100.0000")
        assert p.net_pnl == D("85.0000")
        assert p.is_profitable()

    def test_update_price_rejects_non_positive(self):
        p = Position(symbol="A", quantity=1, average_entry_price=100)
        with pytest.raises(ValidationError, match="must be positive"):
            p.update_price(0)


class TestPositionAddShares:
    def test_averages_entry_price(self):
        p = Position(symbol="A", quantity=10, average_entry_price=100)
        p.add_shares(10, 120)
        assert p.quantity == D("20.00000000")
        assert p.average_entry_price == D("110.00000000")

    def test_returns_negative_cash_for_long(self):
        p = Position(symbol="A", quantity=10, average_entry_price=100)
        assert p.add_shares(5, 100, commission=D("2")) == D("-502.0000")

    def test_returns_positive_cash_for_short(self):
        p = Position(symbol="A", quantity=-10, average_entry_price=100)
        cash = p.add_shares(5, 100, commission=D("2"))
        assert cash == D("498.0000")
        assert p.quantity == D("-15.00000000")  # grew more negative

    def test_sign_of_argument_is_ignored(self):
        """Direction comes from the position, not the caller's sign."""
        p = Position(symbol="A", quantity=10, average_entry_price=100)
        p.add_shares(-5, 100)
        assert p.quantity == D("15.00000000")

    def test_cannot_add_to_closed(self):
        p = Position(symbol="A", quantity=10, average_entry_price=100)
        p.close(100)
        with pytest.raises(ValidationError, match="closed position"):
            p.add_shares(1, 100)

    def test_rejects_negative_commission(self):
        p = Position(symbol="A", quantity=10, average_entry_price=100)
        with pytest.raises(ValidationError, match="commission"):
            p.add_shares(1, 100, commission=-1)


class TestPositionReduce:
    def test_partial_close_realises_proportional_pnl(self):
        p = Position(symbol="A", quantity=10, average_entry_price=100)
        result = p.reduce_shares(4, 110)
        assert result.realized_pnl == D("40.0000")
        assert result.quantity_closed == D("4.00000000")
        assert not result.fully_closed
        assert p.quantity == D("6.00000000")

    def test_partial_close_keeps_average_entry_price(self):
        """Remaining shares keep their original cost basis."""
        p = Position(symbol="A", quantity=10, average_entry_price=100)
        p.reduce_shares(4, 110)
        assert p.average_entry_price == D("100.00000000")

    def test_full_close_marks_closed(self):
        p = Position(symbol="A", quantity=10, average_entry_price=100)
        result = p.close(110)
        assert result.fully_closed
        assert p.quantity == D("0")
        assert not p.is_open
        assert p.status == "closed"
        assert p.closed_at is not None

    def test_short_close_realises_correctly(self):
        p = Position(symbol="A", quantity=-10, average_entry_price=100)
        result = p.close(90)
        assert result.realized_pnl == D("100.0000")
        assert result.cash_delta == D("-900.0000")  # pays to buy back

    def test_long_close_cash_delta(self):
        p = Position(symbol="A", quantity=10, average_entry_price=100)
        assert p.close(110, commission=D("3")).cash_delta == D("1097.0000")

    def test_over_reduce_is_rejected(self):
        """Refuse rather than silently flipping direction."""
        p = Position(symbol="A", quantity=10, average_entry_price=100)
        with pytest.raises(ValidationError, match="more than the open quantity"):
            p.reduce_shares(11, 100)

    def test_closed_position_has_no_unrealized_pnl(self):
        p = Position(symbol="A", quantity=10, average_entry_price=100)
        p.close(110)
        assert p.unrealized_pnl == D("0")
        assert p.realized_pnl == D("100.0000")

    def test_successive_partial_closes_accumulate(self):
        p = Position(symbol="A", quantity=10, average_entry_price=100)
        p.reduce_shares(3, 110)
        p.reduce_shares(3, 120)
        assert p.realized_pnl == D("90.0000")  # 3*10 + 3*20
        assert p.quantity == D("4.00000000")


class TestPositionSerialisation:
    def test_round_trip(self):
        p = Position(symbol="A", quantity=10, average_entry_price=100, current_price=110)
        p.reduce_shares(3, 115)
        restored = Position.from_dict(p.to_dict())
        assert restored.symbol == p.symbol
        assert restored.quantity == p.quantity
        assert restored.average_entry_price == p.average_entry_price
        assert restored.realized_pnl == p.realized_pnl
        assert restored.position_id == p.position_id

    def test_round_trip_of_closed_position(self):
        """from_dict must handle zero quantity, which the ctor rejects."""
        p = Position(symbol="A", quantity=10, average_entry_price=100)
        p.close(110)
        restored = Position.from_dict(p.to_dict())
        assert restored.quantity == D("0")
        assert not restored.is_open
        assert restored.closed_at is not None

    def test_dict_is_json_safe(self):
        p = Position(symbol="A", quantity=10, average_entry_price=100)
        assert json.loads(json.dumps(p.to_dict()))["symbol"] == "A"


# ===========================================================================
# Portfolio — construction and valuation
# ===========================================================================


class TestPortfolioBasics:
    def test_defaults(self, portfolio):
        assert portfolio.current_cash == D("100000.0000")
        assert portfolio.status == PortfolioStatus.ACTIVE
        assert portfolio.calculate_total_equity() == D("100000.0000")
        assert len(portfolio) == 0

    def test_explicit_cash_for_restored_state(self):
        p = Portfolio(name="x", initial_capital=100_000, current_cash=42_000)
        assert p.current_cash == D("42000.0000")

    @pytest.mark.parametrize(
        "kwargs, match",
        [
            (dict(initial_capital=0), "must be positive"),
            (dict(initial_capital=-5), "must be positive"),
            (dict(name="   "), "name"),
            (dict(status="bogus"), "unknown status"),
        ],
    )
    def test_invalid_construction(self, kwargs, match):
        base = dict(name="x", initial_capital=1000)
        with pytest.raises(ValidationError, match=match):
            Portfolio(**{**base, **kwargs})

    def test_ids_are_unique(self):
        a = Portfolio(name="a", initial_capital=1)
        b = Portfolio(name="b", initial_capital=1)
        assert a.portfolio_id != b.portfolio_id

    def test_container_protocol(self, portfolio):
        portfolio.open_position("INFY", 10, 100)
        assert "INFY" in portfolio
        assert "TCS" not in portfolio
        assert len(portfolio) == 1
        assert [p.symbol for p in portfolio] == ["INFY"]


class TestPortfolioValuation:
    def test_equity_unchanged_at_long_entry(self, portfolio):
        """Buying converts cash into position value; net worth is flat."""
        portfolio.open_position("INFY", 10, 1500)
        assert portfolio.current_cash == D("85000.0000")
        assert portfolio.calculate_position_value() == D("15000.0000")
        assert portfolio.calculate_total_equity() == D("100000.0000")

    def test_equity_unchanged_at_short_entry(self, shorting):
        """Shorting credits cash and creates negative position value."""
        shorting.open_position("TCS", -10, 100)
        assert shorting.current_cash == D("101000.0000")
        assert shorting.calculate_position_value() == D("-1000.0000")
        assert shorting.calculate_total_equity() == D("100000.0000")

    def test_commission_reduces_equity_immediately(self, portfolio):
        portfolio.open_position("INFY", 10, 1500, commission=25)
        assert portfolio.calculate_total_equity() == D("99975.0000")
        assert portfolio.total_commission == D("25.0000")

    def test_gross_vs_net_exposure(self, shorting):
        shorting.open_position("A", 10, 100)
        shorting.open_position("B", -10, 100)
        assert shorting.calculate_gross_exposure() == D("2000.0000")
        assert shorting.calculate_net_exposure() == D("0.0000")

    def test_cash_account_buying_power_is_cash(self, portfolio):
        portfolio.open_position("INFY", 10, 1500)
        assert portfolio.calculate_buying_power() == portfolio.current_cash

    def test_leverage_expands_buying_power(self):
        p = Portfolio(
            name="lev",
            initial_capital=100_000,
            limits=PortfolioLimits(max_leverage=D("2"), max_gross_exposure_pct=D("2")),
        )
        assert p.calculate_buying_power() == D("200000.0000")
        p.open_position("A", 100, 1000)  # 100k notional
        assert p.calculate_buying_power() == D("100000.0000")

    def test_margin_used_under_leverage(self):
        p = Portfolio(
            name="lev",
            initial_capital=100_000,
            limits=PortfolioLimits(max_leverage=D("2"), max_gross_exposure_pct=D("2")),
        )
        p.open_position("A", 100, 1000)
        assert p.calculate_margin_used() == D("50000.0000")

    def test_buying_power_never_negative(self, portfolio):
        portfolio.current_cash = D("-500")
        assert portfolio.calculate_buying_power() == D("0")

    def test_exposure_report(self, shorting):
        shorting.open_position("A", 10, 100)
        shorting.open_position("B", -5, 100)
        report = shorting.get_current_exposure()
        assert report["long_exposure"] == D("1000.0000")
        assert report["short_exposure"] == D("500.0000")
        assert report["gross_exposure"] == D("1500.0000")
        assert report["open_positions"] == 2

    def test_total_return_tracks_pnl(self, portfolio):
        portfolio.open_position("INFY", 10, 1000)
        portfolio.update_position("INFY", 1100)
        assert portfolio.total_return == D("1000.0000")
        assert portfolio.total_return_pct == D("0.010000")


# ===========================================================================
# Portfolio — round-trip accounting
# ===========================================================================


class TestRoundTripAccounting:
    def test_long_round_trip(self, portfolio):
        portfolio.open_position("INFY", 10, 1500, commission=5)
        portfolio.close_position("INFY", 1512, commission=5)
        # (1512-1500)*10 = 120 gross, minus 10 commission
        assert portfolio.realized_pnl == D("120.0000")
        assert portfolio.total_commission == D("10.0000")
        assert portfolio.calculate_total_equity() == D("100110.0000")
        assert portfolio.current_cash == D("100110.0000")

    def test_short_round_trip(self, shorting):
        shorting.open_position("TCS", -10, 100)
        shorting.close_position("TCS", 90)
        assert shorting.realized_pnl == D("100.0000")
        assert shorting.calculate_total_equity() == D("100100.0000")

    def test_losing_trade(self, portfolio):
        portfolio.open_position("INFY", 10, 1500)
        portfolio.close_position("INFY", 1400)
        assert portfolio.realized_pnl == D("-1000.0000")
        assert portfolio.calculate_total_equity() == D("99000.0000")

    def test_equity_equals_cash_when_flat(self, portfolio):
        portfolio.open_position("A", 10, 100)
        portfolio.open_position("B", 5, 200)
        portfolio.close_all_positions({"A": 110, "B": 190})
        assert portfolio.calculate_position_value() == D("0.0000")
        assert portfolio.calculate_total_equity() == portfolio.current_cash

    def test_many_trades_reconcile_exactly(self, portfolio):
        """The whole point of Decimal: no drift over many operations."""
        for i in range(100):
            portfolio.open_position("X", 3, D("100.1"), commission=D("0.33"))
            portfolio.close_position("X", D("100.2"), commission=D("0.33"))
        # Each cycle: +0.1*3 = 0.30 gross, -0.66 commission => -0.36 net
        expected = D("100000") + (D("0.30") - D("0.66")) * 100
        assert portfolio.calculate_total_equity() == money(expected)
        assert portfolio.realized_pnl == D("30.0000")
        assert portfolio.total_commission == D("66.0000")

    def test_partial_close_then_full(self, portfolio):
        portfolio.open_position("INFY", 10, 100)
        portfolio.reduce_position("INFY", 4, 110)
        assert portfolio.realized_pnl == D("40.0000")
        assert portfolio.get_position("INFY").quantity == D("6.00000000")
        portfolio.close_position("INFY", 120)
        assert portfolio.realized_pnl == D("160.0000")  # 40 + 6*20
        assert not portfolio.has_position("INFY")

    def test_closed_positions_are_retained(self, portfolio):
        portfolio.open_position("INFY", 10, 100)
        portfolio.close_position("INFY", 110)
        assert len(portfolio.closed_positions) == 1
        assert portfolio.closed_positions[0].symbol == "INFY"


# ===========================================================================
# Portfolio — limit enforcement
# ===========================================================================


class TestCanOpenPosition:
    def test_allows_a_reasonable_trade(self, portfolio):
        check = portfolio.can_open_position("INFY", 10, 1500)
        assert check and check.code == "ok"

    def test_check_is_truthy_and_falsy(self, portfolio):
        assert bool(portfolio.can_open_position("INFY", 10, 1500)) is True
        assert bool(portfolio.can_open_position("INFY", 10, 1_000_000)) is False

    def test_insufficient_funds(self, portfolio):
        check = portfolio.can_open_position("INFY", 1000, 1500)
        assert not check
        assert check.code == "insufficient_funds"
        assert "1500000" in check.reason

    def test_short_disabled_by_default(self, portfolio):
        check = portfolio.can_open_position("INFY", -10, 100)
        assert check.code == "short_selling_not_allowed"

    def test_short_allowed_when_enabled(self, shorting):
        assert shorting.can_open_position("INFY", -10, 100)

    def test_duplicate_position_refused(self, portfolio):
        portfolio.open_position("INFY", 10, 100)
        check = portfolio.can_open_position("INFY", 5, 100)
        assert check.code == "duplicate_position"

    def test_max_open_positions(self):
        p = Portfolio(
            name="x",
            initial_capital=100_000,
            limits=PortfolioLimits(max_open_positions=2),
        )
        p.open_position("A", 1, 100)
        p.open_position("B", 1, 100)
        check = p.can_open_position("C", 1, 100)
        assert check.code == "max_open_positions"

    def test_max_position_value(self):
        p = Portfolio(
            name="x",
            initial_capital=100_000,
            limits=PortfolioLimits(max_position_value=D("5000")),
        )
        assert p.can_open_position("A", 40, 100)
        assert p.can_open_position("A", 60, 100).code == "max_position_value"

    def test_max_position_pct(self):
        p = Portfolio(
            name="x",
            initial_capital=100_000,
            limits=PortfolioLimits(max_position_pct=D("0.10")),
        )
        assert p.can_open_position("A", 100, 100)  # 10% exactly
        assert p.can_open_position("A", 101, 100).code == "max_position_pct"

    def test_max_gross_exposure(self):
        p = Portfolio(
            name="x",
            initial_capital=10_000,
            limits=PortfolioLimits(max_gross_exposure_pct=D("0.5")),
        )
        p.open_position("A", 40, 100)  # 4000 = 40%
        assert p.can_open_position("B", 20, 100).code == "max_gross_exposure"

    def test_min_trade_value(self):
        p = Portfolio(
            name="x",
            initial_capital=100_000,
            limits=PortfolioLimits(min_trade_value=D("1000")),
        )
        assert p.can_open_position("A", 1, 100).code == "below_min_trade_value"
        assert p.can_open_position("A", 20, 100)

    def test_paused_portfolio_refuses(self, portfolio):
        portfolio.pause()
        assert portfolio.can_open_position("A", 1, 100).code == "portfolio_not_active"

    @pytest.mark.parametrize(
        "qty, price, code",
        [
            (0, 100, "zero_quantity"),
            (10, 0, "invalid_price"),
            (10, -5, "invalid_price"),
            ("abc", 100, "invalid_input"),
        ],
    )
    def test_invalid_inputs(self, portfolio, qty, price, code):
        assert portfolio.can_open_position("A", qty, price).code == code

    def test_raise_if_denied_maps_to_exception_types(self, portfolio):
        with pytest.raises(InsufficientFundsError):
            portfolio.can_open_position("A", 10_000, 1000).raise_if_denied()
        with pytest.raises(ShortSellingNotAllowedError):
            portfolio.can_open_position("A", -1, 100).raise_if_denied()
        portfolio.open_position("B", 1, 100)
        with pytest.raises(DuplicatePositionError):
            portfolio.can_open_position("B", 1, 100).raise_if_denied()
        portfolio.pause()
        with pytest.raises(PortfolioStateError):
            portfolio.can_open_position("C", 1, 100).raise_if_denied()

    def test_raise_if_denied_is_silent_when_allowed(self, portfolio):
        portfolio.can_open_position("A", 1, 100).raise_if_denied()

    def test_limit_exceeded_is_the_fallback(self):
        p = Portfolio(
            name="x",
            initial_capital=100_000,
            limits=PortfolioLimits(max_open_positions=1),
        )
        p.open_position("A", 1, 100)
        with pytest.raises(LimitExceededError):
            p.can_open_position("B", 1, 100).raise_if_denied()

    def test_open_position_enforces_limits(self, portfolio):
        with pytest.raises(InsufficientFundsError):
            portfolio.open_position("A", 10_000, 1000)
        assert not portfolio.has_position("A")

    def test_validate_false_bypasses_checks(self, portfolio):
        """Restoring known-good state must not be blocked by limits."""
        portfolio.open_position("A", 10_000, 1000, validate=False)
        assert portfolio.has_position("A")


class TestPortfolioLimitsValidation:
    @pytest.mark.parametrize(
        "kwargs",
        [
            dict(max_position_value=0),
            dict(max_position_pct=-1),
            dict(max_leverage=D("0.5")),
            dict(max_open_positions=0),
            dict(min_trade_value=0),
        ],
    )
    def test_invalid_limits_rejected(self, kwargs):
        with pytest.raises(ValidationError):
            PortfolioLimits(**kwargs)

    def test_round_trip(self):
        limits = PortfolioLimits(
            allow_short=True,
            max_open_positions=5,
            max_position_pct=D("0.2"),
            max_leverage=D("2"),
        )
        restored = PortfolioLimits.from_dict(limits.to_dict())
        assert restored.allow_short is True
        assert restored.max_open_positions == 5
        assert restored.max_position_pct == D("0.2")
        assert restored.max_leverage == D("2")


# ===========================================================================
# Portfolio — position management
# ===========================================================================


class TestPositionManagement:
    def test_get_position_normalises_case(self, portfolio):
        portfolio.open_position("infy", 10, 100)
        assert portfolio.get_position("INFY") is not None
        assert portfolio.get_position("infy") is not None

    def test_get_position_missing_returns_none(self, portfolio):
        assert portfolio.get_position("NOPE") is None

    def test_require_position_raises(self, portfolio):
        with pytest.raises(PositionNotFoundError, match="NOPE"):
            portfolio.require_position("NOPE")

    def test_add_position_does_not_move_cash(self, portfolio):
        before = portfolio.current_cash
        portfolio.add_position(Position(symbol="A", quantity=10, average_entry_price=100))
        assert portfolio.current_cash == before
        assert portfolio.has_position("A")

    def test_add_position_sets_portfolio_id(self, portfolio):
        pos = Position(symbol="A", quantity=10, average_entry_price=100)
        portfolio.add_position(pos)
        assert pos.portfolio_id == portfolio.portfolio_id

    def test_add_duplicate_refused(self, portfolio):
        portfolio.add_position(Position(symbol="A", quantity=10, average_entry_price=100))
        with pytest.raises(DuplicatePositionError):
            portfolio.add_position(Position(symbol="A", quantity=5, average_entry_price=100))

    def test_add_closed_position_refused(self, portfolio):
        pos = Position(symbol="A", quantity=10, average_entry_price=100)
        pos.close(100)
        with pytest.raises(ValidationError, match="closed"):
            portfolio.add_position(pos)

    def test_update_prices_ignores_unknown_symbols(self, portfolio):
        portfolio.open_position("A", 10, 100)
        portfolio.update_prices({"A": 110, "UNKNOWN": 999})
        assert portfolio.get_position("A").current_price == D("110.00000000")

    def test_update_prices_skips_none(self, portfolio):
        portfolio.open_position("A", 10, 100)
        portfolio.update_prices({"A": None})
        assert portfolio.get_position("A").current_price == D("100.00000000")

    def test_close_missing_position_raises(self, portfolio):
        with pytest.raises(PositionNotFoundError):
            portfolio.close_position("NOPE", 100)

    def test_close_defaults_to_last_mark(self, portfolio):
        portfolio.open_position("A", 10, 100)
        portfolio.update_position("A", 130)
        portfolio.close_position("A")
        assert portfolio.realized_pnl == D("300.0000")

    def test_close_all(self, portfolio):
        portfolio.open_position("A", 10, 100)
        portfolio.open_position("B", 10, 200)
        closed = portfolio.close_all_positions({"A": 110, "B": 210})
        assert len(closed) == 2
        assert len(portfolio.positions) == 0
        assert portfolio.realized_pnl == D("200.0000")


# ===========================================================================
# Portfolio — lifecycle and equity history
# ===========================================================================


class TestLifecycle:
    def test_pause_and_resume(self, portfolio):
        portfolio.pause()
        assert portfolio.status == PortfolioStatus.PAUSED
        portfolio.resume()
        assert portfolio.status == PortfolioStatus.ACTIVE

    def test_stopped_is_terminal(self, portfolio):
        portfolio.stop()
        with pytest.raises(PortfolioStateError, match="cannot be resumed"):
            portfolio.resume()

    def test_paused_still_allows_closing(self, portfolio):
        """You must always be able to exit a position, even when paused."""
        portfolio.open_position("A", 10, 100)
        portfolio.pause()
        portfolio.close_position("A", 110)
        assert portfolio.realized_pnl == D("100.0000")


class TestEquityHistory:
    def test_record_equity_appends(self, portfolio):
        point = portfolio.record_equity()
        assert isinstance(point, EquityPoint)
        assert len(portfolio.equity_history) == 1
        assert point.total_equity == D("100000.0000")

    def test_peak_and_drawdown(self, portfolio):
        portfolio.open_position("A", 100, 100)
        portfolio.update_position("A", 120)
        portfolio.record_equity()  # equity 102,000
        assert portfolio.peak_equity() == D("102000.0000")
        portfolio.update_position("A", 110)  # equity 101,000
        assert portfolio.current_drawdown() == D("0.009804")

    def test_no_drawdown_at_peak(self, portfolio):
        portfolio.record_equity()
        assert portfolio.current_drawdown() == D("0")

    def test_peak_without_history(self, portfolio):
        assert portfolio.peak_equity() == D("100000.0000")


# ===========================================================================
# Serialisation
# ===========================================================================


class TestPortfolioSerialisation:
    def test_round_trip_preserves_everything(self, shorting):
        shorting.open_position("A", 10, 100, commission=2)
        shorting.open_position("B", -5, 200, commission=1)
        shorting.update_prices({"A": 110, "B": 190})
        shorting.record_equity()
        shorting.open_position("C", 1, 50)
        shorting.close_position("C", 60)

        restored = Portfolio.from_dict(shorting.to_dict())
        assert restored.portfolio_id == shorting.portfolio_id
        assert restored.name == shorting.name
        assert restored.current_cash == shorting.current_cash
        assert restored.realized_pnl == shorting.realized_pnl
        assert restored.total_commission == shorting.total_commission
        assert restored.calculate_total_equity() == shorting.calculate_total_equity()
        assert set(restored.positions) == set(shorting.positions)
        assert len(restored.closed_positions) == 1
        assert len(restored.equity_history) == 1

    def test_survives_json(self, portfolio):
        portfolio.open_position("A", 10, D("1500.12345678"))
        blob = json.dumps(portfolio.to_dict())
        restored = Portfolio.from_dict(json.loads(blob))
        assert restored.get_position("A").average_entry_price == D("1500.12345678")
        assert restored.calculate_total_equity() == portfolio.calculate_total_equity()

    def test_limits_survive_round_trip(self):
        p = Portfolio(
            name="x",
            initial_capital=1000,
            limits=PortfolioLimits(allow_short=True, max_open_positions=3),
        )
        restored = Portfolio.from_dict(p.to_dict())
        assert restored.limits.allow_short is True
        assert restored.limits.max_open_positions == 3

    def test_status_survives(self, portfolio):
        portfolio.pause()
        assert Portfolio.from_dict(portfolio.to_dict()).status == PortfolioStatus.PAUSED


# ===========================================================================
# Persistence
# ===========================================================================


class TestPersistence:
    def test_save_then_load(self, db, portfolio):
        portfolio.open_position("INFY", 10, 1500, commission=5)
        portfolio.update_position("INFY", 1520)
        portfolio.save_to_db(db)

        loaded = Portfolio.load_from_db(db, portfolio.portfolio_id)
        assert loaded.name == portfolio.name
        assert loaded.current_cash == portfolio.current_cash
        assert loaded.initial_capital == portfolio.initial_capital
        assert loaded.status == portfolio.status
        assert set(loaded.positions) == {"INFY"}
        position = loaded.get_position("INFY")
        assert position.quantity == D("10.00000000")
        assert position.average_entry_price == D("1500.00000000")

    def test_equity_survives_a_restart(self, db, portfolio):
        portfolio.open_position("INFY", 10, 1500)
        portfolio.update_position("INFY", 1600)
        before = portfolio.calculate_total_equity()
        portfolio.save_to_db(db)
        assert Portfolio.load_from_db(db, portfolio.portfolio_id).calculate_total_equity() == before

    def test_save_is_idempotent(self, db, portfolio):
        portfolio.open_position("INFY", 10, 1500)
        portfolio.save_to_db(db)
        portfolio.save_to_db(db)
        assert db.fetch_scalar("SELECT count(*) FROM portfolios") == 1
        assert db.fetch_scalar("SELECT count(*) FROM positions") == 1

    def test_save_reflects_updates(self, db, portfolio):
        portfolio.open_position("INFY", 10, 1500)
        portfolio.save_to_db(db)
        portfolio.update_position("INFY", 1600)
        portfolio.save_to_db(db)
        row = db.fetch_one("SELECT current_price FROM positions WHERE symbol='INFY'")
        assert D(str(row["current_price"])) == D("1600.00000000")

    def test_closed_positions_persist_as_closed(self, db, portfolio):
        portfolio.open_position("INFY", 10, 1500)
        portfolio.save_to_db(db)
        portfolio.close_position("INFY", 1600)
        portfolio.save_to_db(db)

        row = db.fetch_one("SELECT status, quantity FROM positions WHERE symbol='INFY'")
        assert row["status"] == "closed"
        # Reloading must not resurrect it as an open position.
        assert Portfolio.load_from_db(db, portfolio.portfolio_id).positions == {}

    def test_reopening_a_symbol_does_not_violate_the_unique_index(self, db, portfolio):
        """Closed history plus a new open position must coexist."""
        portfolio.open_position("INFY", 10, 1500)
        portfolio.close_position("INFY", 1600)
        portfolio.open_position("INFY", 5, 1610)
        portfolio.save_to_db(db)
        assert db.fetch_scalar("SELECT count(*) FROM positions") == 2
        assert db.fetch_scalar("SELECT count(*) FROM positions WHERE status='open'") == 1

    def test_close_and_reopen_across_two_saves(self, db, portfolio):
        """Regression: caught against real PostgreSQL, missed by an earlier test.

        Saving while open, then closing and reopening the same symbol, used to
        insert the new open row before flipping the old one to closed —
        momentarily leaving two open rows and violating
        uq_positions_one_open_per_symbol.
        """
        portfolio.open_position("INFY", 10, 1500)
        portfolio.save_to_db(db)  # old row persisted as OPEN

        portfolio.close_position("INFY", 1560)
        portfolio.open_position("INFY", 4, 1565)
        portfolio.save_to_db(db)  # must not violate the index

        assert db.fetch_scalar("SELECT count(*) FROM positions WHERE symbol='INFY'") == 2
        assert (
            db.fetch_scalar("SELECT count(*) FROM positions WHERE symbol='INFY' AND status='open'")
            == 1
        )
        reloaded = Portfolio.load_from_db(db, portfolio.portfolio_id)
        assert reloaded.get_position("INFY").quantity == D("4.00000000")

    def test_repeated_close_reopen_cycles_persist(self, db, portfolio):
        """Several save/close/reopen cycles must keep exactly one open row."""
        for i in range(5):
            portfolio.open_position("INFY", 1, 100 + i)
            portfolio.save_to_db(db)
            portfolio.close_position("INFY", 101 + i)
            portfolio.save_to_db(db)
        assert db.fetch_scalar("SELECT count(*) FROM positions WHERE symbol='INFY'") == 5
        assert (
            db.fetch_scalar("SELECT count(*) FROM positions WHERE symbol='INFY' AND status='open'")
            == 0
        )

    def test_load_unknown_id_raises(self, db):
        with pytest.raises(PositionNotFoundError, match="no portfolio"):
            Portfolio.load_from_db(db, "00000000-0000-0000-0000-000000000000")

    def test_save_is_atomic(self, db, portfolio, monkeypatch):
        """A failure mid-save must leave nothing behind."""
        portfolio.open_position("A", 1, 100)
        portfolio.open_position("B", 1, 100)

        calls = {"n": 0}
        original = Portfolio._upsert_position

        def explode(self, session, PositionRow, position):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("disk on fire")
            return original(self, session, PositionRow, position)

        monkeypatch.setattr(Portfolio, "_upsert_position", explode)
        with pytest.raises(RuntimeError, match="disk on fire"):
            portfolio.save_to_db(db)

        assert db.fetch_scalar("SELECT count(*) FROM portfolios") == 0
        assert db.fetch_scalar("SELECT count(*) FROM positions") == 0

    def test_short_position_persists(self, db, shorting):
        shorting.open_position("TCS", -10, 100)
        shorting.save_to_db(db)
        loaded = Portfolio.load_from_db(db, shorting.portfolio_id)
        position = loaded.get_position("TCS")
        assert position.is_short
        assert position.quantity == D("-10.00000000")


# ===========================================================================
# Misc
# ===========================================================================


def test_summary_contains_expected_keys(portfolio):
    portfolio.open_position("A", 10, 100)
    summary = portfolio.summary()
    for key in (
        "name",
        "status",
        "cash",
        "equity",
        "realized_pnl",
        "unrealized_pnl",
        "open_positions",
        "drawdown",
    ):
        assert key in summary


def test_repr_is_informative(portfolio):
    assert "Portfolio" in repr(portfolio) and "active" in repr(portfolio)


def test_simulator_does_not_import_engine_or_forward():
    """Layering rule: simulator/ stays free of the older subsystems.

    Parses the AST rather than grepping, so prose in a docstring that merely
    *mentions* ``backtest.engine`` does not trip the check.
    """
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1] / "src" / "backtest" / "simulator"
    forbidden = ("backtest.engine", "backtest.forward")
    offenders: list[str] = []

    for path in sorted(root.glob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                if any(name == f or name.startswith(f + ".") for f in forbidden):
                    offenders.append(f"{path.name}: {name}")

    assert not offenders, f"simulator/ must not import engine/ or forward/: {offenders}"
