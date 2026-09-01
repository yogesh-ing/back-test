"""Tests for Step 16: Stop Loss & Take Profit Manager."""

from __future__ import annotations

from decimal import Decimal

import pytest

from backtest.simulator.portfolio import Portfolio
from backtest.simulator.stop_manager import StopManager, StopType, TakeProfitType


def make_portfolio():
    from backtest.simulator.portfolio import PortfolioLimits

    return Portfolio(
        name="stop_test", initial_capital=100000, limits=PortfolioLimits(allow_short=True)
    )


def make_position(portfolio, symbol="INFY", qty=100, entry_price=100):
    return portfolio.open_position(symbol, qty, entry_price)


# ---------------------------------------------------------------------------
# Fixed price stops
# ---------------------------------------------------------------------------


def test_fixed_price_stop_long():
    portfolio = make_portfolio()
    pos = make_position(portfolio, qty=100, entry_price=100)
    manager = StopManager(portfolio)

    stop = manager.add_stop_loss(pos, stop_type="fixed_price", params={"price": 95})
    assert stop.price == Decimal("95")
    assert stop.side.value == "sell"

    # Price drops to 94 – should trigger
    hits = manager.check_stops({"INFY": {"close": 94, "low": 94, "high": 101}})
    assert len(hits) == 1
    assert hits[0].symbol == "INFY"
    assert hits[0].is_take_profit is False


def test_fixed_price_take_profit_long():
    portfolio = make_portfolio()
    pos = make_position(portfolio, qty=100, entry_price=100)
    manager = StopManager(portfolio)

    tp = manager.add_take_profit(pos, target_type="fixed_price", params={"price": 110})
    assert tp.price == Decimal("110")

    # Price rises to 111 – should trigger TP
    hits = manager.check_stops({"INFY": {"close": 111, "high": 111, "low": 99}})
    assert len(hits) == 1
    assert hits[0].is_take_profit is True


def test_fixed_price_stop_short():
    portfolio = make_portfolio()
    # Short position
    pos = make_position(portfolio, qty=-100, entry_price=100)
    manager = StopManager(portfolio)

    stop = manager.add_stop_loss(pos, stop_type="fixed_price", params={"price": 105})
    assert stop.side.value == "buy"  # short exit via buy

    # Price rises to 106 – should trigger short SL
    hits = manager.check_stops({"INFY": {"close": 106, "high": 106, "low": 99}})
    assert len(hits) == 1


# ---------------------------------------------------------------------------
# Percentage stops
# ---------------------------------------------------------------------------


def test_percentage_stop_long():
    portfolio = make_portfolio()
    pos = make_position(portfolio, qty=100, entry_price=100)
    manager = StopManager(portfolio)

    stop = manager.add_stop_loss(pos, stop_type="percentage", params={"pct": 0.02})
    # Long stop 2% below entry = 98
    assert stop.price == Decimal("98")

    hits = manager.check_stops({"INFY": {"close": 97, "low": 97}})
    assert len(hits) == 1


def test_percentage_take_profit_long():
    portfolio = make_portfolio()
    pos = make_position(portfolio, qty=100, entry_price=100)
    manager = StopManager(portfolio)

    tp = manager.add_take_profit(pos, target_type="percentage", params={"pct": 0.05})
    # Long TP 5% above = 105
    assert tp.price == Decimal("105")

    hits = manager.check_stops({"INFY": {"close": 106, "high": 106}})
    assert len(hits) == 1
    assert hits[0].is_take_profit


def test_percentage_stop_short():
    portfolio = make_portfolio()
    pos = make_position(portfolio, qty=-100, entry_price=100)
    manager = StopManager(portfolio)

    stop = manager.add_stop_loss(pos, stop_type="percentage", params={"pct": 0.02})
    # Short stop 2% above = 102
    assert stop.price == Decimal("102")


# ---------------------------------------------------------------------------
# ATR-based stops
# ---------------------------------------------------------------------------


def test_atr_based_stop():
    portfolio = make_portfolio()
    pos = make_position(portfolio, qty=100, entry_price=100)
    manager = StopManager(portfolio)

    stop = manager.add_stop_loss(pos, stop_type="atr_based", params={"atr": 2, "atr_multiplier": 2})
    # Long SL = 100 - 2*2 = 96
    assert stop.price == Decimal("96")

    tp = manager.add_take_profit(
        pos, target_type="atr_based", params={"atr": 2, "atr_multiplier": 3}
    )
    # Long TP via ATR-based? Actually ATR-based is StopType, but we use add_take_profit with atr_based
    # For take profit, if we use atr_based as stop_type, it will still calculate as entry +/- atr*mult
    # Our implementation for ATR-based doesn't distinguish is_take_profit for TP? It does: for long TP, entry + atr*mult
    # So TP = 100 + 2*3 =106
    # But we called add_take_profit with target_type atr_based, which will be validated as StopType.ATR_BASED
    # That path for is_take_profit True gives entry + atr*mult
    assert tp.price == Decimal("106")


# ---------------------------------------------------------------------------
# Trailing stops
# ---------------------------------------------------------------------------


def test_trailing_fixed_long():
    portfolio = make_portfolio()
    pos = make_position(portfolio, qty=100, entry_price=100)
    manager = StopManager(portfolio)

    stop = manager.add_stop_loss(pos, stop_type="trailing_fixed", params={"trailing_amount": 2})
    # Initial stop 100-2=98
    assert stop.price == Decimal("98")
    assert stop.is_trailing is True

    # Price goes up to 105 – trailing should move to 103
    updated = manager.update_trailing_stops({"INFY": 105})
    assert len(updated) == 1
    assert updated[0].price == Decimal("103")

    # Price goes down to 102 – trailing should NOT move down (ratchet one way)
    updated2 = manager.update_trailing_stops({"INFY": 102})
    assert len(updated2) == 0
    assert stop.price == Decimal("103")  # still 103


def test_trailing_percentage_long():
    portfolio = make_portfolio()
    pos = make_position(portfolio, qty=100, entry_price=100)
    manager = StopManager(portfolio)

    stop = manager.add_stop_loss(pos, stop_type="trailing_percentage", params={"pct": 0.02})
    # Initial 100*0.98=98
    assert stop.price == Decimal("98")

    # Price up to 110 – new stop 110*0.98=107.8
    updated = manager.update_trailing_stops({"INFY": 110})
    assert len(updated) == 1
    assert stop.price == Decimal("107.8")


def test_trailing_fixed_short():
    portfolio = make_portfolio()
    pos = make_position(portfolio, qty=-100, entry_price=100)
    manager = StopManager(portfolio)

    stop = manager.add_stop_loss(pos, stop_type="trailing_fixed", params={"trailing_amount": 2})
    # Short initial stop 100+2=102
    assert stop.price == Decimal("102")

    # Price goes down to 95 – trailing should move to 97
    updated = manager.update_trailing_stops({"INFY": 95})
    assert len(updated) == 1
    assert updated[0].price == Decimal("97")

    # Price up to 98 – should NOT move up (ratchet down only for short)
    updated2 = manager.update_trailing_stops({"INFY": 98})
    assert len(updated2) == 0
    assert stop.price == Decimal("97")


# ---------------------------------------------------------------------------
# Time-based stops
# ---------------------------------------------------------------------------


def test_time_based_stop():
    portfolio = make_portfolio()
    pos = make_position(portfolio, qty=100, entry_price=100)
    manager = StopManager(portfolio)

    stop = manager.add_stop_loss(pos, stop_type="time_based", params={"bars": 3})
    assert stop.stop_type == "time_based"

    # First 2 bars – no trigger
    for _ in range(2):
        hits = manager.check_stops({"INFY": {"close": 100}})
        assert len(hits) == 0

    # 3rd bar – should trigger
    hits = manager.check_stops({"INFY": {"close": 100}})
    assert len(hits) == 1
    assert hits[0].stop_type == "time_based"


# ---------------------------------------------------------------------------
# Take profit types
# ---------------------------------------------------------------------------


def test_risk_reward_take_profit():
    portfolio = make_portfolio()
    pos = make_position(portfolio, qty=100, entry_price=100)
    manager = StopManager(portfolio)

    # Risk 2%, RR 2:1 => target 4% above
    tp = manager.add_take_profit(
        pos, target_type="risk_reward", params={"risk_reward_ratio": 2, "stop_pct": 0.02}
    )
    # 100 * (1 + 0.02*2) =104
    assert tp.price == Decimal("104")


def test_resistance_take_profit():
    portfolio = make_portfolio()
    pos = make_position(portfolio, qty=100, entry_price=100)
    manager = StopManager(portfolio)

    tp = manager.add_take_profit(pos, target_type="resistance", params={"price": 120})
    assert tp.price == Decimal("120")


# ---------------------------------------------------------------------------
# Management features
# ---------------------------------------------------------------------------


def test_breakeven_move():
    portfolio = make_portfolio()
    pos = make_position(portfolio, qty=100, entry_price=100)
    manager = StopManager(portfolio)

    stop = manager.add_stop_loss(
        pos,
        stop_type="percentage",
        params={"pct": 0.02, "move_to_breakeven": True, "breakeven_trigger_pct": 0.03},
    )
    # Initial stop 98
    assert stop.price == Decimal("98")

    # Price goes to 103 (3% above entry) – should trigger breakeven move
    updated = manager.update_trailing_stops({"INFY": 103})
    assert len(updated) == 1
    assert stop.price == Decimal("100")  # moved to breakeven


def test_scale_out():
    portfolio = make_portfolio()
    pos = make_position(portfolio, qty=100, entry_price=100)
    manager = StopManager(portfolio)

    tp = manager.add_take_profit(
        pos, target_type="percentage", params={"pct": 0.05, "scale_out_pct": 0.5}
    )
    assert tp.scale_out_pct == Decimal("0.5")
    assert tp.quantity == Decimal("50")  # 50% of 100


def test_oco_orders():
    portfolio = make_portfolio()
    pos = make_position(portfolio, qty=100, entry_price=100)
    manager = StopManager(portfolio)

    sl = manager.add_stop_loss(
        pos, stop_type="percentage", params={"pct": 0.02, "oco_group": "exit1"}
    )
    tp = manager.add_take_profit(
        pos, target_type="percentage", params={"pct": 0.04, "oco_group": "exit1"}
    )

    assert sl.oco_group == "exit1"
    assert tp.oco_group == "exit1"

    # Trigger SL – TP should be cancelled via OCO
    hits = manager.check_stops({"INFY": {"close": 97, "low": 97}})
    assert len(hits) == 1
    assert hits[0].stop_id == sl.stop_id

    # TP should now be inactive
    active = manager.get_active_stops("INFY")
    assert len(active) == 0
    assert tp.is_active is False


def test_remove_stops():
    portfolio = make_portfolio()
    pos = make_position(portfolio, qty=100, entry_price=100)
    manager = StopManager(portfolio)

    manager.add_stop_loss(pos, stop_type="percentage", params={"pct": 0.02})
    manager.add_take_profit(pos, target_type="percentage", params={"pct": 0.05})

    assert len(manager.get_active_stops("INFY")) == 2

    removed = manager.remove_stops(pos.position_id)
    assert removed == 2
    assert len(manager.get_active_stops("INFY")) == 0


def test_create_orders_for_hits():
    portfolio = make_portfolio()
    pos = make_position(portfolio, qty=100, entry_price=100)
    manager = StopManager(portfolio)

    manager.add_stop_loss(pos, stop_type="fixed_price", params={"price": 95})

    hits = manager.check_stops({"INFY": {"close": 94, "low": 94}})
    assert len(hits) == 1

    orders = manager.create_orders_for_hits(hits)
    assert len(orders) == 1
    assert orders[0].symbol == "INFY"
    assert str(orders[0].side) == "sell"


def test_backtest_mode():
    portfolio = make_portfolio()
    pos = make_position(portfolio, qty=100, entry_price=100)
    manager = StopManager(portfolio, backtest_mode=True)

    manager.add_stop_loss(pos, stop_type="fixed_price", params={"price": 95})

    hits = manager.check_stops({"INFY": {"close": 94, "low": 94}})
    assert len(hits) == 1
    # In backtest mode, still returns hits but logs what would have happened
    assert hits[0].symbol == "INFY"


def test_multiple_stops_per_position():
    portfolio = make_portfolio()
    pos = make_position(portfolio, qty=100, entry_price=100)
    manager = StopManager(portfolio)

    # Add 3 stops for same position
    manager.add_stop_loss(pos, stop_type="percentage", params={"pct": 0.02})
    manager.add_stop_loss(pos, stop_type="trailing_percentage", params={"pct": 0.02})
    manager.add_stop_loss(pos, stop_type="time_based", params={"bars": 10})

    assert len(manager.get_active_stops("INFY")) == 3
    assert len(manager._stops[pos.position_id]) == 3


def test_stats():
    portfolio = make_portfolio()
    pos = make_position(portfolio, qty=100, entry_price=100)
    manager = StopManager(portfolio)

    manager.add_stop_loss(pos, stop_type="percentage", params={"pct": 0.02})
    assert manager.get_stats()["stops_added"] == 1

    hits = manager.check_stops({"INFY": {"close": 97, "low": 97}})
    assert manager.get_stats()["stops_triggered"] == 1
