"""BUG-04 (ticket 2.2) — one NET P&L contract on every row and snapshot.

The live defect was three books quoting three different things:
``realized_pnl`` was GROSS (price P&L, costs nowhere) while ``daily_pnl`` and
``equity`` were NET, so a runner could show +₹20,335.80 realized on a book
that had actually lost money, and an equity scale-out dropped its realized
half out of ``daily_pnl`` because the closed-at-today filter and the mark were
computed on different clocks.

The contract every row now emits, from the same primitives:

    gross_realized_pnl  lifetime price P&L on closed round trips (both books)
    total_fees_paid     lifetime brokerage + statutory charges (both books)
    realized_pnl        NET realized — gross less the fees the CLOSED trips cost
    realized_pnl_today  NET realized booked in the current session
    open_pnl            NET mark — gross MTM less the fees the OPEN books cost
    daily_pnl           realized_pnl_today + (open_pnl − prev_close_open_pnl)

Fee attribution is deliberate: a charge follows the book that paid it, so an
open position's entry brokerage sits in ``open_pnl`` (it is still at risk) and
moves to ``realized_pnl`` the moment that position closes. The SPLIT is a
convention; the SUM is not, which is why the two invariants below hold for
every state a book can be in.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

import pytest

from backtest.forward.paper_runner import (
    OrderLedger,
    RunnerConfig,
    SIDE_BUY,
    SIDE_SELL,
    StrategyRunner,
)
from backtest.forward.state_store import capture_runner, restore_runner

BASE_DAY = date(2026, 9, 10)
TOLERANCE = 0.02  # the plan's rupee-exact bar (rounding only)

#: Minutes already consumed inside each fixture session. ``process_candle_event``
#: de-dups a bar whose stamp is not newer, so every feed must advance the clock.
_CURSOR: dict = {}


def _bars(closes, start_day: int = 0):
    """Bars inside ONE session — the warmup must not roll the day anchors."""
    base = datetime.combine(BASE_DAY + timedelta(days=start_day), time(9, 0))
    start_minute = _CURSOR.get(start_day, 0)
    _CURSOR[start_day] = start_minute + len(closes)
    return [
        {
            "ts": (base + timedelta(minutes=start_minute + i)).isoformat(),
            "open": close - 1,
            "high": close + 5,
            "low": close - 5,
            "close": float(close),
            "volume": 1000,
        }
        for i, close in enumerate(closes)
    ]


def _runner(**overrides) -> StrategyRunner:
    kwargs = dict(
        name="EQ",
        strategy_name="buy_and_hold",
        allocated_capital=1_000_000,
        symbols=["RELIANCE"],
        timeframe="1day",
        mode="paper",
        source="synthetic",
    )
    kwargs.update(overrides)
    runner = StrategyRunner(RunnerConfig(**kwargs), ledger=OrderLedger())
    runner.start()
    return runner


def _feed(runner: StrategyRunner, closes, start_day: int = 0) -> None:
    for bar in _bars(closes, start_day=start_day):
        runner.process_candle_event("RELIANCE", bar)


def _open_a_position(runner: StrategyRunner, price: float = 100.0) -> None:
    _feed(runner, [price] * 14)
    assert runner.positions, "fixture failed to open a position"


# ---------------------------------------------------------------------------
# Invariant 1: equity == allocated + NET realized + NET open
# ---------------------------------------------------------------------------


class TestEquityIdentity:
    def test_flat_book_reports_zero_everywhere(self):
        runner = _runner()
        assert runner.gross_realized_pnl == 0.0
        assert runner.total_fees_paid == 0.0
        assert runner.realized_pnl == 0.0
        assert runner.open_pnl() == 0.0
        assert runner.daily_pnl() == 0.0
        assert runner.equity() == pytest.approx(runner.config.allocated_capital)

    def test_invariant_holds_while_a_position_is_open(self):
        """The entry fee is charged to the mark, so identity closes at once."""
        runner = _runner()
        _open_a_position(runner)
        _feed(runner, [118.0])  # mark the book up

        assert runner.total_fees_paid > 0.0, "fixture traded without costs"
        assert abs(runner.equity() - (
            runner.config.allocated_capital + runner.realized_pnl + runner.open_pnl()
        )) <= TOLERANCE

    def test_fees_on_an_open_book_are_not_yet_realized(self):
        """Nothing has closed, so nothing is realized — the cost is in the mark."""
        runner = _runner()
        _open_a_position(runner)
        assert runner.realized_pnl == pytest.approx(0.0, abs=TOLERANCE)
        assert runner.gross_realized_pnl == pytest.approx(0.0, abs=TOLERANCE)
        assert runner.open_pnl() < runner.unrealized_pnl()

    def test_invariant_holds_after_a_full_close(self):
        runner = _runner()
        _open_a_position(runner)
        _feed(runner, [118.0])
        runner.close_position("RELIANCE")

        assert not runner.positions
        assert abs(runner.equity() - (
            runner.config.allocated_capital + runner.realized_pnl + runner.open_pnl()
        )) <= TOLERANCE
        # Every rupee of cost the round trip paid is now on the realized side.
        assert runner.realized_pnl == pytest.approx(
            runner.gross_realized_pnl - runner.total_fees_paid, abs=TOLERANCE
        )

    def test_realized_is_net_of_the_costs_that_earned_it(self):
        runner = _runner()
        _open_a_position(runner)
        _feed(runner, [118.0])
        runner.close_position("RELIANCE")
        assert runner.gross_realized_pnl > 0.0
        assert runner.realized_pnl < runner.gross_realized_pnl
        assert runner.gross_realized_pnl - runner.realized_pnl == pytest.approx(
            runner.total_fees_paid, abs=TOLERANCE
        )


# ---------------------------------------------------------------------------
# Invariant 2 + defect #2: today's P&L keeps a scale-out's realized half
# ---------------------------------------------------------------------------


class TestDailyPnlIsADelta:
    def test_invariant_holds_for_a_book_spawned_today(self):
        runner = _runner()
        _open_a_position(runner)
        _feed(runner, [112.0])
        assert runner.prev_close_open_pnl == 0.0
        assert abs(runner.daily_pnl() - (
            runner.realized_pnl_today + runner.open_pnl()
        )) <= TOLERANCE

    def test_partial_scaleout_keeps_its_realized_half_in_today(self):
        """BUG-04 defect #2: half out at a profit, half still working.

        The old reader only counted round trips that CLOSED today, so the
        banked half vanished from ``daily_pnl`` while the open half's mark
        stayed — the TCS row showed the mark but not the ₹429.20.
        """
        runner = _runner()
        _open_a_position(runner, price=100.0)
        _feed(runner, [130.0])
        gross_open_before = runner.unrealized_pnl()

        runner.close_position("RELIANCE", fraction=0.5)

        assert runner.positions, "scale-out must leave the rest working"
        banked = runner.gross_realized_pnl
        assert banked > 0.0, "fixture must bank a profit on the closed half"
        # The half that closed is in TODAY even though the book is still open,
        # and the mark that remains is the other half — together they are the
        # whole move, so nothing is double-counted and nothing is dropped.
        assert runner.realized_pnl_today == pytest.approx(
            runner.realized_pnl, abs=TOLERANCE
        )
        assert runner.realized_pnl_today > 0.0
        assert runner.open_pnl() == pytest.approx(
            gross_open_before / 2 - runner._open_fees(), abs=TOLERANCE
        )
        assert abs(runner.daily_pnl() - (
            runner.realized_pnl_today + runner.open_pnl()
        )) <= TOLERANCE
        assert abs(runner.equity() - (
            runner.config.allocated_capital + runner.realized_pnl + runner.open_pnl()
        )) <= TOLERANCE

    def test_scaleout_leaves_the_survivors_costs_in_the_mark(self):
        """A fee follows the book that still has to recover it.

        The surviving half carries its entry brokerage, so it is netted out of
        ``open_pnl`` — not charged to realized — until that half closes too.
        The split is a convention, but it is the one that makes a partial exit
        move P&L the way a real contract note would.
        """
        runner = _runner()
        _open_a_position(runner)
        _feed(runner, [125.0])
        runner.close_position("RELIANCE", fraction=0.5)

        assert runner._open_fees() > 0.0, "the survivor keeps its entry cost"
        assert runner.realized_pnl == pytest.approx(
            runner.gross_realized_pnl - (runner.total_fees_paid - runner._open_fees()),
            abs=TOLERANCE,
        )
        # Close the rest and the same rupees move to the realized side.
        runner.close_position("RELIANCE")
        assert not runner.positions
        assert runner._open_fees() == pytest.approx(0.0, abs=TOLERANCE)
        assert runner.realized_pnl == pytest.approx(
            runner.gross_realized_pnl - runner.total_fees_paid, abs=TOLERANCE
        )

    def test_new_session_drops_yesterdays_realized_from_today(self):
        runner = _runner()
        _open_a_position(runner)
        _feed(runner, [130.0])
        runner.close_position("RELIANCE")
        assert runner.realized_pnl_today != 0.0

        # A bar from the NEXT session rolls the anchors: yesterday's banked
        # P&L is now the prior close, and today starts flat.
        _feed(runner, [130.0], start_day=1)
        assert runner.prev_close_open_pnl == 0.0
        assert runner.realized_pnl_today == pytest.approx(0.0, abs=TOLERANCE)
        assert runner.daily_pnl() == pytest.approx(runner.open_pnl(), abs=TOLERANCE)
        assert abs(runner.daily_pnl() - (
            runner.realized_pnl_today + runner.open_pnl()
        )) <= TOLERANCE

    def test_mtm_move_only_counts_the_delta_not_the_lifetime(self):
        """Re-marking the same book twice must not re-book the first mark."""
        runner = _runner()
        _open_a_position(runner)
        _feed(runner, [110.0])
        first = runner.daily_pnl()
        _feed(runner, [110.0])
        assert runner.daily_pnl() == pytest.approx(first, abs=TOLERANCE)


# ---------------------------------------------------------------------------
# The contract on the wire: state rows, bucket aggregates, snapshot rows
# ---------------------------------------------------------------------------


class TestContractOnTheWire:
    def test_state_carries_every_contract_field(self):
        runner = _runner()
        _open_a_position(runner)
        _feed(runner, [121.0])
        state = runner.get_state()
        for key in (
            "gross_realized_pnl",
            "total_fees_paid",
            "realized_pnl",
            "realized_pnl_today",
            "open_pnl",
            "prev_close_open_pnl",
            "daily_pnl",
            "equity",
            "allocated_capital",
        ):
            assert key in state, f"runner state is missing {key}"
        assert state["realized_pnl"] <= state["gross_realized_pnl"]
        assert state["total_fees_paid"] > 0.0
        assert abs(state["equity"] - (
            state["allocated_capital"] + state["realized_pnl"] + state["open_pnl"]
        )) <= 0.05

    def test_bucket_aggregates_expose_the_contract(self):
        from backtest.forward.portfolio_manager import PortfolioManager
        from backtest.forward.feed_registry import reset_data_bus
        from backtest.forward.risk_supervisor import GlobalRiskConfig

        reset_data_bus()
        manager = PortfolioManager(
            risk_config=GlobalRiskConfig(daily_loss_limit=1_000_000, max_drawdown_pct=0.9),
            tick_seconds=1.0,
            warmup_bars=5,
            auto_start_feed=False,
        )
        try:
            pid = manager.add_runner(
                RunnerConfig(
                    name="BUCKET",
                    strategy_name="buy_and_hold",
                    allocated_capital=100_000,
                    symbols=["AAA"],
                    timeframe="1hour",
                    mode="paper",
                    source="synthetic",
                ),
                start=False,
            )
            runner = manager.get_runner(pid)
            manager.broker.submit_market(pid, "AAA", SIDE_BUY, 100, 100.0)
            runner.last_price["AAA"] = 120.0
            manager.broker.submit_market(pid, "AAA", SIDE_SELL, 100, 120.0)

            agg = manager.get_bucket_aggregates()["paper"]
            for key in (
                "gross_realized_pnl",
                "total_fees_paid",
                "realized_pnl",
                "realized_pnl_today",
                "open_pnl",
            ):
                assert key in agg, f"bucket aggregate is missing {key}"
            assert agg["realized_pnl"] < agg["gross_realized_pnl"], (
                "net realized must show the costs it netted"
            )
            assert agg["open_pnl"] == pytest.approx(0.0, abs=TOLERANCE)
            summary = manager.get_portfolio_summary(mode="paper")
            assert summary["total_equity"] == pytest.approx(
                summary["total_capital"] + summary["realized_pnl"] + summary["open_pnl"],
                abs=0.05,
            )
        finally:
            manager.shutdown()

    def test_equity_snapshot_rows_carry_gross_and_fees(self):
        from backtest.forward.feed_registry import reset_data_bus
        from backtest.forward.portfolio_manager import PortfolioManager
        from backtest.forward.risk_supervisor import GlobalRiskConfig

        reset_data_bus()
        manager = PortfolioManager(
            risk_config=GlobalRiskConfig(daily_loss_limit=1_000_000, max_drawdown_pct=0.9),
            tick_seconds=1.0,
            warmup_bars=5,
            auto_start_feed=False,
        )
        try:
            manager.add_runner(
                RunnerConfig(
                    name="SNAP",
                    strategy_name="buy_and_hold",
                    allocated_capital=50_000,
                    symbols=["AAA"],
                    timeframe="1hour",
                    mode="paper",
                    source="synthetic",
                ),
                start=False,
            )
            rows = manager.get_equity_snapshots()["per_runner"]
            assert rows, "snapshot produced no runner rows"
            for key in ("gross_realized_pnl", "total_fees_paid", "realized_pnl_today"):
                assert key in rows[0], f"snapshot row is missing {key}"
        finally:
            manager.shutdown()


# ---------------------------------------------------------------------------
# Restart: the anchors are state, not something to re-derive from the book
# ---------------------------------------------------------------------------


class TestAnchorsAcrossARestart:
    def test_anchors_survive_capture_and_restore(self):
        runner = _runner()
        _open_a_position(runner)
        _feed(runner, [130.0])
        runner.close_position("RELIANCE", fraction=0.5)
        expected_today = runner.realized_pnl_today
        expected_daily = runner.daily_pnl()

        revived = _runner(name="EQ")
        restore_runner(revived, capture_runner(runner))

        assert revived.realized_pnl_today == pytest.approx(expected_today, abs=TOLERANCE)
        assert revived.daily_pnl() == pytest.approx(expected_daily, abs=TOLERANCE)

    def test_pre_contract_snapshot_does_not_claim_its_lifetime(self):
        """A state file written before BUG-04 must baseline, not reset to 0.

        Restoring a book with history and anchors of 0 would report the whole
        lifetime as today's P&L until the first EOD — the failure this ticket
        is fixing, in the other direction.
        """
        runner = _runner()
        _open_a_position(runner)
        _feed(runner, [130.0])
        runner.close_position("RELIANCE")
        assert runner.realized_pnl_today != 0.0

        snapshot = capture_runner(runner)
        for key in (
            "pnl_session",
            "last_session_realized",
            "last_session_open",
            "prev_close_realized_net",
            "prev_close_open_pnl",
        ):
            snapshot.pop(key, None)
        # The book's life predates its newest bar — i.e. it has history from an
        # earlier session, which is exactly the case that must NOT be re-claimed.
        snapshot["created_ts"] = "2026-09-01T09:15:00"

        revived = _runner(name="EQ")
        restore_runner(revived, snapshot)
        assert revived.realized_pnl != 0.0, "book must come back with its history"
        assert revived.realized_pnl_today == pytest.approx(0.0, abs=TOLERANCE)
        assert revived.daily_pnl() == pytest.approx(0.0, abs=TOLERANCE)

    def test_snapshot_created_today_keeps_its_own_day(self):
        """The other branch: a book born this session must keep its realized.

        Baselining a runner that started today would erase the morning's losses
        on a mid-day restart — the failure mode the re-baseline guard exists for.
        """
        runner = _runner()
        _open_a_position(runner)
        _feed(runner, [130.0])
        runner.close_position("RELIANCE")
        expected = runner.realized_pnl_today
        assert expected != 0.0

        snapshot = capture_runner(runner)
        for key in (
            "pnl_session",
            "last_session_realized",
            "last_session_open",
            "prev_close_realized_net",
            "prev_close_open_pnl",
        ):
            snapshot.pop(key, None)
        # Same session as the newest bar in the book.
        snapshot["created_ts"] = "2026-09-10T09:15:00"

        revived = _runner(name="EQ")
        restore_runner(revived, snapshot)
        assert revived.realized_pnl_today == pytest.approx(expected, abs=TOLERANCE)
