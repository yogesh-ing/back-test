"""Tests for Step 14: Position Sizing Engine."""

from __future__ import annotations

from decimal import Decimal

import pandas as pd
import pytest

from backtest.simulator.money import money
from backtest.simulator.portfolio import Portfolio
from backtest.simulator.position_sizing import (
    FixedDollarSizer,
    FixedQuantitySizer,
    KellySizer,
    PercentagePortfolioSizer,
    PositionSizer,
    RiskBasedSizer,
    RiskParams,
    SizingConfig,
    SizingConstraints,
    SizingMethod,
    VolatilitySizer,
    load_position_sizing_config,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_portfolio(capital=100000, name="test"):
    return Portfolio(name=name, initial_capital=capital)


def make_signal(symbol="INFY", close=100, indicators=None):
    from backtest.forward.strategy_adapter import Signal

    ind = indicators or {"close": close}
    return Signal(symbol=symbol, action="BUY", indicators=ind)


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------


def test_sizing_method_validation():
    assert SizingMethod.validate("fixed_quantity") == "fixed_quantity"
    assert SizingMethod.validate("FIXED") == "fixed_quantity"
    assert SizingMethod.validate("risk") == "risk_based"
    with pytest.raises(Exception):
        SizingMethod.validate("invalid_method")


def test_sizing_config_validation():
    cfg = SizingConfig(method="fixed_quantity", fixed_quantity=100)
    assert cfg.method == "fixed_quantity"

    with pytest.raises(Exception):
        SizingConfig(method="fixed_quantity", fixed_quantity=0)

    with pytest.raises(Exception):
        SizingConfig(method="percentage_portfolio", percentage=1.5)

    with pytest.raises(Exception):
        SizingConfig(method="risk_based", risk_per_trade=0, stop_loss_pct=0.02)


def test_constraints_validation():
    c = SizingConstraints(max_position_pct=0.2, min_trade_value=1000, lot_size=1)
    assert c.max_position_pct == Decimal("0.2")

    with pytest.raises(Exception):
        SizingConstraints(max_position_pct=0)

    with pytest.raises(Exception):
        SizingConstraints(lot_size=0)


def test_risk_params_validation():
    rp = RiskParams(max_risk_per_trade=0.01, stop_loss_pct=0.02)
    assert rp.max_risk_per_trade == Decimal("0.01")

    with pytest.raises(Exception):
        RiskParams(max_risk_per_trade=-0.01)


# ---------------------------------------------------------------------------
# Individual sizers
# ---------------------------------------------------------------------------


def test_fixed_quantity():
    sizer = FixedQuantitySizer(quantity=123)
    qty = sizer.calculate_position_size()
    assert qty == Decimal("123")

    # apply_*
    sizer.apply_fixed_quantity(200)
    assert sizer.quantity == Decimal("200")


def test_fixed_dollar():
    sizer = FixedDollarSizer(dollar_amount=10000)
    qty = sizer.calculate_position_size(current_price=100)
    assert qty == Decimal("100")

    sizer.apply_fixed_dollar_amount(20000)
    assert sizer.dollar_amount == Decimal("20000.0000")


def test_percentage_portfolio():
    sizer = PercentagePortfolioSizer(percentage=0.1)
    portfolio = make_portfolio(100000)
    qty = sizer.calculate_position_size(portfolio=portfolio, current_price=100)
    assert qty == Decimal("100")  # 10% of 100k =10k /100 =100

    sizer.apply_percentage_of_portfolio(0.2)
    assert sizer.percentage == Decimal("0.2")


def test_risk_based():
    sizer = RiskBasedSizer(risk_per_trade=0.01, stop_loss_pct=0.02)
    portfolio = make_portfolio(100000)
    # risk 1% =1000, loss per share =100*0.02=2, qty=500
    qty = sizer.calculate_position_size(portfolio=portfolio, current_price=100)
    assert qty == Decimal("500")

    # custom overrides
    qty2 = sizer.calculate_position_size(
        portfolio=portfolio, current_price=100, risk_per_trade=0.02, stop_loss_pct=0.01
    )
    # risk 2% =2000, loss 1 => 2000 qty
    assert qty2 == Decimal("2000")


def test_volatility_based():
    sizer = VolatilitySizer(risk_amount=1000, atr_multiplier=2, atr=5)
    qty = sizer.calculate_position_size(atr=5)
    # 1000/(5*2)=100
    assert qty == Decimal("100")

    # with signal containing ATR
    signal = make_signal(indicators={"atr": 10, "close": 100})
    qty2 = sizer.calculate_position_size(signal=signal, risk_amount=1000, atr_multiplier=1)
    assert qty2 == Decimal("100")  # 1000/10=100

    # missing ATR should raise
    sizer_no_atr = VolatilitySizer(risk_amount=1000)
    with pytest.raises(Exception):
        sizer_no_atr.calculate_position_size(current_price=100)


def test_kelly():
    # win 60%, avg win 200, avg loss 100, b=2, f* =0.6 -0.4/2=0.4, half=0.2, equity 100k, price 100 => 200
    sizer = KellySizer(win_rate=0.6, avg_win=200, avg_loss=100, kelly_fraction=0.5)
    portfolio = make_portfolio(100000)
    qty = sizer.calculate_position_size(portfolio=portfolio, current_price=100)
    assert qty == Decimal("200")

    # losing strategy: win 40%, b=1 => f*=0.4-0.6= -0.2 => 0 qty
    sizer_losing = KellySizer(win_rate=0.4, avg_win=100, avg_loss=100, kelly_fraction=0.5)
    qty_losing = sizer_losing.calculate_position_size(portfolio=portfolio, current_price=100)
    assert qty_losing == Decimal("0")

    # apply_kelly
    raw = sizer.apply_kelly_criterion(win_rate=0.55, avg_win=150, avg_loss=100, fraction=0.5)
    # b=1.5, f*=0.55-0.45/1.5=0.25
    assert abs(float(raw) - 0.25) < 0.001


# ---------------------------------------------------------------------------
# Composite sizer with constraints
# ---------------------------------------------------------------------------


def test_position_sizer_fixed():
    cfg = SizingConfig(method="fixed_quantity", fixed_quantity=100)
    sizer = PositionSizer(cfg)
    portfolio = make_portfolio()
    qty = sizer.calculate_position_size(symbol="INFY", current_price=100, portfolio=portfolio)
    assert qty == Decimal("100")


def test_position_sizer_percentage_with_constraints():
    cfg = SizingConfig(
        method="percentage_portfolio",
        percentage=0.5,  # 50% of 100k =50k
        constraints=SizingConstraints(max_position_pct=0.2),  # cap 20% =20k
    )
    sizer = PositionSizer(cfg)
    portfolio = make_portfolio(100000)
    qty = sizer.calculate_position_size(symbol="INFY", current_price=100, portfolio=portfolio)
    # capped to 20k => 200 shares
    assert qty == Decimal("200")


def test_max_position_value_constraint():
    cfg = SizingConfig(
        method="fixed_quantity",
        fixed_quantity=1000,
        constraints=SizingConstraints(max_position_value=10000),
    )
    sizer = PositionSizer(cfg)
    portfolio = make_portfolio()
    qty = sizer.calculate_position_size(symbol="INFY", current_price=100, portfolio=portfolio)
    # 1000*100=100k >10k max => capped to 100
    assert qty == Decimal("100")


def test_min_trade_value_constraint():
    cfg = SizingConfig(
        method="fixed_quantity",
        fixed_quantity=1,
        constraints=SizingConstraints(min_trade_value=5000),
    )
    sizer = PositionSizer(cfg)
    portfolio = make_portfolio()
    qty = sizer.calculate_position_size(symbol="INFY", current_price=100, portfolio=portfolio)
    # 1*100=100 <5000 => 0
    assert qty == Decimal("0")


def test_round_lots():
    cfg = SizingConfig(
        method="fixed_quantity",
        fixed_quantity=123,
        constraints=SizingConstraints(round_lots=True, lot_size=50),
    )
    sizer = PositionSizer(cfg)
    portfolio = make_portfolio()
    qty = sizer.calculate_position_size(symbol="INFY", current_price=100, portfolio=portfolio)
    # floor 123 to nearest 50 => 100
    assert qty == Decimal("100")


def test_max_gross_exposure():
    cfg = SizingConfig(
        method="fixed_quantity",
        fixed_quantity=1000,
        constraints=SizingConstraints(max_gross_exposure_pct=0.5),
    )
    sizer = PositionSizer(cfg)
    portfolio = make_portfolio(100000)
    # open existing position 40k exposure
    portfolio.open_position("TCS", 400, 100)
    # max gross 50% =50k, remaining 10k, price 100 => max 100 shares
    qty = sizer.calculate_position_size(symbol="INFY", current_price=100, portfolio=portfolio)
    assert qty == Decimal("100")


def test_max_open_positions():
    cfg = SizingConfig(
        method="fixed_quantity",
        fixed_quantity=10,
        constraints=SizingConstraints(max_open_positions=1),
    )
    sizer = PositionSizer(cfg)
    portfolio = make_portfolio()
    portfolio.open_position("TCS", 10, 100)
    qty = sizer.calculate_position_size(symbol="INFY", current_price=100, portfolio=portfolio)
    assert qty == Decimal("0")


def test_sizing_result_details():
    cfg = SizingConfig(
        method="fixed_quantity",
        fixed_quantity=1000,
        constraints=SizingConstraints(max_position_pct=0.1),
    )
    sizer = PositionSizer(cfg)
    portfolio = make_portfolio(100000)
    result = sizer.calculate_with_details(symbol="INFY", current_price=100, portfolio=portfolio)
    assert result.raw_quantity == Decimal("1000")
    assert result.quantity == Decimal("100")
    assert result.constrained is True
    assert result.notional == Decimal("10000.0000")


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------


def test_load_config_default():
    cfg = load_position_sizing_config()
    assert cfg.method in SizingMethod.ALL


def test_load_config_profiles():
    for profile in ["fixed", "percentage", "conservative", "volatility", "kelly"]:
        cfg = load_position_sizing_config(profile=profile)
        assert cfg.method in SizingMethod.ALL


def test_load_config_unknown_profile():
    with pytest.raises(Exception):
        load_position_sizing_config(profile="nonexistent_profile_xyz")


# ---------------------------------------------------------------------------
# Spec-required apply_* methods
# ---------------------------------------------------------------------------


def test_apply_methods():
    sizer = PositionSizer()

    q = sizer.apply_fixed_quantity(150)
    assert q == Decimal("150")
    assert sizer.config.method == "fixed_quantity"

    amt = sizer.apply_fixed_dollar_amount(20000)
    assert amt == Decimal("20000.0000")
    assert sizer.config.method == "fixed_dollar"

    pct = sizer.apply_percentage_of_portfolio(0.1)
    assert pct == Decimal("0.1")
    assert sizer.config.method == "percentage_portfolio"

    rp = sizer.apply_risk_percentage(0.02, 0.03)
    assert rp == Decimal("0.02")
    assert sizer.config.method == "risk_based"

    atr = sizer.apply_volatility_based(atr=5, risk_amount=1000, atr_multiplier=2)
    assert atr == Decimal("5")
    assert sizer.config.method == "atr_based"

    kelly_raw = sizer.apply_kelly_criterion(win_rate=0.6, avg_win=200, avg_loss=100, fraction=0.5)
    assert kelly_raw > Decimal("0")
    assert sizer.config.method == "kelly"


# ---------------------------------------------------------------------------
# Integration with StrategyAdapter
# ---------------------------------------------------------------------------


def test_integration_with_adapter():
    from backtest.forward.strategy_adapter import StrategyAdapter
    from backtest.strategy.base import Strategy

    class DummyStrat(Strategy):
        name = ""
        params = {}

        def __init__(self):
            super().__init__()
            self.name = "dummy"

        def generate_signals(self, candles):
            return pd.Series(1, index=candles.index)

    portfolio = make_portfolio(100000, name="integ_adapter_test")
    cfg = SizingConfig(method="risk_based", risk_per_trade=0.01, stop_loss_pct=0.02)
    sizer = PositionSizer(cfg)
    adapter = StrategyAdapter(
        strategy=DummyStrat(),
        portfolio=portfolio,
        position_sizer=sizer,
        symbols=["INFY"],
        min_bars=1,
    )

    bar = {
        "symbol": "INFY",
        "timestamp": "2024-01-01T09:15:00+05:30",
        "open": 100,
        "high": 101,
        "low": 99,
        "close": 100,
        "volume": 1000,
    }
    sigs = adapter.on_bar_close(bar)
    adapter.create_orders(sigs, market_data=bar)

    assert len(adapter.order_history) == 1
    # risk 1% of 100k =1000, stop 2% at 100 => loss per share 2, qty 500
    assert adapter.order_history[0].quantity == Decimal("500")


def test_risk_params_override():
    cfg = SizingConfig(method="risk_based", risk_per_trade=0.01, stop_loss_pct=0.02)
    sizer = PositionSizer(cfg)
    portfolio = make_portfolio(100000)

    # override risk to 2%
    qty = sizer.calculate_position_size(
        symbol="INFY", current_price=100, portfolio=portfolio, risk_per_trade=0.02
    )
    # risk 2% =2000, loss 2 => 1000
    assert qty == Decimal("1000")

    # same override via a risk_params dict — exercises the dict-merge path
    # (fields(RiskParams) walk in PositionSizer.calculate_position_size)
    qty_dict = sizer.calculate_position_size(
        symbol="INFY",
        current_price=100,
        portfolio=portfolio,
        risk_params={"max_risk_per_trade": 0.02},
    )
    assert qty_dict == Decimal("1000")


def test_kelly_with_signal():
    cfg = SizingConfig(method="kelly", win_rate=0.6, avg_win=200, avg_loss=100, kelly_fraction=0.5)
    sizer = PositionSizer(cfg)
    portfolio = make_portfolio(100000)
    signal = make_signal(close=100)

    qty = sizer.calculate_position_size(signal=signal, portfolio=portfolio)
    assert qty == Decimal("200")
