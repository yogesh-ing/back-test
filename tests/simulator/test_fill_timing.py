"""Fill timing: the next-bar-open rule (ticket P1.3).

The behavioral law under test: a signal is computed from bars *through* bar
``t``; an order submitted on that knowledge is **never** filled at bar ``t``'s
close. The first :meth:`OrderExecutor.step` after :meth:`OrderExecutor.submit`
only *arms* the order; it trades at the **open** of the next completed bar.

Prices are asserted exactly (not within tolerance) by disabling slippage and
price improvement — this ticket is about *when* and *where* the fill price
comes from, not about cost modelling, which ``tests/test_simulator_execution.py``
and the slippage suite cover.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from backtest.simulator import (
    CommissionCalculator,
    ExecutionConfig,
    ExecutionStatus,
    Order,
    OrderExecutor,
    OrderStatus,
    RejectionCode,
    SlippageCalculator,
    ValidationError,
)

D = Decimal
IST = "Asia/Kolkata"


@dataclass
class Bar:
    """A completed bar. ``close`` must never leak into a fill price."""

    open: Decimal
    close: Decimal
    volume: Decimal | None = None
    timestamp: datetime | None = None


def make_executor(**kw) -> OrderExecutor:
    kw.setdefault("slippage", SlippageCalculator.disabled())
    kw.setdefault("fees", CommissionCalculator())
    config = kw.pop("config", None) or ExecutionConfig(seed=7, price_improvement_probability=D("0"))
    return OrderExecutor(config=config, **kw)


# ===========================================================================
# The ticket's tests
# ===========================================================================


def test_no_lookahead_single_bar():
    ex = make_executor()
    bar_t = Bar(open=D("100"), close=D("105"))
    ex.submit(Order.market("INFY", "buy", 100))
    fills_after_t = ex.step(bar_t)  # feeds bar t
    assert fills_after_t == []  # NOT filled yet (no look-ahead!)

    bar_t1 = Bar(open=D("103"), close=D("110"))  # next bar
    fills_after_t1 = ex.step(bar_t1)
    assert len(fills_after_t1) == 1
    assert fills_after_t1[0].fill is not None
    assert fills_after_t1[0].fill.fill_price == D("103")  # MUST be bar t+1's OPEN
    assert fills_after_t1[0].fill.fill_price != bar_t.close  # never bar t's close


def test_multi_order_stays_in_sequence():
    # two orders submitted on consecutive bars fill in order, each at next open
    ex = make_executor()
    a = Order.market("INFY", "buy", 100)
    ex.submit(a)

    assert ex.step(Bar(open=D("100"), close=D("101"))) == []  # t0: arms a

    b = Order.market("TCS", "buy", 50)
    ex.submit(b)

    r1 = ex.step(Bar(open=D("102"), close=D("103")))  # t1: a fills, b arms
    assert [r.order_id for r in r1] == [a.order_id]
    assert r1[0].fill is not None
    assert r1[0].fill.fill_price == D("102")

    r2 = ex.step(Bar(open=D("400"), close=D("401")))  # t2: b fills
    assert [r.order_id for r in r2] == [b.order_id]
    assert r2[0].fill is not None
    assert r2[0].fill.fill_price == D("400")


# ===========================================================================
# Acceptance: never bar t's close, always t+1's open
# ===========================================================================


@pytest.mark.parametrize(
    "open_t, close_t, open_t1",
    [
        (D("100"), D("105"), D("103")),  # typical: close drifts, next open reverts
        (D("100"), D("99"), D("98.5")),  # gap down
        (D("100"), D("100.01"), D("100.01")),  # close ≈ next open (coincidence ok)
    ],
)
def test_fill_price_is_next_open_not_current_close(open_t, close_t, open_t1):
    ex = make_executor()
    ex.submit(Order.market("INFY", "buy", 100))
    assert ex.step(Bar(open=open_t, close=close_t)) == []
    result = ex.step(Bar(open=open_t1, close=open_t1 + D("2")))
    assert len(result) == 1
    assert result[0].fill is not None
    assert result[0].fill.fill_price == open_t1


def test_sell_fills_at_next_open_too():
    ex = make_executor()
    ex.submit(Order.market("INFY", "sell", 100))
    assert ex.step(Bar(open=D("100"), close=D("97"))) == []
    result = ex.step(Bar(open=D("96"), close=D("95")))
    assert result[0].fill is not None
    assert result[0].fill.fill_price == D("96")


def test_bar_without_volume_or_timestamp_still_fills_at_open():
    ex = make_executor()
    ex.submit(Order.market("INFY", "buy", 100))
    ex.step(Bar(open=D("100"), close=D("101")))
    result = ex.step(Bar(open=D("100.5"), close=D("101")))
    assert result[0].fill is not None
    assert result[0].fill.fill_price == D("100.5")


# ===========================================================================
# Resting and partial orders
# ===========================================================================


def test_limit_rests_until_an_open_crosses_it():
    ex = make_executor()
    o = Order.limit("INFY", "buy", 100, limit_price=D("98"))
    ex.submit(o)

    assert ex.step(Bar(open=D("100"), close=D("101"))) == []  # arm
    r1 = ex.step(Bar(open=D("99"), close=D("100")))  # open 99 > 98: still away
    assert len(r1) == 1
    assert r1[0].status == ExecutionStatus.NO_FILL
    assert not r1[0].did_trade
    assert o.is_working

    r2 = ex.step(Bar(open=D("97"), close=D("98")))  # open 97 < 98: trades through
    assert len(r2) == 1
    assert r2[0].did_trade
    assert r2[0].fill is not None
    assert r2[0].fill.fill_price == D("97")
    assert o.status is OrderStatus.FILLED


def test_partial_fill_remainder_trades_at_next_open():
    ex = make_executor()  # max_participation 0.1
    o = Order.market("INFY", "buy", 100)
    ex.submit(o)

    assert ex.step(Bar(open=D("100"), close=D("101"), volume=D("5000"))) == []  # arm
    r1 = ex.step(Bar(open=D("101"), close=D("102"), volume=D("500")))  # cap 50
    assert len(r1) == 1
    assert r1[0].is_partial
    assert r1[0].fill is not None
    assert r1[0].fill.fill_price == D("101")
    assert r1[0].fill.quantity == D("50")
    assert o.remaining_quantity == D("50")
    assert o.is_working

    r2 = ex.step(Bar(open=D("102"), close=D("103"), volume=D("10000")))  # cap 1000
    assert len(r2) == 1
    assert r2[0].is_filled
    assert r2[0].fill is not None
    assert r2[0].fill.fill_price == D("102")  # remainder at the NEXT open
    assert o.status is OrderStatus.FILLED


# ===========================================================================
# Terminal outcomes and queue hygiene
# ===========================================================================


def test_halted_order_is_rejected_and_leaves_the_queue():
    ex = make_executor()
    ex.halt("INFY")
    ex.submit(Order.market("INFY", "buy", 100))

    assert ex.step(Bar(open=D("100"), close=D("101"))) == []  # arm
    r = ex.step(Bar(open=D("101"), close=D("102")))
    assert len(r) == 1
    assert r[0].is_rejected
    assert r[0].rejection_code == RejectionCode.SYMBOL_HALTED

    assert ex.step(Bar(open=D("102"), close=D("103"))) == []  # queue is empty


def test_externally_cancelled_order_is_dropped():
    ex = make_executor()
    o = Order.market("INFY", "buy", 100)
    ex.submit(o)
    ex.step(Bar(open=D("100"), close=D("101")))  # arm
    o.cancel("risk limit")
    assert ex.step(Bar(open=D("101"), close=D("102"))) == []
    assert ex.step(Bar(open=D("102"), close=D("103"))) == []


def test_submit_twice_raises():
    ex = make_executor()
    o = Order.market("INFY", "buy", 100)
    ex.submit(o)
    with pytest.raises(ValidationError, match="already queued"):
        ex.submit(o)


def test_submit_terminal_order_raises():
    ex = make_executor()
    o = Order.market("INFY", "buy", 100)
    o.submit()
    o.cancel("done")
    with pytest.raises(ValidationError):
        ex.submit(o)


def test_submit_submits_a_pending_order_automatically():
    ex = make_executor()
    o = Order.market("INFY", "buy", 100)
    assert not o.is_working
    ex.submit(o)
    assert o.is_working


def test_reset_clears_the_queue():
    ex = make_executor()
    ex.submit(Order.market("INFY", "buy", 100))
    ex.reset()
    assert ex.step(Bar(open=D("100"), close=D("101"))) == []
    assert ex.step(Bar(open=D("101"), close=D("102"))) == []


def test_step_requires_a_bar_with_open():
    ex = make_executor()
    ex.submit(Order.market("INFY", "buy", 100))
    ex.step(Bar(open=D("100"), close=D("101")))
    with pytest.raises(ValidationError, match="open"):
        ex.step(object())  # no .open attribute


def test_timestamped_bar_is_accepted():
    ex = make_executor()
    ex.submit(Order.market("INFY", "buy", 100))
    ex.step(Bar(open=D("100"), close=D("101")))
    result = ex.step(
        Bar(
            open=D("101"),
            close=D("102"),
            timestamp=datetime(2024, 1, 2, 9, 15, tzinfo=timezone.utc),
        )
    )
    assert len(result) == 1
    assert result[0].fill is not None
    assert result[0].fill.fill_price == D("101")
