"""Ticket 1.4 / BUG-26 — an equity paper round trip pays its own costs.

Paper equity buckets executed on ``free_executor``, whose zero-fee profile was
written for V1 ("paper trading had no costs"). The consequence was exactly the
one a trader cares about: a 1-minute scalper booked gross P&L while the venue
would have taken STT, exchange txn, SEBI, IPFT, stamp and GST out of it, so the
book read profitable at precisely the frequency it should have been taxed. The
same round trip also lost WHY it closed — the exit order's reason never reached
the position.

Contract under test:

* the fee engine is resolved like the settings panel resolves it (DB →
  ``config/brokers.yaml`` → preset), never hardcoded;
* the intraday/delivery distinction comes from the runner's **timeframe**, not
  ``config.segment`` (a capital partition here) — 8x on a round trip;
* ``brokerage_total`` + ``statutory_fees_total`` == ``commission_total``, and
  ``net_pnl == realized_pnl − commission_total``;
* a cost model that cannot be resolved raises instead of trading free;
* ``exit_reason`` rides the closing fill's tag onto the position, the trade
  dict, and the DB row.

No live market, no broker session — the DB broker layer is pinned out so these
assertions measure the yaml/preset cost model rather than whatever the deployed
panel row says today.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

import backtest.simulator.fees as fees_module
from backtest.forward.paper_runner import (
    INTRADAY_FEE_TIMEFRAMES,
    FillEvent,
    OrderLedger,
    RunnerConfig,
    StrategyRunner,
    equity_costed_executor,
    equity_fee_broker,
    equity_fee_segment,
    equity_fee_split,
)
from backtest.plugins import discover_plugins
from backtest.simulator.fees import PAPER_FREE_PROFILE, TradeSegment
from backtest.simulator.position import Position

BASE_DAY = date(2026, 9, 10)


@pytest.fixture(autouse=True)
def _testing_profile(monkeypatch):
    """Deterministic fee resolution: skip the settings-panel DB layer."""
    monkeypatch.setenv("BACKTEST_DATA_PROFILE", "testing")
    monkeypatch.setattr(fees_module, "_load_db_broker_profile", lambda broker: None)
    discover_plugins()


@pytest.fixture()
def db_manager():
    """In-memory SQLite with the full schema — the permanent layer, no server."""
    from backtest.db import DatabaseManager
    from backtest.db.config import DatabaseConfig
    from backtest.db.models import Base

    manager = DatabaseManager(DatabaseConfig(url="sqlite://", echo=False))
    manager.connect()
    Base.metadata.create_all(manager.engine)
    yield manager
    manager.disconnect()


def _bars(closes, symbol: str = "RELIANCE"):
    return [
        {
            "ts": f"{(BASE_DAY + timedelta(days=i)).isoformat()}T09:15:00",
            "open": close - 1,
            "high": close + 5,
            "low": close - 5,
            "close": float(close),
            "volume": 1000,
        }
        for i, close in enumerate(closes)
    ]


def _equity_runner(timeframe: str = "1min", **overrides) -> StrategyRunner:
    kwargs = dict(
        name="EQ",
        strategy_name="buy_and_hold",
        allocated_capital=1_000_000,
        symbols=["RELIANCE"],
        timeframe=timeframe,
        mode="paper",
        source="synthetic",
    )
    kwargs.update(overrides)
    runner = StrategyRunner(RunnerConfig(**kwargs), ledger=OrderLedger())
    runner.start()
    for bar in _bars([100.0] * 14):
        runner.process_candle_event("RELIANCE", bar)
    assert runner.positions, "fixture failed to open a position"
    return runner


def _round_trip(runner: StrategyRunner, exit_price: float) -> Position:
    runner.last_price["RELIANCE"] = exit_price
    runner.close_position("RELIANCE", reason="manual_target")
    closed = [p for p in runner.portfolio.closed_positions if p.symbol == "RELIANCE"]
    assert closed, "close did not land in closed_positions"
    return closed[-1]


# ---------------------------------------------------------------------------
# Resolution: which cost model, which segment
# ---------------------------------------------------------------------------


class TestCostModelResolution:
    def test_intraday_timeframes_price_as_intraday(self):
        for timeframe in sorted(INTRADAY_FEE_TIMEFRAMES):
            assert equity_fee_segment(timeframe) == TradeSegment.EQUITY_INTRADAY

    def test_swing_timeframes_price_as_delivery(self):
        for timeframe in ("1day", "3day", "1week", "1month"):
            assert equity_fee_segment(timeframe) == TradeSegment.EQUITY_DELIVERY

    def test_segment_is_case_and_space_tolerant(self):
        assert equity_fee_segment(" 1MIN ") == TradeSegment.EQUITY_INTRADAY

    def test_data_only_feed_names_no_broker(self):
        """Where the bars came from is not whose rates apply."""
        def cfg(execution_broker=None, source=""):
            return SimpleNamespace(execution_broker=execution_broker, source=source)

        assert equity_fee_broker(cfg(source="synthetic")) is None
        assert equity_fee_broker(cfg(source="replay")) is None
        assert equity_fee_broker(cfg(execution_broker="dhan", source="mstock")) == "dhan"
        assert equity_fee_broker(cfg(source="mstock")) == "mstock"

    def test_runner_costs_its_fills_with_the_named_broker(self):
        runner = _equity_runner()
        assert runner.fee_profile == "zerodha"
        assert runner.fee_segment == TradeSegment.EQUITY_INTRADAY
        assert runner.executor.fees.broker.name != PAPER_FREE_PROFILE.name

    def test_crypto_universe_stays_free(self):
        """A USD-quoted pair has no Indian cash-market cost model: booking STT
        and GST on it would be inventing a charge, not measuring one."""
        cfg = RunnerConfig(
            name="CRYPTO",
            strategy_name="buy_and_hold",
            allocated_capital=100_000,
            symbols=["BTC/USD"],
            timeframe="1hour",
            mode="paper",
            source="synthetic",
        )
        executor = equity_costed_executor(_blank_portfolio(), cfg)
        assert executor.fees.broker.name == PAPER_FREE_PROFILE.name

    def test_unresolvable_cost_model_refuses_instead_of_trading_free(self, monkeypatch):
        def boom(**kwargs):
            raise RuntimeError("panel row is corrupt")

        monkeypatch.setattr(fees_module, "load_broker_profile", boom)
        cfg = RunnerConfig(
            name="LOUD",
            strategy_name="buy_and_hold",
            allocated_capital=100_000,
            symbols=["RELIANCE"],
            timeframe="1min",
            mode="paper",
            source="synthetic",
        )
        with pytest.raises(ValueError, match="never fall back"):
            equity_costed_executor(_blank_portfolio(), cfg)


def _blank_portfolio():
    from backtest.simulator.portfolio import Portfolio

    return Portfolio(name="probe", initial_capital=100_000, mode="paper")


# ---------------------------------------------------------------------------
# The money identity
# ---------------------------------------------------------------------------


class TestRoundTripCosts:
    def test_round_trip_pays_brokerage_and_statutory_charges(self):
        closed = _round_trip(_equity_runner(), exit_price=95.0)
        assert closed.brokerage_total > 0
        assert closed.statutory_fees_total > 0

    def test_fee_split_sums_to_the_commission_total(self):
        closed = _round_trip(_equity_runner(), exit_price=95.0)
        assert closed.commission_total == closed.brokerage_total + closed.statutory_fees_total

    def test_net_pnl_is_gross_less_the_whole_cost_stack(self):
        closed = _round_trip(_equity_runner(), exit_price=101.0)
        assert closed.net_pnl == closed.realized_pnl - closed.commission_total
        # A scalp that only beat the entry by ₹1 does not beat its costs.
        assert closed.realized_pnl > 0
        assert closed.net_pnl < closed.realized_pnl

    def test_costs_are_paid_out_of_the_book(self):
        """Cash after a full round trip = capital + gross P&L − the cost stack."""
        runner = _equity_runner()
        closed = _round_trip(runner, exit_price=101.0)
        assert runner.portfolio.current_cash == (
            Decimal(str(runner.config.allocated_capital))
            + closed.realized_pnl
            - closed.commission_total
        )

    def test_delivery_pays_more_stt_than_intraday_on_the_same_trip(self):
        """The 8x distinction that made segment a bug-class detail."""
        intraday = _round_trip(_equity_runner(), exit_price=110.0)
        delivery = _round_trip(_equity_runner(timeframe="1day"), exit_price=110.0)
        assert delivery.statutory_fees_total > intraday.statutory_fees_total


# ---------------------------------------------------------------------------
# What a closed trade reports
# ---------------------------------------------------------------------------


class TestClosedTradeReporting:
    def test_equity_trade_dict_names_its_costs_and_reason(self):
        runner = _equity_runner()
        _round_trip(runner, exit_price=95.0)
        trade = [t for t in runner.closed_trades if t["kind"] == "equity"][-1]
        assert trade["commission"] > 0
        assert trade["statutory_fees"] > 0
        assert trade["gross_pnl"] == pytest.approx(trade["pnl"])
        assert trade["net_pnl"] == pytest.approx(
            trade["gross_pnl"] - trade["commission"] - trade["statutory_fees"], abs=0.01
        )
        assert trade["segment"] == TradeSegment.EQUITY_INTRADAY
        assert trade["exit_reason"] == "manual_target"

    def test_audit_record_carries_the_same_split(self):
        runner = _equity_runner()
        _round_trip(runner, exit_price=95.0)
        rec = [t for t in runner._closed_trades if t["symbol"] == "RELIANCE"][-1]
        assert Decimal(str(rec["gross_pnl"])) > 0 or Decimal(str(rec["gross_pnl"])) < 0
        assert Decimal(str(rec["commission"])) > 0
        assert Decimal(str(rec["statutory_fees"])) > 0
        assert rec["exit_reason"] == "manual_target"
        assert Decimal(str(rec["net_pnl"])) == (
            Decimal(str(rec["gross_pnl"]))
            - Decimal(str(rec["commission"]))
            - Decimal(str(rec["statutory_fees"]))
        )

    def test_legacy_book_without_a_split_is_not_read_as_free(self):
        """A snapshot written before the split only has ``commission_total``."""
        pos = Position(symbol="RELIANCE", quantity=100, average_entry_price=100)
        pos.commission_total = Decimal("12.34")
        assert equity_fee_split(pos) == (12.34, 0.0)

    def test_split_survives_the_state_snapshot(self):
        pos = Position(symbol="RELIANCE", quantity=100, average_entry_price=100)
        pos.note_fee_split(Decimal("6.00"), Decimal("4.34"))
        pos.exit_reason = "stop_loss"
        restored = Position.from_dict({**pos.to_dict(), "quantity": "0"})
        assert restored.brokerage_total == Decimal("6.00")
        assert restored.statutory_fees_total == Decimal("4.34")
        assert restored.exit_reason == "stop_loss"


# ---------------------------------------------------------------------------
# exit_reason: stamped from the closing fill, never guessed
# ---------------------------------------------------------------------------


class TestExitReasonStamping:
    def _runner_with_closed_book(self) -> StrategyRunner:
        runner = _equity_runner()
        _round_trip(runner, exit_price=95.0)
        return runner

    def test_reason_reaches_the_position_from_the_order_tag(self):
        closed = self._runner_with_closed_book().portfolio.closed_positions[-1]
        assert closed.exit_reason == "manual_target"

    def test_first_reason_wins_on_a_re_delivered_fill(self):
        runner = self._runner_with_closed_book()
        closed = runner.portfolio.closed_positions[-1]
        runner._record_closed_trade(
            FillEvent(
                client_order_id="PRT-x",
                instance_id=runner.instance_id,
                symbol="RELIANCE",
                side="SELL",
                quantity=float(closed.closed_quantity or 0),
                price=float(closed.current_price or 95.0),
                ts=closed.closed_at.isoformat(),
                tag={"kind": "exit", "reason": "manual_close"},
            )
        )
        assert closed.exit_reason == "manual_target"

    def test_absent_reason_is_never_invented(self):
        runner = _equity_runner()
        _round_trip(runner, exit_price=95.0)
        closed = runner.portfolio.closed_positions[-1]
        closed.exit_reason = None
        runner._closed_trades.clear()
        runner._record_closed_trade(
            FillEvent(
                client_order_id="PRT-y",
                instance_id=runner.instance_id,
                symbol="RELIANCE",
                side="SELL",
                quantity=float(closed.closed_quantity or 0),
                price=float(closed.current_price or 95.0),
                ts=closed.closed_at.isoformat(),
            )
        )
        assert closed.exit_reason is None
        assert runner._closed_trades[-1]["exit_reason"] is None


# ---------------------------------------------------------------------------
# Persistence: the columns the DB already has
# ---------------------------------------------------------------------------


class TestPersistence:
    def test_equity_trade_lands_with_gross_net_and_costs(self, db_manager):
        from backtest.forward.trade_persistence import LiveTradePersister

        runner = _equity_runner()
        closed = _round_trip(runner, exit_price=95.0)
        # mStock is the only source the permanent history accepts.
        runner.config.source = "mstock"
        persister = LiveTradePersister(db_manager)
        assert persister.flush_runner(runner) >= 1

        rows = db_manager.fetch_all(
            "SELECT gross_pnl, net_pnl, commission_total, exit_reason FROM trades"
        )
        assert len(rows) == 1
        row = rows[0]
        assert float(row["gross_pnl"]) == pytest.approx(float(closed.realized_pnl), abs=0.01)
        assert float(row["commission_total"]) == pytest.approx(
            float(closed.commission_total), abs=0.01
        )
        assert float(row["net_pnl"]) == pytest.approx(float(closed.net_pnl), abs=0.01)
        assert row["exit_reason"] == "take_profit"
