"""Task B2 — expiry settlement and roll inside the forward loop.

B1 shipped a days-to-expiry *square-off* (the established ``auto_square_off``
reason), but nothing in the forward path could actually **settle** a contract
at expiry: ``ExpiryManager.process_expiries`` was only called by the backtest
driver and the dashboard, never by a runner. A forward test that rides into
expiry therefore held a position whose only mark was a Black-Scholes price at
zero time value, and the book never booked the settlement.

B2 drives the canonical pipeline from the bridge's bar clock:

* settle **on** the expiry bar (``include_today=True``) — index options settle
  on the expiry-day close, which is the price the bar carries;
* settlement spot = the synthetic market's spot for that bar;
* ``exit_reason = "expiry_settlement"`` on the structure, ``EXPIRED`` on the
  legs, cash moved by intrinsic, P&L net of the premium paid;
* the next view re-enters on the next monthly expiry (a **roll**).

Also covered here: the settlement P&L fix. ``settle_expired`` used to book the
*gross* intrinsic, which double-counted the premium and inflated book equity by
a full premium per settled leg.

See ``docs/OPTIONS-FORWARD-TESTING.md`` → task B2.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from backtest.forward.options_bridge import OptionsBridge
from backtest.forward.paper_runner import OrderLedger, RunnerConfig, StrategyRunner
from backtest.options.exit_policy import EXIT_DTE
from backtest.options.expiry import EXIT_REASON_SETTLEMENT
from backtest.options.paper_trading import PositionStatus
from backtest.options.quote_providers import (
    SyntheticChainGenerator,
    SyntheticQuoteProvider,
)
from backtest.strategy.intent import Direction, MarketView


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _bars(closes, start_day=1):
    base = date(2026, 9, 1) + timedelta(days=start_day - 1)
    return [
        {
            "ts": f"{(base + timedelta(days=i)).isoformat()}T09:15:00",
            "open": close - 10,
            "high": close + 30,
            "low": close - 30,
            "close": float(close),
            "volume": 1000,
        }
        for i, close in enumerate(closes)
    ]


def _view(direction: Direction, spot: float = 24_800.0) -> MarketView:
    return MarketView(
        direction=direction,
        confidence=0.9,
        underlying="NIFTY",
        spot_price=Decimal(str(spot)),
    )


def _ride_to_settlement_bridge(capital: float = 1_000_000.0) -> OptionsBridge:
    """A bridge whose policy holds the structure into expiry."""
    return OptionsBridge(
        capital=capital,
        expression={
            "type": "long_call",
            "exit": {"min_days_to_expiry": None, "signal_flip": False},
        },
        quote_provider=SyntheticQuoteProvider(SyntheticChainGenerator()),
    )


def _open_at_bar(bridge: OptionsBridge, day: date, spot: float = 24_800.0):
    """Open a structure on a given bar date and return it."""
    bridge.on_bar("NIFTY", spot, f"{day.isoformat()}T09:15:00")
    result = bridge.on_market_view(_view(Direction.BULLISH, spot), "s")
    assert result is not None and not result.get("rejected")
    return bridge.option_broker.get_open_structures()[0]


def _runner(**expression_exit):
    exit_cfg = {"min_days_to_expiry": None, "signal_flip": False}
    exit_cfg.update(expression_exit)
    expression = {"type": "long_call", "exit": exit_cfg}
    runner = StrategyRunner(
        RunnerConfig(
            name="opt-runner",
            strategy_name="directional_options",
            allocated_capital=1_000_000,
            symbols=["NIFTY"],
            timeframe="1day",
            instrument={"type": "option", "expression": expression},
        ),
        ledger=OrderLedger(),
    )
    runner.start()
    return runner


# ---------------------------------------------------------------------------
# Settlement on the expiry bar
# ---------------------------------------------------------------------------


class TestExpirySettlement:
    def test_structure_held_to_expiry_is_cash_settled(self):
        bridge = _ride_to_settlement_bridge()
        start = date(2026, 9, 2)
        structure = _open_at_bar(bridge, start)
        expiry = structure.expiry

        # Hold to the expiry bar itself.
        bridge.on_bar("NIFTY", 25_100.0, f"{expiry.isoformat()}T09:15:00")

        assert bridge.open_structure_id is None
        assert bridge.closed_count == 1
        assert bridge.summary()["settled_count"] == 1

        event = bridge.pop_exit_event()
        assert event is not None
        assert event["reason"] == EXIT_REASON_SETTLEMENT
        assert event["settled"] is True
        assert "cash settled at expiry" in event["detail"]

        settled = bridge.option_broker.get_closed_structures()[0]
        assert settled.exit_reason == EXIT_REASON_SETTLEMENT
        assert all(leg.status == PositionStatus.EXPIRED for leg in settled.legs)

    def test_settlement_waits_until_the_expiry_bar(self):
        """Nothing settles early — and the day after expiry also settles."""
        bridge = _ride_to_settlement_bridge()
        structure = _open_at_bar(bridge, date(2026, 9, 2))
        expiry = structure.expiry

        bridge.on_bar("NIFTY", 24_900.0, f"{(expiry - timedelta(days=2)).isoformat()}T09:15:00")
        assert bridge.open_structure_id is not None
        assert bridge.summary()["settled_count"] == 0

        bridge.on_bar("NIFTY", 24_900.0, f"{(expiry + timedelta(days=1)).isoformat()}T09:15:00")
        assert bridge.summary()["settled_count"] == 1

    def test_settlement_pnl_is_net_of_premium(self):
        """Settling ITM books (intrinsic − premium) × units, not the gross."""
        bridge = _ride_to_settlement_bridge()
        structure = _open_at_bar(bridge, date(2026, 9, 2))
        entry_premium = structure.total_entry_cost
        units = structure.legs[0].total_quantity
        expiry = structure.expiry

        # Settle 300 points above a 24,800 ATM strike.
        bridge.on_bar("NIFTY", 25_100.0, f"{expiry.isoformat()}T09:15:00")
        settled = bridge.option_broker.get_closed_structures()[0]
        pnl = settled.total_realized_pnl

        assert pnl > 0
        # ≤ the spread's max payoff (intrinsic × units); and strictly less than
        # gross intrinsic because the premium is netted out.
        assert pnl < Decimal("300") * Decimal(str(units))
        assert pnl == pytest.approx(
            (Decimal("300") - entry_premium / Decimal(str(units)))
            * Decimal(str(units)),
            rel=1e-3,
        )

    def test_equity_reconciles_after_settlement(self):
        """Capital + realized P&L must equal cash once the book is flat."""
        bridge = _ride_to_settlement_bridge()
        broker = bridge.option_broker
        structure = _open_at_bar(bridge, date(2026, 9, 2))
        expiry = structure.expiry

        bridge.on_bar("NIFTY", 25_100.0, f"{expiry.isoformat()}T09:15:00")

        assert bridge.open_structure_id is None
        costs = broker.total_costs_paid
        # Equity anchors on capital + realized − fees; cash is what actually
        # moved. They agree once nothing is open.
        assert broker.total_equity == pytest.approx(broker.available_cash, abs=0.01)
        assert broker.total_equity == pytest.approx(
            broker.capital + broker.total_realized_pnl - costs, abs=0.01
        )

    def test_otm_expiry_loses_exactly_the_premium(self):
        bridge = _ride_to_settlement_bridge()
        structure = _open_at_bar(bridge, date(2026, 9, 2))
        entry_premium = structure.total_entry_cost
        expiry = structure.expiry
        units = structure.legs[0].total_quantity

        # Crater the underlying: the long call expires worthless.
        bridge.on_bar("NIFTY", 20_000.0, f"{expiry.isoformat()}T09:15:00")

        settled = bridge.option_broker.get_closed_structures()[0]
        assert settled.exit_reason == EXIT_REASON_SETTLEMENT
        assert settled.total_realized_pnl == pytest.approx(
            -entry_premium, rel=1e-3
        )
        assert abs(float(entry_premium) / units) > 0  # premium was real

    def test_settlement_does_not_fire_with_the_default_dte_rule(self):
        """Default policy squares off a day early → nothing to settle.

        ``min_days_to_expiry = 1`` is the shipped default, so the common path
        is an ``auto_square_off`` close; settlement is for riders.
        """
        bridge = OptionsBridge(
            capital=1_000_000.0,
            expression={"type": "long_call", "exit": {"signal_flip": False}},
            quote_provider=SyntheticQuoteProvider(SyntheticChainGenerator()),
        )
        structure = _open_at_bar(bridge, date(2026, 9, 2))
        expiry = structure.expiry

        bridge.on_bar("NIFTY", 24_800.0, f"{(expiry - timedelta(days=1)).isoformat()}T09:15:00")

        assert bridge.summary()["settled_count"] == 0
        assert bridge.closed_count == 1
        assert bridge.last_exit["reason"] == EXIT_DTE

        bridge.on_bar("NIFTY", 24_800.0, f"{expiry.isoformat()}T09:15:00")
        assert bridge.summary()["settled_count"] == 0  # nothing left to settle

    def test_settlement_needs_no_view(self):
        """Pool runners never route views — settlement must still happen."""
        bridge = _ride_to_settlement_bridge()
        structure = _open_at_bar(bridge, date(2026, 9, 2))
        expiry = structure.expiry
        for _ in range(4):
            bridge.on_bar("NIFTY", 24_800.0, f"{expiry.isoformat()}T09:15:00")
        assert bridge.summary()["settled_count"] == 1

    def test_settlement_advances_the_book_clock(self):
        """Days-to-expiry is measured on the bar clock, not the wall clock."""
        bridge = _ride_to_settlement_bridge()
        structure = _open_at_bar(bridge, date(2026, 9, 2))
        expiry = structure.expiry
        assert expiry > date(2026, 9, 2)
        bridge.on_bar("NIFTY", 24_800.0, f"{expiry.isoformat()}T09:15:00")
        assert bridge.last_exit["reason"] == EXIT_REASON_SETTLEMENT


# ---------------------------------------------------------------------------
# Roll
# ---------------------------------------------------------------------------


class TestRoll:
    def test_next_view_rolls_to_the_next_expiry(self):
        bridge = OptionsBridge(
            capital=1_000_000.0,
            expression={
                "type": "bull_call_spread",
                "exit": {"min_days_to_expiry": None, "signal_flip": False},
            },
            quote_provider=SyntheticQuoteProvider(SyntheticChainGenerator()),
        )
        first = _open_at_bar(bridge, date(2026, 9, 2))
        first_id, first_expiry = first.structure_id, first.expiry

        # Settle it, then give the strategy the same conviction a day later.
        bridge.on_bar("NIFTY", 24_900.0, f"{first_expiry.isoformat()}T09:15:00")
        assert bridge.open_structure_id is None

        after = first_expiry + timedelta(days=1)
        bridge.on_bar("NIFTY", 24_900.0, f"{after.isoformat()}T09:15:00")
        result = bridge.on_market_view(_view(Direction.BULLISH, 24_900.0), "s")

        assert result is not None and not result.get("rejected")
        rolled = bridge.option_broker.get_open_structures()[0]
        assert rolled.structure_id != first_id
        assert rolled.expiry > first_expiry
        assert bridge.executed_count == 2
        assert bridge.summary()["settled_count"] == 1

    def test_runner_logs_settlement_then_rolls(self):
        """End-to-end: a runner riding into expiry settles and re-enters."""
        runner = _runner()
        # 60 daily bars from 2026-09-01: rally through and past the expiry.
        closes = [24_800 + i * 30 for i in range(60)]
        for bar in _bars(closes):
            runner.process_candle_event("NIFTY", bar)

        summary = runner.options_summary()
        assert summary["executed_count"] >= 2, "the runner must roll into a new expiry"
        assert summary["settled_count"] >= 1, "a ridden position must settle"
        assert summary["closed_count"] >= 1

        # Settlements carry their own signal kind (B3) so a log reader can tell
        # "we chose to close" from "it expired on us".
        settled = [s for s in runner.signal_log if s["kind"] == "OPTION_SETTLED"]
        assert settled
        assert "cash settled at expiry" in settled[0]["reason"]

        entries = [s for s in runner.signal_log if s["kind"] == "OPTION_ENTRY"]
        expiries = [e["reason"].split("expiry=")[1].split(" ")[0] for e in entries]
        assert expiries == sorted(expiries)
        assert len(set(expiries)) >= 2, "the roll must move to a later expiry"

    def test_runner_equity_reflects_settlement(self):
        runner = _runner()
        closes = [24_800 + i * 30 for i in range(60)]
        for bar in _bars(closes):
            runner.process_candle_event("NIFTY", bar)

        booked = runner.realized_pnl
        assert booked != 0
        assert runner.option_pnl() != 0
        assert runner.equity() == pytest.approx(1_000_000 + runner.option_pnl())
