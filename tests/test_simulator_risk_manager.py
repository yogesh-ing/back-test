"""Tests for Step 15: Risk Manager."""

from __future__ import annotations

from decimal import Decimal
from datetime import date

import pytest

from backtest.simulator.portfolio import Portfolio
from backtest.simulator.order import Order
from backtest.simulator.risk_manager import RiskManager, RiskConfig, RiskCheckResult, load_risk_config


def make_portfolio(capital=100000, name="risk_test"):
    return Portfolio(name=name, initial_capital=capital)


def make_order(symbol="INFY", quantity=100, side="buy", order_type="market", limit_price=None):
    return Order(symbol=symbol, side=side, quantity=quantity, order_type=order_type, limit_price=limit_price)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def test_risk_config_defaults():
    cfg = RiskConfig()
    assert cfg.max_drawdown_pct == Decimal("0.10")
    assert cfg.daily_loss_limit_pct == Decimal("0.02")


def test_risk_config_validation():
    cfg = RiskConfig(max_position_pct=0.2, max_drawdown_pct=0.1)
    assert cfg.max_position_pct == Decimal("0.2")

    with pytest.raises(Exception):
        RiskConfig(max_position_pct=0)

    with pytest.raises(Exception):
        RiskConfig(max_drawdown_pct=1.5)

    with pytest.raises(Exception):
        RiskConfig(max_open_positions=0)


def test_load_config():
    cfg = load_risk_config()
    assert cfg.max_drawdown_pct is not None

    for profile in ["conservative", "aggressive", "intraday", "permissive"]:
        cfg = load_risk_config(profile=profile)
        assert cfg is not None


# ---------------------------------------------------------------------------
# Order-level checks
# ---------------------------------------------------------------------------


def test_restricted_symbol():
    portfolio = make_portfolio()
    cfg = RiskConfig(restricted_symbols={"INFY"})
    risk = RiskManager(portfolio, cfg)

    order = make_order(symbol="INFY", quantity=10)
    result = risk.validate_order(order, current_price=100)
    assert not result.allowed
    assert result.code == "restricted_symbol"

    order2 = make_order(symbol="TCS", quantity=10)
    result2 = risk.validate_order(order2, current_price=100)
    assert result2.allowed


def test_allowed_symbols():
    portfolio = make_portfolio()
    cfg = RiskConfig(allowed_symbols={"INFY", "TCS"})
    risk = RiskManager(portfolio, cfg)

    order = make_order(symbol="INFY", quantity=10)
    assert risk.validate_order(order, current_price=100).allowed

    order2 = make_order(symbol="RELIANCE", quantity=10)
    assert not risk.validate_order(order2, current_price=100).allowed


def test_min_max_order_value():
    portfolio = make_portfolio()
    cfg = RiskConfig(min_order_value=1000, max_order_value=10000)
    risk = RiskManager(portfolio, cfg)

    # 5 *100 =500 <1000
    order = make_order(quantity=5)
    result = risk.validate_order(order, current_price=100)
    assert not result.allowed
    assert result.code == "below_min_order_value"

    # 200*100=20000 >10000
    order2 = make_order(quantity=200)
    result2 = risk.validate_order(order2, current_price=100)
    assert not result2.allowed
    assert result2.code == "above_max_order_value"

    # 50*100=5000 ok
    order3 = make_order(quantity=50)
    assert risk.validate_order(order3, current_price=100).allowed


def test_max_order_pct_of_daily_volume():
    portfolio = make_portfolio()
    cfg = RiskConfig(max_order_pct_of_daily_volume=0.1)
    risk = RiskManager(portfolio, cfg)

    # Order 100 shares, avg daily 1000 => 10% ok
    order = make_order(quantity=100)
    result = risk.validate_order(order, current_price=100, daily_volume=1000)
    assert result.allowed

    # Order 200 shares, avg daily 1000 => 20% >10% => reject
    order2 = make_order(quantity=200)
    result2 = risk.validate_order(order2, current_price=100, daily_volume=1000)
    assert not result2.allowed
    assert result2.code == "exceeds_daily_volume"


# ---------------------------------------------------------------------------
# Position-level checks
# ---------------------------------------------------------------------------


def test_max_position_value():
    portfolio = make_portfolio()
    cfg = RiskConfig(max_position_value=5000)
    risk = RiskManager(portfolio, cfg)

    # 100*100=10000 >5000
    result = risk.check_position_limits("INFY", 100, current_price=100)
    assert not result.allowed
    assert result.code == "max_position_value"

    result2 = risk.check_position_limits("INFY", 10, current_price=100)
    assert result2.allowed


def test_max_position_pct():
    portfolio = make_portfolio(100000)
    cfg = RiskConfig(max_position_pct=0.1)
    risk = RiskManager(portfolio, cfg)

    # 200*100=20000 =20% of 100k >10%
    result = risk.check_position_limits("INFY", 200, current_price=100)
    assert not result.allowed
    assert result.code == "max_position_pct"

    result2 = risk.check_position_limits("INFY", 50, current_price=100)
    assert result2.allowed


def test_max_open_positions():
    portfolio = make_portfolio()
    portfolio.open_position("TCS", 10, 100)
    portfolio.open_position("RELIANCE", 10, 100)

    cfg = RiskConfig(max_open_positions=2)
    risk = RiskManager(portfolio, cfg)

    # Already have 2, trying to open 3rd should fail
    result = risk.check_position_limits("INFY", 10, current_price=100)
    assert not result.allowed
    assert result.code == "max_open_positions"

    # Existing symbol should pass (not opening new)
    portfolio2 = make_portfolio()
    portfolio2.open_position("INFY", 10, 100)
    risk2 = RiskManager(portfolio2, RiskConfig(max_open_positions=1))
    # Trying to open same symbol? Actually has_position true, so max_open_positions check should not trigger for same symbol
    # Our implementation checks has_position, so same symbol should pass
    result3 = risk2.check_position_limits("INFY", 10, current_price=100)
    # It checks has_position, so if already has INFY, it won't count as new
    # But our test for same symbol: has_position true, so it should allow (not opening new)
    # However our implementation only checks max_open_positions when not has_position
    # So this should pass
    assert result3.allowed


def test_sector_exposure():
    portfolio = make_portfolio(100000)
    portfolio.open_position("HDFCBANK", 100, 100)  # 10k exposure in BANKING

    cfg = RiskConfig(
        sector_exposure_limits={"BANKING": Decimal("0.2")},  # 20% =20k max
        symbol_to_sector={"HDFCBANK": "BANKING", "ICICIBANK": "BANKING", "INFY": "IT"},
    )
    risk = RiskManager(portfolio, cfg)

    # Trying to add ICICIBANK 150 shares @100 =15k, total BANKING would be 25k >20k
    result = risk.check_position_limits("ICICIBANK", 150, current_price=100)
    assert not result.allowed
    assert result.code == "sector_exposure"

    # Adding IT should pass
    result2 = risk.check_position_limits("INFY", 50, current_price=100)
    assert result2.allowed


# ---------------------------------------------------------------------------
# Portfolio-level checks
# ---------------------------------------------------------------------------


def test_buying_power():
    portfolio = make_portfolio(10000)
    cfg = RiskConfig()
    risk = RiskManager(portfolio, cfg)

    # Required 5000, buying power 10000 (cash) ok
    result = risk.check_buying_power(5000)
    assert result.allowed

    # Required 20000 >10000
    result2 = risk.check_buying_power(20000)
    assert not result2.allowed
    assert "buying_power" in result2.code or "funds" in result2.code


def test_drawdown_limits():
    portfolio = make_portfolio(100000)
    # Simulate drawdown: equity 90k, peak 100k => 10% drawdown
    portfolio.current_cash = Decimal("90000")
    # Need to mock peak equity via equity_history
    from backtest.simulator.portfolio import EquityPoint
    from datetime import datetime, timezone

    portfolio.equity_history = [
        EquityPoint(ts=datetime.now(timezone.utc), total_equity=Decimal("100000"), cash=Decimal("100000"), position_value=Decimal("0"))
    ]

    cfg = RiskConfig(max_drawdown_pct=0.05)  # 5% max
    risk = RiskManager(portfolio, cfg)

    result = risk.check_drawdown_limits(portfolio)
    # Drawdown 10% >5% => fail
    assert not result.allowed
    assert result.code == "max_drawdown"

    cfg2 = RiskConfig(max_drawdown_pct=0.15)
    risk2 = RiskManager(portfolio, cfg2)
    assert risk2.check_drawdown_limits(portfolio).allowed


def test_daily_loss_limit():
    portfolio = make_portfolio(100000)
    cfg = RiskConfig(daily_loss_limit_pct=0.02)
    risk = RiskManager(portfolio, cfg)

    # Record daily loss 3% of equity
    today = date.today()
    risk._daily_pnl[today] = Decimal("-3000")  # -3k loss on 100k =3% >2%

    result = risk.check_daily_loss_limit(portfolio)
    assert not result.allowed
    assert result.code == "daily_loss_limit"

    # Smaller loss should pass
    risk2 = RiskManager(portfolio, cfg)
    risk2._daily_pnl[today] = Decimal("-1000")  # 1% <2%
    assert risk2.check_daily_loss_limit(portfolio).allowed


def test_leverage():
    portfolio = make_portfolio(100000)
    portfolio.open_position("INFY", 100, 100)  # 10k gross

    cfg = RiskConfig(max_leverage=1)
    risk = RiskManager(portfolio, cfg)

    # Gross 10k, equity ~100k (cash 90k + position 10k =100k), leverage 0.1 <1 ok
    assert risk.check_leverage(portfolio).allowed

    # Open large position to increase leverage
    portfolio2 = make_portfolio(100000)
    # Use limits to allow leverage, but risk manager checks leverage separately
    # Simulate gross 150k, equity 100k => 1.5x >1
    portfolio2.current_cash = Decimal("0")
    # Create positions with notional 150k but equity 100k? Let's mock
    # For simplicity, directly test leverage calc: gross 150k, equity 100k
    class MockPF:
        def calculate_total_equity(self):
            return Decimal("100000")
        def calculate_gross_exposure(self):
            return Decimal("150000")

    risk3 = RiskManager(MockPF(), RiskConfig(max_leverage=1))
    result = risk3.check_leverage()
    assert not result.allowed
    assert result.code == "max_leverage"


def test_max_gross_exposure():
    portfolio = make_portfolio(100000)
    portfolio.open_position("TCS", 400, 100)  # 40k

    cfg = RiskConfig(max_gross_exposure_pct=0.5)  # 50k max
    risk = RiskManager(portfolio, cfg)

    # New order 200*100=20k, total would be 60k >50k
    order = make_order(quantity=200)
    result = risk.validate_order(order, current_price=100)
    assert not result.allowed
    assert result.code == "max_gross_exposure"


# ---------------------------------------------------------------------------
# Circuit breakers
# ---------------------------------------------------------------------------


def test_emergency_stop():
    portfolio = make_portfolio()
    portfolio.open_position("INFY", 10, 100)
    order = make_order(quantity=10)
    portfolio.add_order(order)

    risk = RiskManager(portfolio, RiskConfig())
    cancelled = risk.emergency_stop_all("test emergency")

    assert risk.is_halted()
    assert risk._halt_reason == "test emergency"
    assert cancelled >= 1

    # New orders should be rejected when halted
    order2 = make_order(symbol="TCS", quantity=10)
    result = risk.validate_order(order2, current_price=100)
    assert not result.allowed
    assert result.code == "trading_halted"


def test_circuit_breaker_drawdown():
    portfolio = make_portfolio(100000)
    portfolio.current_cash = Decimal("80000")
    from backtest.simulator.portfolio import EquityPoint
    from datetime import datetime, timezone

    portfolio.equity_history = [
        EquityPoint(ts=datetime.now(timezone.utc), total_equity=Decimal("100000"), cash=Decimal("100000"), position_value=Decimal("0"))
    ]

    cfg = RiskConfig(max_drawdown_pct=0.1)  # 10%
    risk = RiskManager(portfolio, cfg)

    # Drawdown 20% >10% should trigger breaker
    breaker = risk.check_circuit_breakers()
    assert breaker is not None
    assert breaker.code == "max_drawdown"
    assert risk.is_halted()


def test_consecutive_losses_breaker():
    portfolio = make_portfolio()
    cfg = RiskConfig(max_consecutive_losses=3)
    risk = RiskManager(portfolio, cfg)

    # Record 3 losses
    for _ in range(3):
        risk.record_trade_result(pnl=-100, is_win=False)

    assert risk._consecutive_losses == 3

    breaker = risk.check_circuit_breakers()
    assert breaker is not None
    assert breaker.code == "consecutive_losses"
    assert risk.is_halted()

    # Win should reset
    risk2 = RiskManager(portfolio, cfg)
    risk2.record_trade_result(pnl=-100, is_win=False)
    risk2.record_trade_result(pnl=200, is_win=True)
    assert risk2._consecutive_losses == 0


def test_override():
    portfolio = make_portfolio()
    cfg = RiskConfig(allow_override=True, override_code="SECRET123", max_drawdown_pct=0.1)
    risk = RiskManager(portfolio, cfg)

    risk.emergency_stop_all("test")
    assert risk.is_halted()

    # Wrong code should fail
    assert risk.override("WRONG") is False
    assert risk.is_halted()

    # Correct code should succeed
    assert risk.override("SECRET123", duration_minutes=60) is True
    assert not risk.is_halted()

    # After override, orders should pass
    order = make_order(quantity=10)
    assert risk.validate_order(order, current_price=100).allowed


def test_alert_callbacks():
    portfolio = make_portfolio()
    cfg = RiskConfig(restricted_symbols={"INFY"})
    risk = RiskManager(portfolio, cfg)

    alerts = []
    risk.add_alert_callback(lambda level, msg, details: alerts.append((level, msg, details)))

    order = make_order(symbol="INFY", quantity=10)
    risk.validate_order(order, current_price=100)

    assert len(alerts) >= 1
    assert alerts[0][0] == "warning"
    assert "INFY" in alerts[0][1]


def test_validate_orders_batch():
    portfolio = make_portfolio()
    cfg = RiskConfig(restricted_symbols={"BAD"})
    risk = RiskManager(portfolio, cfg)

    orders = [
        make_order(symbol="INFY", quantity=10),
        make_order(symbol="BAD", quantity=10),
        make_order(symbol="TCS", quantity=10),
    ]

    approved = risk.validate_orders(orders)
    assert len(approved) == 2
    assert all(o.symbol != "BAD" for o in approved)


def test_record_trade_and_daily_pnl():
    portfolio = make_portfolio()
    risk = RiskManager(portfolio, RiskConfig())

    risk.record_trade_result(pnl=100, is_win=True)
    assert risk._consecutive_losses == 0

    risk.record_trade_result(pnl=-50, is_win=False)
    assert risk._consecutive_losses == 1

    today = date.today()
    assert risk._daily_pnl[today] == Decimal("50")  # 100-50


def test_error_tracking():
    portfolio = make_portfolio()
    cfg = RiskConfig(max_consecutive_errors=2, pause_on_technical_error=True)
    risk = RiskManager(portfolio, cfg)

    risk.record_error()
    assert not risk.is_halted()

    risk.record_error()
    # Should halt after 2 errors
    assert risk.is_halted()

    risk.reset_error_count()
    assert risk._consecutive_errors == 0
